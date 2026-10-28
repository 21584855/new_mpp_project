"""Ticket 28 acceptance: retrieval cache replaces answer cache; answer log feeds export."""

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
        "MPPKB_REVIEWER": "cache-tester",
    }
    values.update(extra)
    return values


def inspect_pdf(tmp_path: Path, pdf: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "inspect", str(pdf), "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def payload_for(info: dict, section: dict, **overrides) -> dict:
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


def ingest(tmp_path: Path, pdf: Path, env: dict[str, str], items: list[dict]) -> None:
    info = inspect_pdf(tmp_path, pdf, env)
    section = info["sections"][0]
    replay = tmp_path / f"replay-{pdf.stem}.json"
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: items}), encoding="utf-8"
    )
    result = run_cli(
        tmp_path, "ingest", str(pdf), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert result.returncode == 0, result.stdout + result.stderr


def remap(tmp_path: Path, env: dict[str, str]) -> None:
    result = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert result.returncode == 0, result.stderr


def ask(tmp_path: Path, env: dict[str, str], query: str = "阿奇霉素 一线治疗 儿童") -> dict:
    result = run_cli(tmp_path, "answer", "--query", query, "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def count_rows(db_path: str, table: str) -> int:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


def test_retrieval_cache_and_answer_log(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    section = info["sections"][0]
    ingest(tmp_path, MINI, env, [payload_for(info, section)])
    remap(tmp_path, env)

    first = ask(tmp_path, env)
    second = ask(tmp_path, env)

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert count_rows(env["MPPKB_KB_DB"], "retrieval_cache") == 1
    assert count_rows(env["MPPKB_KB_DB"], "answer_cache") == 0
    assert count_rows(env["MPPKB_KB_DB"], "answer_log") == 2

    connection = sqlite3.connect(env["MPPKB_KB_DB"])
    row = connection.execute(
        "SELECT cache_type, value_json, hit_count FROM retrieval_cache"
    ).fetchone()
    connection.close()
    assert row[0] == "retrieval"
    assert row[2] >= 1
    cached = json.loads(row[1])
    assert "version_ids" in cached
    assert "sections" not in cached and "claims" not in cached


def test_promotion_invalidates_retrieval_cache(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    section = info["sections"][0]
    ingest(tmp_path, MINI, env, [payload_for(info, section)])
    remap(tmp_path, env)
    assert ask(tmp_path, env)["cache_hit"] is False

    long_info = inspect_pdf(tmp_path, LONG, env)
    long_section = long_info["sections"][0]
    ingest(
        tmp_path,
        LONG,
        env,
        [payload_for(long_info, long_section, intervention="红霉素")],
    )
    remap(tmp_path, env)

    after = ask(tmp_path, env)
    assert after["cache_hit"] is False
    assert count_rows(env["MPPKB_KB_DB"], "retrieval_cache") == 1


def test_export_reads_answer_log(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    section = info["sections"][0]
    ingest(tmp_path, MINI, env, [payload_for(info, section)])
    remap(tmp_path, env)
    ask(tmp_path, env)
    ask(tmp_path, env, "量子纠缠 超导")

    out = tmp_path / "eval.json"
    result = run_cli(tmp_path, "export", "--out", str(out), "--json", env_extra=env)
    assert result.returncode == 0, result.stderr

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["count"] == count_rows(env["MPPKB_KB_DB"], "answer_log") == 2
    record = payload["records"][0]
    assert record["question_hash"]
    assert "snapshot_id" in record
    assert "query" not in record
