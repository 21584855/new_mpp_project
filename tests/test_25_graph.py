"""Ticket 25 acceptance: LangGraph skeleton, budgets, checkpoint and human gate."""

from __future__ import annotations

import json
from pathlib import Path

from support import run_cli

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINI = FIXTURES / "mini_two_pages.pdf"


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    values = {
        "MPPKB_KB_DB": str(tmp_path / "data" / "kb.sqlite"),
        "MPPKB_CORPUS_DB": str(tmp_path / "data" / "corpus.sqlite"),
        "MPPKB_CORPUS_DIR": str(corpus),
        "MPPKB_ONTOLOGY": str(REPO / "ontology.yaml"),
        "MPPKB_REVIEWER": "graph-tester",
    }
    values.update(extra)
    return values


def inspect_pdf(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def setup_claim(tmp_path: Path, env: dict[str, str]) -> None:
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    payload = {
        "population": "儿童",
        "severity": None,
        "intervention": "阿奇霉素",
        "attribute": "一线治疗",
        "value": {"kind": "direction", "value": "first_line"},
        "direction": "first_line",
        "evidence_type": "guideline",
        "anchors": [
            {
                "doc_id": info["sha256"][:16],
                "page_start": section["page_start"],
                "page_end": section["page_end"],
                "quote": section["text"].strip()[:24],
            }
        ],
        "confidence": 0.9,
    }
    replay = tmp_path / "replay.json"
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: [payload]}), encoding="utf-8"
    )
    result = run_cli(
        tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    remap = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert remap.returncode == 0, remap.stderr


def graph_run(tmp_path: Path, env: dict[str, str], case: dict, thread_id: str | None = None) -> dict:
    case_file = tmp_path / f"case-{thread_id or 'auto'}.json"
    case_file.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    args = ["graph", "run", "--case-json", str(case_file), "--json"]
    if thread_id:
        args += ["--thread-id", thread_id]
    result = run_cli(tmp_path, *args, env_extra=env)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def graph_resume(tmp_path: Path, env: dict[str, str], thread_id: str, decision: str) -> dict:
    result = run_cli(
        tmp_path,
        "graph",
        "resume",
        "--thread-id",
        thread_id,
        "--decision",
        decision,
        "--json",
        env_extra=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_simple_path_runs_and_writes_audit(tmp_path):
    env = env_for(tmp_path)
    setup_claim(tmp_path, env)
    payload = graph_run(
        tmp_path,
        env,
        {
            "case_id": "case-simple",
            "question": "阿奇霉素 一线治疗",
            "patient_context": {"age_years": 30},
        },
        thread_id="thread-simple",
    )

    assert payload["status"] == "done"
    assert payload["complexity"] == "simple"
    assert "阿奇霉素" in payload["answer"]
    assert payload["citations"][0]["page"] >= 1
    for node in ("intake", "triage", "tools", "retrieve", "conflict_resolve", "synthesize", "verify", "finalize"):
        assert node in payload["audit"]
    assert (tmp_path / "data" / "graph.sqlite").is_file()


def test_refusal_when_no_evidence(tmp_path):
    env = env_for(tmp_path)
    setup_claim(tmp_path, env)
    payload = graph_run(
        tmp_path,
        env,
        {"case_id": "case-refused", "question": "量子纠缠 超导 拓扑", "patient_context": {}},
        thread_id="thread-refused",
    )

    assert payload["status"] == "done"
    assert "没有足够的当前证据" in payload["answer"]
    assert payload["citations"] == []


def test_complex_path_interrupts_then_resumes(tmp_path):
    env = env_for(tmp_path)
    setup_claim(tmp_path, env)
    thread_id = "thread-complex"
    case = {
        "case_id": "case-complex",
        "question": "重症 难治 阿奇霉素 一线治疗 儿童",
        "patient_context": {"age_years": 5, "severity": "severe"},
        "force_gap": True,
    }
    interrupted = graph_run(tmp_path, env, case, thread_id=thread_id)

    assert interrupted["interrupted"] is True
    assert interrupted["status"] == "interrupted"
    assert interrupted["complexity"] == "complex"
    assert "plan" in interrupted["audit"] and "gap_check" in interrupted["audit"]
    assert interrupted["budget"]["gap_retries"] == 2

    resumed = graph_resume(tmp_path, env, thread_id, "approve")
    assert resumed["status"] == "done"
    assert "阿奇霉素" in resumed["answer"]
    assert resumed["citations"]
    assert "human_gate" in resumed["audit"]


def test_budget_exhaustion_stops_retrieval(tmp_path):
    env = env_for(tmp_path)
    setup_claim(tmp_path, env)
    payload = graph_run(
        tmp_path,
        env,
        {
            "case_id": "case-budget",
            "question": "阿奇霉素 一线治疗 儿童",
            "patient_context": {"age_years": 5},
            "budget": {"max_retrievals": 0},
        },
        thread_id="thread-budget",
    )

    assert payload["status"] == "budget_exhausted"
    assert "预算耗尽" in payload["answer"]
    assert "retrieve" in payload["audit"]
    assert "finalize" in payload["audit"]


def test_human_gate_can_reject(tmp_path):
    env = env_for(tmp_path)
    setup_claim(tmp_path, env)
    thread_id = "thread-reject"
    graph_run(
        tmp_path,
        env,
        {
            "case_id": "case-reject",
            "question": "重症 阿奇霉素 一线治疗 儿童",
            "patient_context": {"age_years": 5, "severity": "severe"},
        },
        thread_id=thread_id,
    )

    rejected = graph_resume(tmp_path, env, thread_id, "reject")
    assert rejected["status"] == "rejected"
    assert "拒绝" in rejected["answer"]
