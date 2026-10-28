"""Ticket 21 acceptance: case-level human gate, degradation and case audit."""

from __future__ import annotations

import json
import sqlite3
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
        "MPPKB_REVIEWER": "gate-reviewer",
    }
    values.update(extra)
    return values


def seed_p0_claim(tmp_path: Path, env: dict[str, str]) -> None:
    info = json.loads(run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env).stdout)
    section = info["sections"][0]
    payload = {
        "population": "儿童",
        "severity": None,
        "intervention": "左氧氟沙星",
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
    }
    replay = tmp_path / "replay-p0.json"
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: [payload]}, ensure_ascii=False),
        encoding="utf-8",
    )
    ingest = run_cli(
        tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert ingest.returncode == 0, ingest.stdout + ingest.stderr
    remap = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert remap.returncode == 0, remap.stderr


def graph_run(tmp_path: Path, env: dict[str, str], case: dict, thread_id: str) -> dict:
    case_file = tmp_path / f"{thread_id}.json"
    case_file.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    result = run_cli(
        tmp_path,
        "graph",
        "run",
        "--case-json",
        str(case_file),
        "--thread-id",
        thread_id,
        "--json",
        env_extra=env,
    )
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


def case_audit_rows(env: dict[str, str]) -> list[dict]:
    connection = sqlite3.connect(env["MPPKB_KB_DB"])
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT case_id, decision, actor, detail_json FROM case_audit ORDER BY seq"
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]


def p0_case() -> dict:
    return {
        "case_id": "case-p0-gate",
        "question": "重症 左氧氟沙星 儿童 一线治疗",
        "patient_context": {"age_years": 5, "severity": "severe", "drug": "左氧氟沙星"},
    }


def test_p0_warning_precedes_gate_and_reject_degrades(tmp_path):
    env = env_for(tmp_path)
    seed_p0_claim(tmp_path, env)
    interrupted = graph_run(tmp_path, env, p0_case(), "thread-gate-reject")

    assert interrupted["interrupted"] is True
    assert interrupted["p0_warning"]["template"]
    assert any("安全警示" in json.dumps(item, ensure_ascii=False) for item in interrupted["interrupt"])

    todos = json.loads(run_cli(tmp_path, "todos", "--json", env_extra=env).stdout)
    assert todos["case_gate"] == 1

    rejected = graph_resume(tmp_path, env, "thread-gate-reject", "reject")
    assert rejected["status"] == "rejected"
    assert rejected["gate_decision"] == "rejected"
    assert "安全警示" in rejected["answer"]
    assert "未签核" in rejected["answer"]
    assert "首选" not in rejected["answer"]
    assert rejected["citations"] == []

    todos = json.loads(run_cli(tmp_path, "todos", "--json", env_extra=env).stdout)
    assert todos["case_gate"] == 0
    rows = case_audit_rows(env)
    assert rows[0]["decision"] == "rejected"
    assert rows[0]["actor"] == "gate-reviewer"


def test_gate_approve_keeps_warning_and_recommendation(tmp_path):
    env = env_for(tmp_path)
    seed_p0_claim(tmp_path, env)
    graph_run(tmp_path, env, p0_case(), "thread-gate-approve")
    approved = graph_resume(tmp_path, env, "thread-gate-approve", "approve")

    assert approved["status"] == "done"
    assert approved["gate_decision"] == "approved"
    assert "安全警示" in approved["answer"]
    assert "首选" in approved["answer"]
    assert approved["citations"]
    rows = case_audit_rows(env)
    assert rows[0]["decision"] == "approved"


def test_gate_timeout_degrades(tmp_path):
    env = env_for(tmp_path)
    seed_p0_claim(tmp_path, env)
    graph_run(tmp_path, env, p0_case(), "thread-gate-timeout")
    timed_out = graph_resume(tmp_path, env, "thread-gate-timeout", "timeout")

    assert timed_out["status"] == "timeout"
    assert "超时" in timed_out["answer"]
    assert "未签核" in timed_out["answer"]
    assert "首选" not in timed_out["answer"]


def test_non_high_risk_case_does_not_enter_gate(tmp_path):
    env = env_for(tmp_path)
    payload = graph_run(
        tmp_path,
        env,
        {"case_id": "case-normal", "question": "阿奇霉素 一线治疗", "patient_context": {}},
        "thread-normal",
    )
    assert payload["interrupted"] is False
    assert payload["gate_decision"] is None
    rows = case_audit_rows(env)
    assert rows and rows[0]["decision"] == "auto"


def test_case_audit_is_separate_from_kb_audit(tmp_path):
    env = env_for(tmp_path)
    seed_p0_claim(tmp_path, env)
    graph_run(tmp_path, env, p0_case(), "thread-audit")
    graph_resume(tmp_path, env, "thread-audit", "approve")
    connection = sqlite3.connect(env["MPPKB_KB_DB"])
    case_count = connection.execute("SELECT COUNT(*) FROM case_audit").fetchone()[0]
    kb_actions = {
        row[0]
        for row in connection.execute("SELECT DISTINCT action FROM kb_audit").fetchall()
    }
    connection.close()
    assert case_count >= 1
    assert "graph_node" in kb_actions
    assert "tool_call" in kb_actions
