"""Ticket 19 acceptance: task-graph planner, gap retry and degradation."""

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
        "MPPKB_REVIEWER": "complex-tester",
    }
    values.update(extra)
    return values


def inspect_pdf(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def payload_for(info: dict, section: dict, **overrides) -> dict:
    payload = {
        "population": "儿童",
        "severity": None,
        "intervention": "阿奇霉素",
        "attribute": "剂量",
        "value": {"kind": "numeric", "value": 10, "unit": "mg/kg/day"},
        "direction": None,
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
    payload.update(overrides)
    return payload


def seed_dose_claim(tmp_path: Path, env: dict[str, str]) -> None:
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    replay = tmp_path / "replay-dose.json"
    replay.write_text(
        json.dumps(
            {"__default__": [], section["chunk_hash"]: [payload_for(info, section)]},
            ensure_ascii=False,
        ),
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


def test_gap_retry_fills_and_trace_is_exposed(tmp_path):
    env = env_for(tmp_path)
    seed_dose_claim(tmp_path, env)
    payload = graph_run(
        tmp_path,
        env,
        {
            "case_id": "case-gap-fill",
            "question": "重症 阿奇霉素 儿童 剂量",
            "patient_context": {
                "drug": "阿奇霉素",
                "weight_kg": 20,
                "age_years": 30,
            },
        },
        "thread-gap-fill",
    )

    assert payload["status"] == "done"
    assert payload["complexity"] == "complex"
    assert payload["plan"]
    assert payload["gap_retries"] == 1
    assert payload["gaps"]
    dose_tasks = [item for item in payload["plan"] if item["tool"] == "dose_calculator"]
    assert len(dose_tasks) == 2
    assert dose_tasks[0]["status"] == "done"
    assert payload["degraded"] is False
    assert payload["answer"]
    assert payload["tools"]


def test_continuous_gap_degrades_after_two_retries(tmp_path):
    env = env_for(tmp_path)
    seed_dose_claim(tmp_path, env)
    payload = graph_run(
        tmp_path,
        env,
        {
            "case_id": "case-gap-degrade",
            "question": "重症 阿奇霉素 儿童 剂量",
            "patient_context": {
                "drug": "红霉素",
                "weight_kg": 20,
                "age_years": 30,
            },
        },
        "thread-gap-degrade",
    )

    assert payload["status"] == "done"
    assert payload["gap_retries"] == 2
    assert payload["degraded"] is True
    assert "证据不足" in payload["answer"]
    assert "工具缺口" in payload["answer"]


def test_tool_error_is_retried_and_degraded(tmp_path):
    env = env_for(tmp_path)
    payload = graph_run(
        tmp_path,
        env,
        {
            "case_id": "case-tool-error",
            "question": "重症 左氧氟沙星 儿童",
            "patient_context": {"drug": "左氧氟沙星", "age_years": "unknown"},
        },
        "thread-tool-error",
    )

    assert payload["status"] == "done"
    p0_tasks = [item for item in payload["plan"] if item["tool"] == "p0_check"]
    assert p0_tasks
    p0_tools = [item for item in payload["tools"] if item["tool"] == "p0_check"]
    assert p0_tools and all(item["status"] == "error" for item in p0_tools)
    assert payload["degraded"] is True
    assert payload["gap_retries"] == 2


def test_simple_case_is_not_routed_into_complex_plan(tmp_path):
    env = env_for(tmp_path)
    payload = graph_run(
        tmp_path,
        env,
        {"case_id": "case-simple-19", "question": "阿奇霉素 一线治疗", "patient_context": {}},
        "thread-simple-19",
    )
    assert payload["complexity"] == "simple"
    assert payload["plan"] == []
    assert payload["tools"] == []
    assert payload["gap_retries"] == 0
