"""LangGraph orchestration skeleton (ticket 25).

Two paths share one state object and one evidence service:

* simple  : tools (optional) → retrieve → conflict → synthesize → verify → finalize
* complex : plan → tools → gap_check → (retry ≤2) → retrieve → conflict → human_gate → synthesize → verify → finalize

Checkpoints live in a dedicated SQLite file next to the governance database so a
human-gate interrupt can be resumed by a later process. Every node writes one
redacted audit row at finalize time.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .answer import DISCLAIMER, EvidenceService, _active_snapshot, _value_text
from .clock import now_iso
from .config import Config
from .db import ensure_databases, library_version
from .routing import route
from .synthesis import synthesize as synthesize_claims
from .tools import run_tool

SEVERE_VALUES = ("severe", "refractory", "fulminant", "critical")
BUDGETS = {
    "simple": {"max_tool_calls": 4, "max_retrievals": 6, "max_gap_retries": 0},
    "complex": {"max_tool_calls": 10, "max_retrievals": 14, "max_gap_retries": 2},
}


class AgentState(TypedDict, total=False):
    case_id: str
    thread_id: str
    patient_context: dict
    question: str
    question_hash: str
    complexity: str
    route_reason: list[str]
    plan: list[dict]
    tool_results: list[dict]
    evidence: list[dict]
    evidence_gaps: list[str]
    pending_gaps: list[dict]
    gap_history: list[dict]
    degraded: bool
    conflicts: list[dict]
    answer: str
    citations: list[dict]
    synthesis: dict
    attribution: list[dict]
    synthesis_defects: list[dict]
    review_required: bool
    approved: bool
    gate_decision: str | None
    p0_warning: dict | None
    library_snapshot: str | None
    budget: dict
    budget_overrides: dict
    force_gap: bool
    review_requested: bool
    needs_rewrite: bool
    status: str
    audit: list[dict]


def _audit(state: AgentState, node: str, note: str = "") -> list[dict]:
    entries = list(state.get("audit") or [])
    entries.append({"node": node, "note": note})
    return entries


def _budget(state: AgentState) -> dict:
    return dict(state.get("budget") or {})


def _case_payload(state: AgentState, extra: dict | None = None) -> dict:
    return {
        "thread_id": state.get("thread_id"),
        "question_hash": state.get("question_hash"),
        "snapshot_id": state.get("library_snapshot"),
        "complexity": state.get("complexity"),
        "gap_retries": (state.get("budget") or {}).get("gap_retries", 0),
        "degraded": bool(state.get("degraded")),
        "tools": [
            {"tool": item.get("tool"), "status": item.get("status")}
            for item in state.get("tool_results") or []
        ],
        **(extra or {}),
    }


def _case_audit_upsert(
    kb, state: AgentState, decision: str, actor: str | None = None, extra: dict | None = None
) -> None:
    case_id = state.get("case_id")
    detail = json.dumps(_case_payload(state, extra), ensure_ascii=False)
    version = library_version(kb)
    with kb:
        if decision == "pending":
            cursor = kb.execute(
                "UPDATE case_audit SET detail_json = ?, path = ?, library_version = ? "
                "WHERE case_id = ? AND decision = 'pending'",
                (detail, state.get("complexity"), version, case_id),
            )
            if cursor.rowcount == 0:
                kb.execute(
                    "INSERT INTO case_audit(case_id, library_version, path, decision, actor, "
                    "detail_json, created_at) VALUES (?, ?, ?, 'pending', NULL, ?, ?)",
                    (case_id, version, state.get("complexity"), detail, now_iso()),
                )
            return
        cursor = kb.execute(
            "UPDATE case_audit SET decision = ?, actor = ?, detail_json = ?, path = ?, "
            "library_version = ? WHERE case_id = ? AND decision = 'pending'",
            (decision, actor, detail, state.get("complexity"), version, case_id),
        )
        if cursor.rowcount == 0:
            kb.execute(
                "INSERT INTO case_audit(case_id, library_version, path, decision, actor, "
                "detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    case_id,
                    version,
                    state.get("complexity"),
                    decision,
                    actor,
                    detail,
                    now_iso(),
                ),
            )


def _case_audit_final(kb, state: AgentState) -> None:
    case_id = state.get("case_id")
    detail = json.dumps(
        _case_payload(
            state,
            {
                "answer_chars": len(state.get("answer") or ""),
                "citations": len(state.get("citations") or []),
            },
        ),
        ensure_ascii=False,
    )
    version = library_version(kb)
    with kb:
        cursor = kb.execute(
            "UPDATE case_audit SET detail_json = ?, path = ?, library_version = ? "
            "WHERE case_id = ?",
            (detail, state.get("complexity"), version, case_id),
        )
        if cursor.rowcount == 0:
            kb.execute(
                "INSERT INTO case_audit(case_id, library_version, path, decision, actor, "
                "detail_json, created_at) VALUES (?, ?, ?, 'auto', NULL, ?, ?)",
                (case_id, version, state.get("complexity"), detail, now_iso()),
            )


def build_graph(kb, cfg: Config, service: EvidenceService | None = None, corpus=None):
    service = service or EvidenceService(kb, cfg)

    def intake(state: AgentState) -> AgentState:
        question = str(state.get("question") or "")
        context = state.get("patient_context") or {}
        return {
            **state,
            "case_id": state.get("case_id") or f"case-{uuid.uuid4().hex[:8]}",
            "question": question,
            "question_hash": hashlib.sha256(question.encode("utf-8")).hexdigest()[:32],
            "patient_context": context,
            "library_snapshot": _active_snapshot(kb),
            "status": "running",
            "audit": _audit(state, "intake"),
        }

    def triage(state: AgentState) -> AgentState:
        context = state.get("patient_context") or {}
        routing = route(state.get("question", ""), context)
        complexity = routing["path"]
        budget = {**BUDGETS[complexity], "tool_calls": 0, "retrieval_calls": 0,
                  "gap_retries": 0, "citation_rewrites": 0}
        for key, value in (state.get("budget_overrides") or {}).items():
            if key in budget and isinstance(value, int):
                budget[key] = value
        severity = str(context.get("severity") or "")
        review_required = bool(state.get("review_requested")) or (
            complexity == "complex" and severity in SEVERE_VALUES
        )
        return {
            **state,
            "complexity": complexity,
            "route_reason": routing["reasons"],
            "budget": budget,
            "review_required": review_required,
            "approved": not review_required,
            "audit": _audit(state, "triage", complexity),
        }

    def plan(state: AgentState) -> AgentState:
        context = state.get("patient_context") or {}
        question = state.get("question", "")
        drug = context.get("drug")
        age = context.get("age_years")
        weight = context.get("weight_kg")
        marker = context.get("lab_marker")
        existing = list(state.get("plan") or [])
        pending = list(state.get("pending_gaps") or [])
        retry_round = len(state.get("gap_history") or [])
        new_tasks: list[dict] = []
        if not existing:
            if drug and age is not None:
                new_tasks.append(
                    {
                        "id": "p0",
                        "tool": "p0_check",
                        "input": {"drug": drug, "age_years": age},
                        "depends_on": [],
                        "status": "pending",
                        "attempts": 0,
                    }
                )
            if drug and weight is not None:
                new_tasks.append(
                    {
                        "id": "dose",
                        "tool": "dose_calculator",
                        "input": {"drug": drug, "weight_kg": weight, "age_years": age},
                        "depends_on": ["p0"] if drug and age is not None else [],
                        "status": "pending",
                        "attempts": 0,
                    }
                )
            if marker:
                new_tasks.append(
                    {
                        "id": "lab",
                        "tool": "lab_threshold",
                        "input": {"marker": marker},
                        "depends_on": [],
                        "status": "pending",
                        "attempts": 0,
                    }
                )
            new_tasks.append(
                {
                    "id": "guideline",
                    "tool": "guideline_search",
                    "input": {"query": question, "top_k": 5},
                    "depends_on": [],
                    "status": "pending",
                    "attempts": 0,
                }
            )
        elif pending:
            by_tool = {task["tool"]: task for task in existing}
            for gap in pending:
                base = by_tool.get(gap["tool"])
                if base is None:
                    continue
                input_data = dict(base.get("input") or {})
                if gap["tool"] == "dose_calculator":
                    input_data.pop("age_years", None)
                elif gap["tool"] == "guideline_search":
                    input_data["query"] = str(input_data.get("query") or question)[:30]
                new_tasks.append(
                    {
                        "id": f"{gap['tool']}-r{retry_round}",
                        "tool": gap["tool"],
                        "input": input_data,
                        "depends_on": [],
                        "status": "pending",
                        "attempts": 0,
                    }
                )
        return {
            **state,
            "plan": existing + new_tasks,
            "pending_gaps": [],
            "audit": _audit(
                state, "plan", f"round={retry_round} new={len(new_tasks)}"
            ),
        }

    def tools(state: AgentState) -> AgentState:
        budget = _budget(state)
        tasks = list(state.get("plan") or [])
        if not tasks:
            return {**state, "audit": _audit(state, "tools", "no_plan")}
        results = list(state.get("tool_results") or [])
        exhausted = False
        for task in tasks:
            if task.get("status") in ("done", "error"):
                continue
            attempts = 0
            result: dict[str, Any] = {"status": "error", "error": "not executed"}
            while attempts < 2:
                if budget.get("tool_calls", 0) >= budget.get("max_tool_calls", 0):
                    exhausted = True
                    break
                attempts += 1
                budget["tool_calls"] = budget.get("tool_calls", 0) + 1
                result = run_tool(
                    task.get("tool", ""), task.get("input") or {}, kb, cfg, corpus
                )
                status = result.get("status", "error")
                with kb:
                    kb.execute(
                        "INSERT INTO kb_audit(event_seq, action, actor, detail_json, created_at) "
                        "VALUES (NULL, 'tool_call', 'system', ?, ?)",
                        (
                            json.dumps(
                                {
                                    "tool": task.get("tool"),
                                    "task_id": task.get("id"),
                                    "status": status,
                                    "thread_id": state.get("thread_id"),
                                    "question_hash": state.get("question_hash"),
                                },
                                ensure_ascii=False,
                            ),
                            now_iso(),
                        ),
                    )
                if status != "error":
                    break
            if exhausted:
                break
            task["status"] = "error" if result.get("status") == "error" else "done"
            task["attempts"] = attempts
            results.append(
                {
                    "task_id": task.get("id"),
                    "tool": task.get("tool"),
                    "status": result.get("status", "error"),
                    "attempts": attempts,
                    "result": result,
                }
            )
        updated = {
            **state,
            "budget": budget,
            "tool_results": results,
            "audit": _audit(
                state, "tools", "budget_exhausted" if exhausted else f"calls={len(results)}"
            ),
        }
        if exhausted:
            updated["status"] = "budget_exhausted"
            updated["answer"] = "预算耗尽：仅完成部分工具调用，以下为已收集信息。"
        return updated

    def gap_check(state: AgentState) -> AgentState:
        budget = _budget(state)
        latest: dict[str, dict] = {}
        for entry in state.get("tool_results") or []:
            latest[entry.get("tool", "")] = entry
        gaps: list[dict] = []
        for tool, entry in latest.items():
            status = entry.get("status")
            if tool == "dose_calculator" and status != "ok":
                gaps.append({"tool": tool, "reason": str(status)})
            elif tool == "p0_check" and status == "error":
                gaps.append({"tool": tool, "reason": "error"})
            elif tool == "lab_threshold" and status in ("not_found", "error"):
                gaps.append({"tool": tool, "reason": str(status)})
            elif tool == "guideline_search" and status in ("not_found", "error"):
                gaps.append({"tool": tool, "reason": str(status)})
        if state.get("force_gap"):
            gaps.append({"tool": "forced_gap", "reason": "injected"})
        history = list(state.get("gap_history") or [])
        retries = int(budget.get("gap_retries", 0))
        if gaps:
            history.append({"round": retries, "gaps": gaps})
        if gaps and retries < int(budget.get("max_gap_retries", 0)):
            budget["gap_retries"] = retries + 1
            return {
                **state,
                "budget": budget,
                "pending_gaps": gaps,
                "gap_history": history,
                "evidence_gaps": [],
                "audit": _audit(state, "gap_check", f"retry-{retries + 1}"),
            }
        final_gaps = [gap["tool"] for gap in gaps]
        return {
            **state,
            "budget": budget,
            "pending_gaps": [],
            "gap_history": history,
            "evidence_gaps": final_gaps,
            "degraded": bool(final_gaps),
            "audit": _audit(
                state, "gap_check", "degraded" if final_gaps else "clear"
            ),
        }

    def retrieve(state: AgentState) -> AgentState:
        budget = _budget(state)
        if budget.get("retrieval_calls", 0) >= budget.get("max_retrievals", 0):
            return {
                **state,
                "status": "budget_exhausted",
                "answer": "证据不足（检索预算耗尽），以下仅为已收集信息。",
                "audit": _audit(state, "retrieve", "budget_exhausted"),
            }
        budget["retrieval_calls"] = budget.get("retrieval_calls", 0) + 1
        retrieval = service.retrieve(state.get("question", ""))
        evidence = [
            {
                "version_id": item["row"]["version_id"],
                "claim_id": item["row"]["claim_id"],
                "status": item["row"]["status"],
                "intervention": item["row"].get("intervention"),
                "attribute": item["row"].get("attribute"),
                "value": json.loads(item["row"]["value_json"]),
                "direction": item["row"].get("direction"),
                "anchors": item["anchors"],
                "provenance": item["row"].get("provenance"),
            }
            for item in retrieval["items"]
        ]
        gaps = list(state.get("evidence_gaps") or [])
        if not evidence and "no_evidence" not in gaps:
            gaps.append("no_evidence")
        return {
            **state,
            "budget": budget,
            "evidence": evidence,
            "evidence_gaps": gaps,
            "audit": _audit(state, "retrieve", f"{retrieval['evidence_status']}:{len(evidence)}"),
        }

    def conflict_resolve(state: AgentState) -> AgentState:
        warning = None
        for item in state.get("evidence") or []:
            if item.get("provenance") != "p0_exception":
                continue
            rule = kb.execute(
                "SELECT rule_id, template FROM p0_rules WHERE drug = ? AND enabled = 1",
                (item.get("intervention"),),
            ).fetchone()
            if rule is not None:
                warning = {
                    "rule_id": rule["rule_id"],
                    "template": rule["template"],
                    "claim_id": item.get("claim_id"),
                }
                break
        return {
            **state,
            "conflicts": [],
            "p0_warning": warning,
            "audit": _audit(
                state, "conflict_resolve", "p0" if warning else "clear"
            ),
        }

    def human_gate(state: AgentState) -> AgentState:
        warning = state.get("p0_warning")
        warning_text = f"【安全警示】{warning['template']}" if warning else None
        interrupt_payload = {
            "reason": "high_risk",
            "case_id": state.get("case_id"),
            "question_hash": state.get("question_hash"),
            "p0_warning": warning,
            "p0_warning_text": warning_text,
        }
        _case_audit_upsert(kb, state, "pending")
        decision = str(interrupt(interrupt_payload) or "").strip().lower()
        if decision == "approve":
            _case_audit_upsert(kb, state, "approved", cfg.reviewer)
            return {
                **state,
                "approved": True,
                "gate_decision": "approved",
                "audit": _audit(state, "human_gate", "approved"),
            }
        label = {"timeout": "超时", "reject": "拒绝"}.get(decision, "拒绝")
        stored = "timeout" if decision == "timeout" else "rejected"
        warning_text = f"【安全警示】{warning['template']}\n" if warning else ""
        answer = (
            warning_text
            + f"本病例未通过人审（{label}），不输出治疗建议。\n"
            + "【未签核】"
            + "\n"
            + DISCLAIMER
        )
        _case_audit_upsert(kb, state, stored, cfg.reviewer)
        return {
            **state,
            "approved": False,
            "status": "timeout" if decision == "timeout" else "rejected",
            "gate_decision": stored,
            "answer": answer,
            "citations": [],
            "sections": [],
            "audit": _audit(state, "human_gate", stored),
        }

    def synthesize(state: AgentState) -> AgentState:
        evidence = state.get("evidence") or []
        gaps = state.get("evidence_gaps") or []
        warning = state.get("p0_warning")
        warning_text = f"【安全警示】{warning['template']}\n" if warning else ""
        gap_note = (
            "证据不足（工具缺口未补齐：" + "、".join(gaps) + "）。"
            if gaps
            else ""
        )
        if not evidence:
            return {
                **state,
                "answer": warning_text + (gap_note or "") + "没有足够的当前证据回答该问题。\n" + DISCLAIMER,
                "citations": [],
                "attribution": [],
                "synthesis": {"backend": "rule", "sentences": 0, "defects": 0},
                "synthesis_defects": [],
                "needs_rewrite": False,
                "audit": _audit(state, "synthesize", "refused"),
            }
        facts: list[dict] = []
        for item in evidence:
            label = (
                f"{service._label(item.get('intervention'))}"
                f"{service._label(item.get('attribute'))}"
            )
            text = f"{label}：{_value_text(item.get('value') or {}, item.get('direction'))}"
            facts.append(
                {
                    "claim_id": item.get("claim_id"),
                    "text": text,
                    "anchors": item.get("anchors") or [],
                }
            )
        synthesized = synthesize_claims(facts)
        body = synthesized.get("answer") or ""
        if not synthesized.get("sentences"):
            body = "没有足够的当前证据回答该问题。"
        answer = warning_text + (gap_note + ("以下仅为冷库证据。\n" if gap_note else "")) + body + "\n" + DISCLAIMER
        return {
            **state,
            "answer": answer,
            "citations": synthesized.get("citations") or [],
            "attribution": synthesized.get("attribution") or [],
            "synthesis": {
                **synthesized.get("meta", {}),
                "sentences": len(synthesized.get("sentences") or []),
                "defects": len(synthesized.get("defects") or []),
                "rewrites": synthesized.get("rewrites", 0),
            },
            "synthesis_defects": synthesized.get("defects") or [],
            "needs_rewrite": False,
            "audit": _audit(
                state, "synthesize", f"sentences={len(synthesized.get('sentences') or [])}"
            ),
        }

    def verify(state: AgentState) -> AgentState:
        citations = state.get("citations") or []
        invalid = [c for c in citations if not c.get("doc_id") or not c.get("page")]
        budget = _budget(state)
        if invalid and budget.get("citation_rewrites", 0) < 1:
            budget["citation_rewrites"] = budget.get("citation_rewrites", 0) + 1
            return {
                **state,
                "budget": budget,
                "needs_rewrite": True,
                "audit": _audit(state, "verify", "rewrite"),
            }
        return {**state, "needs_rewrite": False, "audit": _audit(state, "verify", "ok")}

    def finalize(state: AgentState) -> AgentState:
        status = state.get("status") or "done"
        if status == "running":
            status = "done"
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with kb:
            for entry in state.get("audit") or []:
                kb.execute(
                    "INSERT INTO kb_audit(event_seq, action, actor, detail_json, created_at) "
                    "VALUES (NULL, 'graph_node', 'system', ?, ?)",
                    (
                        json.dumps(
                            {
                                "thread_id": state.get("thread_id"),
                                "node": entry["node"],
                                "note": entry.get("note", ""),
                                "question_hash": state.get("question_hash"),
                                "snapshot_id": state.get("library_snapshot"),
                            },
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
        _case_audit_final(kb, state)
        return {**state, "status": status, "audit": _audit(state, "finalize", status)}

    builder = StateGraph(AgentState)
    builder.add_node("intake", intake)
    builder.add_node("triage", triage)
    builder.add_node("plan", plan)
    builder.add_node("tools", tools)
    builder.add_node("gap_check", gap_check)
    builder.add_node("retrieve", retrieve)
    builder.add_node("conflict_resolve", conflict_resolve)
    builder.add_node("human_gate", human_gate)
    builder.add_node("synthesize", synthesize)
    builder.add_node("verify", verify)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "triage")
    builder.add_conditional_edges(
        "triage", lambda s: "plan" if s.get("complexity") == "complex" else "tools"
    )
    builder.add_edge("plan", "tools")
    builder.add_conditional_edges(
        "tools",
        lambda s: "finalize"
        if s.get("status") == "budget_exhausted"
        else ("gap_check" if s.get("complexity") == "complex" else "retrieve"),
    )
    builder.add_conditional_edges(
        "gap_check",
        lambda s: "plan" if s.get("pending_gaps") else "retrieve",
    )
    builder.add_conditional_edges(
        "retrieve",
        lambda s: "finalize" if s.get("status") == "budget_exhausted" else "conflict_resolve",
    )
    builder.add_conditional_edges(
        "conflict_resolve",
        lambda s: "human_gate" if s.get("review_required") and not s.get("approved") else "synthesize",
    )
    builder.add_conditional_edges(
        "human_gate", lambda s: "synthesize" if s.get("approved") else "finalize"
    )
    builder.add_edge("synthesize", "verify")
    builder.add_conditional_edges(
        "verify", lambda s: "synthesize" if s.get("needs_rewrite") else "finalize"
    )
    builder.add_edge("finalize", END)

    return builder


def _graph_db(cfg: Config) -> Path:
    return Path(cfg.kb_db).parent / "graph.sqlite"


def _output(state: dict, thread_id: str, interrupted: bool) -> dict:
    return {
        "ok": True,
        "thread_id": thread_id,
        "status": "interrupted" if interrupted else state.get("status", "done"),
        "interrupted": interrupted,
        "complexity": state.get("complexity"),
        "route_reason": state.get("route_reason", []),
        "answer": state.get("answer", ""),
        "citations": state.get("citations", []),
        "budget": state.get("budget", {}),
        "audit": [entry["node"] for entry in state.get("audit", [])],
        "plan": [
            {
                "id": task.get("id"),
                "tool": task.get("tool"),
                "depends_on": task.get("depends_on", []),
                "status": task.get("status"),
                "attempts": task.get("attempts", 0),
            }
            for task in state.get("plan") or []
        ],
        "gaps": state.get("gap_history", []),
        "gap_retries": (state.get("budget") or {}).get("gap_retries", 0),
        "degraded": bool(state.get("degraded")),
        "p0_warning": state.get("p0_warning"),
        "gate_decision": state.get("gate_decision"),
        "synthesis": state.get("synthesis", {}),
        "attribution": state.get("attribution", []),
        "synthesis_defects": state.get("synthesis_defects", []),
        "tools": [
            {"tool": item.get("tool"), "status": item.get("status")}
            for item in state.get("tool_results") or []
        ],
        "snapshot_id": state.get("library_snapshot"),
    }


def run_case(
    kb,
    cfg: Config,
    corpus,
    case: dict,
    thread_id: str | None = None,
) -> dict:
    """Run one case through the LangGraph runtime and return the CLI-shaped payload."""
    thread_id = thread_id or f"thread-{uuid.uuid4().hex[:12]}"
    graph_db = _graph_db(cfg)
    config = {"configurable": {"thread_id": thread_id}}
    with SqliteSaver.from_conn_string(str(graph_db)) as saver:
        graph = build_graph(kb, cfg, corpus=corpus).compile(checkpointer=saver)
        initial: AgentState = {
            "case_id": case.get("case_id") or thread_id,
            "thread_id": thread_id,
            "question": case.get("question", ""),
            "patient_context": case.get("patient_context") or {},
            "budget_overrides": case.get("budget") or {},
            "force_gap": bool(case.get("force_gap")),
            "review_requested": bool(case.get("review_required")),
        }
        result = graph.invoke(initial, config)
    interrupted = "__interrupt__" in result
    payload = _output(result, thread_id, interrupted)
    if interrupted:
        payload["interrupt"] = [
            getattr(item, "value", None)
            for item in result.get("__interrupt__") or []
        ]
    return payload


def run(args: Any, cfg: Config) -> int:
    command = getattr(args, "graph_command", "run")
    kb, corpus = ensure_databases(cfg)
    graph_db = _graph_db(cfg)
    thread_id = getattr(args, "thread_id", None) or f"thread-{uuid.uuid4().hex[:12]}"
    config = {"configurable": {"thread_id": thread_id}}

    try:
        with SqliteSaver.from_conn_string(str(graph_db)) as saver:
            graph = build_graph(kb, cfg, corpus=corpus).compile(checkpointer=saver)
            if command == "run":
                case_path = Path(getattr(args, "case_json", "") or "")
                if not case_path.is_file():
                    raise FileNotFoundError(f"case file not found: {case_path}")
                case = json.loads(case_path.read_text(encoding="utf-8"))
                initial: AgentState = {
                    "case_id": case.get("case_id") or thread_id,
                    "thread_id": thread_id,
                    "question": case.get("question", ""),
                    "patient_context": case.get("patient_context") or {},
                    "budget_overrides": case.get("budget") or {},
                    "force_gap": bool(case.get("force_gap")),
                    "review_requested": bool(case.get("review_required")),
                }
                result = graph.invoke(initial, config)
            elif command == "resume":
                result = graph.invoke(
                    Command(resume=getattr(args, "decision", "approve")), config
                )
            else:
                raise ValueError(f"unknown graph command: {command}")
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        payload = {"ok": False, "error": str(exc)}
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"error: {exc}")
        return 1

    interrupted = "__interrupt__" in result
    payload = _output(result, thread_id, interrupted)
    if interrupted:
        interrupts = result.get("__interrupt__") or []
        payload["interrupt"] = [
            getattr(item, "value", None) for item in interrupts
        ]
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"thread: {thread_id}")
        print(f"status: {payload['status']}")
        if payload["answer"]:
            print(payload["answer"])
    return 0
