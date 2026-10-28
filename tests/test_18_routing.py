"""Ticket 18 acceptance: deterministic routing and the shared response contract."""

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
    }
    values.update(extra)
    return values


def prepare(tmp_path: Path, env: dict[str, str]) -> None:
    result = run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env)
    info = json.loads(result.stdout)
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
    run_cli(tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env)
    remap = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert remap.returncode == 0, remap.stderr


def ask(tmp_path: Path, env: dict[str, str], query: str) -> dict:
    result = run_cli(tmp_path, "answer", "--query", query, "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_simple_question_uses_simple_path(tmp_path):
    env = env_for(tmp_path)
    prepare(tmp_path, env)

    payload = ask(tmp_path, env, "阿奇霉素 一线治疗 儿童")

    assert payload["path"] == "simple"
    assert payload["routing"] == {"path": "simple", "reasons": []}
    assert payload["budgets"]["retrieval_calls"] >= 1
    assert "disclaimer" in payload


def test_high_risk_question_uses_complex_path(tmp_path):
    env = env_for(tmp_path)
    prepare(tmp_path, env)

    payload = ask(tmp_path, env, "重症 难治 儿童 多药 联合用药 阿奇霉素")

    assert payload["path"] == "complex"
    assert payload["routing"]["reasons"]
    assert any(reason.startswith("risk:") for reason in payload["routing"]["reasons"])


def test_routing_is_deterministic(tmp_path):
    env = env_for(tmp_path)
    prepare(tmp_path, env)

    first = ask(tmp_path, env, "重症 难治 儿童 阿奇霉素")
    second = ask(tmp_path, env, "重症 难治 儿童 阿奇霉素")

    assert first["path"] == second["path"]
    assert first["routing"] == second["routing"]


def test_case_context_can_force_complex_path():
    import sys

    sys.path.insert(0, str(REPO))
    from mppkb.routing import route

    simple = route("阿奇霉素 剂量", {"age_years": 12})
    complex_ = route("阿奇霉素 剂量", {"age_years": 3})
    severe = route("阿奇霉素 剂量", {"severity": "severe"})

    assert simple["path"] == "simple"
    assert complex_["path"] == "complex"
    assert "case:age_under_8" in complex_["reasons"]
    assert severe["path"] == "complex"
