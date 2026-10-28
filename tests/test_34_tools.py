"""Ticket 34 acceptance: deterministic tools and LangGraph tool calls."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from support import run_cli

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINI = FIXTURES / "mini_two_pages.pdf"
LONG = FIXTURES / "long_section.pdf"


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    values = {
        "MPPKB_KB_DB": str(tmp_path / "data" / "kb.sqlite"),
        "MPPKB_CORPUS_DB": str(tmp_path / "data" / "corpus.sqlite"),
        "MPPKB_CORPUS_DIR": str(corpus),
        "MPPKB_ONTOLOGY": str(REPO / "ontology.yaml"),
        "MPPKB_REVIEWER": "tool-tester",
    }
    values.update(extra)
    return values


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


def ingest(tmp_path: Path, pdf: Path, env: dict[str, str], payloads: list[dict], tag: str) -> dict:
    info = json.loads(run_cli(tmp_path, "inspect", str(pdf), "--json", env_extra=env).stdout)
    section = info["sections"][0]
    replay = tmp_path / f"replay-{tag}.json"
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: payloads}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = run_cli(
        tmp_path, "ingest", str(pdf), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return info


def tool(tmp_path: Path, env: dict[str, str], name: str, payload: dict) -> dict:
    result = run_cli(
        tmp_path,
        "tools",
        "run",
        "--name",
        name,
        "--input-json",
        json.dumps(payload, ensure_ascii=False),
        "--json",
        env_extra=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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


def test_tools_list_and_p0(tmp_path):
    env = env_for(tmp_path)
    run_cli(tmp_path, "p0", "list", "--json", env_extra=env)

    listed = json.loads(run_cli(tmp_path, "tools", "list", "--json", env_extra=env).stdout)
    assert {"dose_calculator", "p0_check", "lab_threshold", "guideline_search"} <= set(
        listed["tools"]
    )

    hit = tool(tmp_path, env, "p0_check", {"drug": "左氧氟沙星", "age_years": 10})
    assert hit["status"] == "ok" and hit["data"]["hit"] is True
    missed = tool(tmp_path, env, "p0_check", {"drug": "左氧氟沙星", "age_years": 30})
    assert missed["status"] == "ok" and missed["data"]["hit"] is False


def test_dose_calculator(tmp_path):
    env = env_for(tmp_path)
    info = json.loads(run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env).stdout)
    section = info["sections"][0]
    ingest(tmp_path, MINI, env, [payload_for(info, section)], "dose")
    run_cli(tmp_path, "remap", "--json", env_extra=env)

    result = tool(
        tmp_path, env, "dose_calculator", {"drug": "阿奇霉素", "weight_kg": 20, "age_years": 5}
    )
    assert result["status"] == "ok"
    assert result["data"]["calculation"]["value"] == 200.0

    missing = tool(
        tmp_path, env, "dose_calculator", {"drug": "红霉素", "weight_kg": 20}
    )
    assert missing["status"] == "not_found"


def test_lab_threshold_and_guideline_search(tmp_path):
    env = env_for(tmp_path)
    info = json.loads(run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env).stdout)
    section = info["sections"][0]
    lab = payload_for(
        info,
        section,
        attribute="血糖",
        value={"kind": "numeric", "value": 6.5, "unit": "mmol/L"},
    )
    ingest(tmp_path, MINI, env, [lab], "lab")
    remapped = run_cli(tmp_path, "remap", "--include-labs", "--json", env_extra=env)
    assert remapped.returncode == 0, remapped.stderr

    threshold = tool(tmp_path, env, "lab_threshold", {"marker": "血糖"})
    assert threshold["status"] == "ok"
    assert threshold["data"]["thresholds"][0]["value"]["value"] == 6.5

    no_crp = tool(tmp_path, env, "lab_threshold", {"marker": "C反应蛋白"})
    assert no_crp["status"] == "not_found"

    search = tool(tmp_path, env, "guideline_search", {"query": "阿奇霉素", "top_k": 3})
    assert search["status"] == "ok"
    assert search["data"]["passages"][0]["page_start"] >= 1


def test_graph_complex_calls_tools_and_audits(tmp_path):
    env = env_for(tmp_path)
    run_cli(tmp_path, "p0", "list", "--json", env_extra=env)
    payload = graph_run(
        tmp_path,
        env,
        {
            "case_id": "case-tools",
            "question": "重症 左氧氟沙星 儿童",
            "patient_context": {
                "age_years": 10,
                "drug": "左氧氟沙星",
                "weight_kg": 20,
                "severity": "severe",
            },
        },
        "thread-tools",
    )
    assert payload["interrupted"] is True
    names = {item["tool"] for item in payload["tools"]}
    assert {"p0_check", "dose_calculator", "guideline_search"} <= names
    assert all(item["status"] in ("ok", "not_found") for item in payload["tools"])

    connection = sqlite3.connect(env["MPPKB_KB_DB"])
    count = connection.execute(
        "SELECT COUNT(*) FROM kb_audit WHERE action = 'tool_call'"
    ).fetchone()[0]
    connection.close()
    assert count >= 3

    exhausted = graph_run(
        tmp_path,
        env,
        {
            "case_id": "case-budget",
            "question": "重症 左氧氟沙星 儿童",
            "patient_context": {"age_years": 10, "drug": "左氧氟沙星", "weight_kg": 20},
            "budget": {"max_tool_calls": 1},
        },
        "thread-budget",
    )
    assert exhausted["status"] == "budget_exhausted"
    assert len(exhausted["tools"]) == 1


def test_graph_simple_has_no_tools(tmp_path):
    env = env_for(tmp_path)
    payload = graph_run(
        tmp_path,
        env,
        {"case_id": "case-simple", "question": "阿奇霉素 一线治疗", "patient_context": {}},
        "thread-simple-tools",
    )
    assert payload["tools"] == []
