"""Ticket 20 acceptance: constrained synthesis, attribution and citation repair."""

from __future__ import annotations

import json
from pathlib import Path

from support import run_cli

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINI = FIXTURES / "mini_two_pages.pdf"

FACTS = [
    {
        "claim_id": "claim-1",
        "text": "阿奇霉素一线治疗：首选",
        "anchors": [{"doc_id": "doc-1", "page_start": 3, "page_end": 3, "quote": "轻症首选阿奇霉素"}],
    },
    {
        "claim_id": "claim-2",
        "text": "阿奇霉素剂量：10 mg/kg/day",
        "anchors": [{"doc_id": "doc-2", "page_start": 4, "page_end": 4, "quote": "10 mg/kg"}],
    },
]


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    values = {
        "MPPKB_KB_DB": str(tmp_path / "data" / "kb.sqlite"),
        "MPPKB_CORPUS_DB": str(tmp_path / "data" / "corpus.sqlite"),
        "MPPKB_CORPUS_DIR": str(corpus),
        "MPPKB_ONTOLOGY": str(REPO / "ontology.yaml"),
        "MPPKB_REVIEWER": "synth-tester",
    }
    values.update(extra)
    return values


def write_json(path: Path, payload) -> str:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_synth_run_attribution_and_metadata(tmp_path):
    env = env_for(tmp_path)
    facts = write_json(tmp_path / "facts.json", FACTS)
    first = run_cli(tmp_path, "synth", "run", "--facts", facts, "--json", env_extra=env)
    second = run_cli(tmp_path, "synth", "run", "--facts", facts, "--json", env_extra=env)
    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout

    payload = json.loads(first.stdout)
    assert payload["backend"] == "rule"
    assert payload["attribution"][0]["claim_id"] == "claim-1"
    assert "[Source[1]]" in payload["answer"]
    assert "[Source[2]]" in payload["answer"]
    assert payload["meta"]["temperature"] == 0.0
    assert payload["meta"]["compile_version"]
    assert payload["meta"]["prompt_hash"]
    assert payload["defects"] == []


def test_synth_check_repairs_wrong_source_and_drops_unmapped(tmp_path):
    env = env_for(tmp_path)
    facts = write_json(tmp_path / "facts.json", FACTS)
    sentences = write_json(
        tmp_path / "sentences.json",
        [
            {"text": "阿奇霉素一线治疗：首选[Source[9]]", "claim_id": "claim-1"},
            {"text": "没有出处的一句话", "claim_id": "missing-claim"},
        ],
    )
    result = run_cli(
        tmp_path,
        "synth",
        "check",
        "--facts",
        facts,
        "--sentences",
        sentences,
        "--json",
        env_extra=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["sentences"][0]["text"].endswith("[Source[1]]")
    reasons = {item["reason"] for item in payload["defects"]}
    assert "wrong_source_marker" in reasons
    assert "unmapped_claim" in reasons
    assert any(item["action"] == "dropped" for item in payload["defects"])


def test_answer_has_attribution_and_synthesis_meta(tmp_path):
    env = env_for(tmp_path)
    info = json.loads(run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env).stdout)
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
    }
    replay = tmp_path / "replay.json"
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: [payload]}, ensure_ascii=False),
        encoding="utf-8",
    )
    ingest = run_cli(
        tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert ingest.returncode == 0, ingest.stdout + ingest.stderr
    run_cli(tmp_path, "remap", "--json", env_extra=env)

    answer = run_cli(
        tmp_path, "answer", "--query", "阿奇霉素 一线治疗 儿童", "--json", env_extra=env
    )
    parsed = json.loads(answer.stdout)
    assert parsed["attribution"]
    assert parsed["synthesis"]["backend"] == "rule"
    assert parsed["synthesis"]["temperature"] == 0.0
    assert parsed["synthesis"]["sentences"] == len(parsed["attribution"])
    citation_ids = {item["source_no"] for item in parsed["citations"]}
    assert all(item["source_no"] in citation_ids for item in parsed["attribution"])


def test_graph_synthesis_meta_and_attribution(tmp_path):
    env = env_for(tmp_path)
    info = json.loads(run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env).stdout)
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
    }
    replay = tmp_path / "replay-graph.json"
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: [payload]}, ensure_ascii=False),
        encoding="utf-8",
    )
    ingest = run_cli(
        tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert ingest.returncode == 0, ingest.stdout + ingest.stderr
    run_cli(tmp_path, "remap", "--json", env_extra=env)

    case = tmp_path / "case.json"
    case.write_text(
        json.dumps(
            {
                "case_id": "case-synth",
                "question": "重症 阿奇霉素 一线治疗 儿童",
                "patient_context": {"age_years": 5, "drug": "阿奇霉素"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = run_cli(
        tmp_path,
        "graph",
        "run",
        "--case-json",
        str(case),
        "--thread-id",
        "thread-synth",
        "--json",
        env_extra=env,
    )
    parsed = json.loads(result.stdout)
    assert parsed["synthesis"]["backend"] == "rule"
    assert parsed["synthesis"]["temperature"] == 0.0
    assert parsed["attribution"]
    assert parsed["synthesis"]["sentences"] == len(parsed["attribution"])
    assert parsed["synthesis_defects"] == []
