"""Ticket 09 acceptance: cold-only answers, citations, refusal, hot notes, cache."""

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
    }
    values.update(extra)
    return values


def inspect_pdf(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def make_payload(info: dict, section: dict, **overrides) -> dict:
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
    payload.update(overrides)
    return payload


def ingest(tmp_path: Path, env: dict[str, str], payloads: list[dict]) -> None:
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    replay = tmp_path / "replay.json"
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: payloads}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = run_cli(
        tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert result.returncode == 0, result.stdout + result.stderr


def remap(tmp_path: Path, env: dict[str, str]) -> None:
    result = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert result.returncode == 0, result.stderr


def ask(tmp_path: Path, env: dict[str, str], query: str) -> dict:
    result = run_cli(tmp_path, "answer", "--query", query, "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_answer_uses_cold_claims_with_citations(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(tmp_path, env, [make_payload(info, section)])
    remap(tmp_path, env)

    payload = ask(tmp_path, env, "阿奇霉素 一线治疗 儿童")

    assert payload["ok"] is True
    assert payload["refused"] is False
    assert payload["evidence_status"] == "current"
    assert payload["sections"]
    assert payload["citations"]
    assert all(citation["page"] >= 1 for citation in payload["citations"])
    assert payload["claims"]
    assert payload["disclaimer"]


def test_unrelated_question_is_refused(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(tmp_path, env, [make_payload(info, section)])
    remap(tmp_path, env)

    payload = ask(tmp_path, env, "量子纠缠 超导材料 天气")

    assert payload["ok"] is True
    assert payload["refused"] is True
    assert payload["sections"] == []
    assert "证据" in payload["message"]


def test_answer_exposes_evidence_status_and_route_budget(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(
        tmp_path,
        env,
        [
            make_payload(info, section),
            make_payload(
                info,
                section,
                attribute="剂量",
                value={"kind": "numeric", "value": 10, "unit": "mg/kg/d"},
                direction=None,
                evidence_type="observational_study",
            ),
        ],
    )
    remap(tmp_path, env)

    off = ask(tmp_path, env, "阿奇霉素 剂量 疗程 儿童")
    assert off["evidence_status_block"]["summary"].startswith("知识库 v")
    assert off["evidence_status_block"]["snapshot_label"].startswith("快照 ")
    assert off["budgets"]["retrieval_calls"] == 1
    assert off["citation_check"]["coverage"] == 1.0
    assert off["citation_check"]["sentences"] >= 1
    assert "gap_details" in off["evidence_status_block"]
    assert "gap_fills" in off
    assert off["evidence_status_block"]["gap_fills"] == off["gap_fills"]

    on_env = dict(env, MPPKB_ORCHESTRATION_V2="1")
    on = ask(tmp_path, on_env, "阿奇霉素 剂量 疗程 儿童")
    assert on["evidence_status_block"]["summary"] == off["evidence_status_block"]["summary"]
    assert on["budgets"]["retrieval_calls"] == 1
    assert on["budgets"]["gap_retries"] == 0
    assert on["planned_expansion"] is True
    assert on["orchestration_v2"] is True

    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(
        tmp_path,
        env,
        [
            make_payload(info, section),
            make_payload(
                info,
                section,
                attribute="剂量",
                value={"kind": "numeric", "value": 10, "unit": "mg/kg/d"},
                direction=None,
                evidence_type="observational_study",
            ),
        ],
    )
    remap(tmp_path, env)

    payload = ask(tmp_path, env, "阿奇霉素 剂量 儿童")

    assert payload["refused"] is False
    assert payload["note"]
    assert all("未审核" in note["text"] for note in payload["note"])
    body = " ".join(section["text"] for section in payload["sections"])
    assert "未审核" not in body


def test_cache_hits_then_invalidates_on_promotion(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(tmp_path, env, [make_payload(info, section)])
    remap(tmp_path, env)

    first = ask(tmp_path, env, "阿奇霉素 一线治疗 儿童")
    second = ask(tmp_path, env, "阿奇霉素 一线治疗 儿童")
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True

    long_info = run_cli(tmp_path, "inspect", str(LONG), "--json", env_extra=env)
    long_payload = json.loads(long_info.stdout)
    long_section = long_payload["sections"][0]
    replay = tmp_path / "replay-long.json"
    replay.write_text(
        json.dumps(
            {
                "__default__": [],
                long_section["chunk_hash"]: [
                    make_payload(long_payload, long_section, intervention="红霉素")
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ingested = run_cli(
        tmp_path, "ingest", str(LONG), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert ingested.returncode == 0, ingested.stdout + ingested.stderr
    remap(tmp_path, env)

    third = ask(tmp_path, env, "阿奇霉素 一线治疗 儿童")
    assert third["cache_hit"] is False


def test_superseded_evidence_is_labelled(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(tmp_path, env, [make_payload(info, section)])
    remap(tmp_path, env)

    connection = sqlite3.connect(env["MPPKB_KB_DB"])
    connection.execute("UPDATE claim_versions SET status = 'superseded' WHERE status = 'approved'")
    connection.commit()
    connection.close()

    payload = ask(tmp_path, env, "阿奇霉素 一线治疗 儿童")

    assert payload["evidence_status"] == "superseded"
    assert payload["sections"]
    assert "已过期" in payload["sections"][0]["text"]
