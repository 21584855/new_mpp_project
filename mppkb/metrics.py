"""Metrics, promotion gates and monitoring (ticket 29).

Everything here is deterministic and offline: reports are computed from the
governance database, evaluation runs the same answer service over labeled
cases, and the promotion gate returns explicit checks instead of a score.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from typing import Any

from .config import Config
from .db import ensure_databases, library_version
from .routing import route

TEST_SETS: dict[str, dict[str, Any]] = {
    "snapshot_regression": {
        "description": "同一冷库快照下重复回答，结果与引用稳定",
        "target": {"citation_accuracy": 0.9},
    },
    "hot_update": {
        "description": "热库高等级更新被显式提示且不改写推荐",
        "target": {"review_trigger_rate": 1.0},
    },
    "conflict_six_types": {
        "description": "数值/类别/时间/范围/来源/版本六类冲突",
        "target": {"conflict_recall": 0.9},
    },
    "safety_exception": {
        "description": "P0 撤市/禁忌/黑框：警示并压制推荐",
        "target": {"high_risk_block_rate": 1.0},
    },
    "patient_data_conflict": {
        "description": "病例数据与知识库结论不一致时标记不确定",
        "target": {"flag_rate": 1.0},
    },
    "edge_cases": {
        "description": "部分晋升、多语言、日期歧义、撤回",
        "target": {"citation_accuracy": 0.9},
    },
}

THRESHOLDS: dict[str, float] = {
    "conflict_recall": 0.9,
    "conflict_false_positive": 0.15,
    "citation_accuracy": 0.9,
    "routing_accuracy": 0.9,
    "cache_hit_rate": 0.3,
    "high_risk_block_rate": 1.0,
}


def _check(check_id: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"id": check_id, "passed": bool(passed), "detail": detail}


def _records(kb) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in kb.execute("SELECT record_json FROM answer_log ORDER BY seq").fetchall():
        try:
            records.append(json.loads(row["record_json"]))
        except json.JSONDecodeError:
            continue
    return records


def _citation_accuracy(records: list[dict[str, Any]]) -> float | None:
    answered = [r for r in records if not r.get("refused")]
    if not answered:
        return None
    good = sum(
        1
        for r in answered
        if r.get("citations") and all(c.get("doc_id") and c.get("page") for c in r["citations"])
    )
    return round(good / len(answered), 4)


def _cache_hit_rate(records: list[dict[str, Any]]) -> float | None:
    if not records:
        return None
    return round(sum(1 for r in records if r.get("cache_hit")) / len(records), 4)


def _avg_latency(records: list[dict[str, Any]]) -> float | None:
    values = [r.get("latency_ms") for r in records if isinstance(r.get("latency_ms"), (int, float))]
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _count(kb, sql: str, params: tuple = ()) -> int:
    row = kb.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def gate(kb, baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    p0_drugs = {
        row["drug"] for row in kb.execute("SELECT drug FROM p0_rules WHERE enabled = 1")
    }
    conflicts = kb.execute(
        "SELECT v.*, c.intervention FROM claim_versions v "
        "JOIN claims c ON c.claim_id = v.claim_id WHERE v.status = 'conflicted'"
    ).fetchall()
    p0_conflicts = [row for row in conflicts if row["intervention"] in p0_drugs]
    checks.append(
        _check(
            "no_unresolved_p0_conflicts",
            not p0_conflicts,
            f"{len(p0_conflicts)} unresolved conflict(s) on P0 drugs",
        )
    )

    missing_reports = [
        row
        for row in conflicts
        if kb.execute(
            "SELECT 1 FROM claim_events WHERE version_id = ? AND event_type = 'conflict_classified' LIMIT 1",
            (row["version_id"],),
        ).fetchone()
        is None
    ]
    checks.append(
        _check(
            "conflict_reports_complete",
            not missing_reports,
            f"{len(missing_reports)} conflicted version(s) without a report",
        )
    )

    approved = kb.execute("SELECT * FROM claim_versions WHERE status = 'approved'").fetchall()
    unsigned = [row for row in approved if not row["decided_by"]]
    checks.append(
        _check("reviewer_signed", not unsigned, f"{len(unsigned)} approved version(s) unsigned")
    )

    covered: set[str] = set()
    for row in kb.execute("SELECT claim_ids_json FROM kb_snapshots").fetchall():
        covered.update(json.loads(row["claim_ids_json"]))
    missing_snapshot = [row for row in approved if row["version_id"] not in covered]
    checks.append(
        _check(
            "snapshots_complete",
            not missing_snapshot,
            f"{len(missing_snapshot)} approved version(s) outside snapshots",
        )
    )

    current_version = library_version(kb)
    stale_cache = _count(
        kb, "SELECT COUNT(*) FROM retrieval_cache WHERE library_version <> ?", (current_version,)
    )
    legacy_cache = _count(kb, "SELECT COUNT(*) FROM answer_cache")
    checks.append(
        _check(
            "cache_versioned",
            stale_cache == 0 and legacy_cache == 0,
            f"stale={stale_cache}, legacy_answer_cache={legacy_cache}",
        )
    )

    unaudited = [
        row
        for row in approved
        if kb.execute(
            "SELECT 1 FROM claim_events WHERE version_id = ? LIMIT 1", (row["version_id"],)
        ).fetchone()
        is None
    ]
    checks.append(
        _check("audit_trail", not unaudited, f"{len(unaudited)} approved version(s) without events")
    )

    if baseline is None:
        checks.append(_check("no_regression", True, "no baseline supplied"))
    else:
        current_accuracy = _citation_accuracy(_records(kb))
        base_accuracy = (baseline.get("metrics") or {}).get("citation_accuracy")
        passed = (
            current_accuracy is None
            or base_accuracy is None
            or current_accuracy >= float(base_accuracy) - 0.02
        )
        checks.append(
            _check(
                "no_regression",
                passed,
                f"citation_accuracy now={current_accuracy} baseline={base_accuracy}",
            )
        )

    return {
        "ok": all(check["passed"] for check in checks),
        "checks": checks,
        "library_version": current_version,
    }


def report(kb, cfg: Config) -> dict[str, Any]:
    records = _records(kb)
    conflicts_open = _count(
        kb, "SELECT COUNT(*) FROM claim_versions WHERE status = 'conflicted'"
    )
    counts = {
        "claims": _count(kb, "SELECT COUNT(*) FROM claims"),
        "versions": _count(kb, "SELECT COUNT(*) FROM claim_versions"),
        "approved": _count(kb, "SELECT COUNT(*) FROM claim_versions WHERE status = 'approved'"),
        "conflicted": conflicts_open,
        "proposed": _count(kb, "SELECT COUNT(*) FROM claim_versions WHERE status = 'proposed'"),
        "expired": _count(kb, "SELECT COUNT(*) FROM claim_versions WHERE status = 'expired'"),
        "snapshots": _count(kb, "SELECT COUNT(*) FROM kb_snapshots"),
        "hot_claimed": _count(kb, "SELECT COUNT(*) FROM claim_versions WHERE status = 'proposed'"),
        "retrieval_cache": _count(kb, "SELECT COUNT(*) FROM retrieval_cache"),
        "answer_log": len(records),
    }
    routing = Counter(
        (record.get("routing") or {}).get("path") for record in records
    )
    metrics = {
        "citation_accuracy": _citation_accuracy(records),
        "cache_hit_rate": _cache_hit_rate(records),
        "avg_latency_ms": _avg_latency(records),
        "refused_rate": round(
            sum(1 for record in records if record.get("refused")) / len(records), 4
        )
        if records
        else None,
        "review_triggered": sum(1 for record in records if record.get("review_triggered")),
        "p0_answers": sum(1 for record in records if record.get("p0_warning")),
        "routing": dict(routing),
    }
    gate_result = gate(kb)
    return {
        "ok": True,
        "library_version": library_version(kb),
        "counts": counts,
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "test_sets": TEST_SETS,
        "gate": gate_result,
    }


def evaluate(kb, cfg: Config, cases: list[dict[str, Any]]) -> dict[str, Any]:
    from .answer import answer

    per_set: dict[str, Counter] = {}
    results: list[dict[str, Any]] = []
    for case in cases:
        question = case["question"]
        context = case.get("context") or {}
        routing = route(question, context)
        response = answer(kb, cfg, question, case_id=case.get("case_id"), case_context=context)
        expect = case.get("expect") or {}
        set_name = case.get("set") or "default"
        counters = per_set.setdefault(set_name, Counter())
        counters["cases"] += 1
        if expect.get("path") is None or routing["path"] == expect["path"]:
            counters["path_ok"] += 1
        if expect.get("refused") is None or bool(response.get("refused")) == bool(expect["refused"]):
            counters["refused_ok"] += 1
        citations = response.get("citations") or []
        if expect.get("citation") is None or bool(citations) == bool(expect["citation"]):
            counters["citation_ok"] += 1
        results.append(
            {
                "set": set_name,
                "question": question,
                "path": routing["path"],
                "refused": bool(response.get("refused")),
                "citations": len(citations),
                "latency_ms": response.get("latency_ms"),
            }
        )

    summary: dict[str, Any] = {}
    for set_name, counters in per_set.items():
        total = counters["cases"] or 1
        summary[set_name] = {
            "cases": counters["cases"],
            "routing_accuracy": round(counters["path_ok"] / total, 4),
            "refusal_accuracy": round(counters["refused_ok"] / total, 4),
            "citation_accuracy": round(counters["citation_ok"] / total, 4),
            "target": TEST_SETS.get(set_name, {}).get("target", {}),
        }
    return {"ok": True, "testsets": summary, "results": results}


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _fail(args: Any, message: str, code: int = 1) -> int:
    if getattr(args, "json", False):
        _emit({"ok": False, "error": message})
    else:
        print(f"error: {message}")
    return code


def run(args: Any, cfg: Config) -> int:
    kb, _corpus = ensure_databases(cfg)
    command = getattr(args, "metrics_command", "report")

    if command == "report":
        payload = report(kb, cfg)
        if getattr(args, "json", False):
            _emit(payload)
        else:
            print(f"library_version: {payload['library_version']}")
            for name, value in payload["metrics"].items():
                print(f"{name}: {value}")
            print(f"gate: {'pass' if payload['gate']['ok'] else 'fail'}")
        return 0

    if command == "gate":
        baseline = None
        baseline_path = getattr(args, "baseline", None)
        if baseline_path:
            try:
                baseline = json.loads(open(baseline_path, encoding="utf-8").read())
            except (OSError, json.JSONDecodeError) as exc:
                return _fail(args, f"baseline read failed: {exc}")
        payload = gate(kb, baseline)
        if getattr(args, "json", False):
            _emit(payload)
        else:
            for check in payload["checks"]:
                print(f"{'PASS' if check['passed'] else 'FAIL'} {check['id']}: {check['detail']}")
        return 0 if payload["ok"] else 1

    if command == "eval":
        path = getattr(args, "cases", None)
        if not path:
            return _fail(args, "--cases is required")
        try:
            cases = json.loads(open(path, encoding="utf-8").read())
        except (OSError, json.JSONDecodeError) as exc:
            return _fail(args, f"cases read failed: {exc}")
        payload = evaluate(kb, cfg, cases)
        if getattr(args, "json", False):
            _emit(payload)
        else:
            for name, summary in payload["testsets"].items():
                print(f"{name}: {summary['cases']} cases, {summary}")
        return 0

    return _fail(args, f"unknown metrics command: {command}")
