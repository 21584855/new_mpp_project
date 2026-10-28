"""Ticket 22 acceptance: deterministic structured evaluation export."""

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
        "confidence": 0.9,
    }
    replay = tmp_path / "replay.json"
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: [payload]}), encoding="utf-8"
    )
    run_cli(tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env)
    remap = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert remap.returncode == 0, remap.stderr


def export(tmp_path: Path, env: dict[str, str], name: str) -> dict:
    out = tmp_path / name
    result = run_cli(tmp_path, "export", "--out", str(out), "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_export_contains_structured_fields(tmp_path):
    env = env_for(tmp_path)
    prepare(tmp_path, env)
    run_cli(tmp_path, "answer", "--query", "阿奇霉素 一线治疗 儿童", "--json", env_extra=env)
    run_cli(tmp_path, "answer", "--query", "量子纠缠 超导", "--json", env_extra=env)

    payload = export(tmp_path, env, "eval.json")

    assert payload["ok"] is True
    assert payload["count"] == 2
    record = payload["records"][0]
    for field in ("path", "routing", "claims", "citations", "sections", "budgets", "model"):
        assert field in record
    assert record["library_version"] >= 1


def test_export_is_deterministic(tmp_path):
    env = env_for(tmp_path)
    prepare(tmp_path, env)
    run_cli(tmp_path, "answer", "--query", "阿奇霉素 一线治疗 儿童", "--json", env_extra=env)

    first = export(tmp_path, env, "a.json")
    second = export(tmp_path, env, "b.json")

    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()
    assert first == second


def test_empty_export_is_valid(tmp_path):
    env = env_for(tmp_path)

    payload = export(tmp_path, env, "empty.json")

    assert payload["ok"] is True
    assert payload["count"] == 0
    assert payload["records"] == []
