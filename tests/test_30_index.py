"""Ticket 30 acceptance: derived Chroma index, cloud embeddings and fallback."""

from __future__ import annotations

import json
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
        "MPPKB_REVIEWER": "index-tester",
        "MPPKB_EMBEDDING_BACKEND": "fake",
        "MPPKB_CHROMA_DIR": str(tmp_path / "data" / "chroma"),
        "ANONYMIZED_TELEMETRY": "False",
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


def ingest(tmp_path: Path, pdf: Path, env: dict[str, str], payloads: list[dict], tag: str) -> None:
    info = inspect_pdf(tmp_path, pdf, env)
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


def remap(tmp_path: Path, env: dict[str, str]) -> None:
    result = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert result.returncode == 0, result.stderr


def setup(tmp_path: Path, env: dict[str, str]) -> None:
    info = inspect_pdf(tmp_path, MINI, env)
    section = info["sections"][0]
    ingest(tmp_path, MINI, env, [payload_for(info, section)], "cold")

    long_info = inspect_pdf(tmp_path, LONG, env)
    long_section = long_info["sections"][0]
    ingest(
        tmp_path,
        LONG,
        env,
        [
            payload_for(
                long_info,
                long_section,
                intervention="红霉素",
                evidence_type="observational_study",
            )
        ],
        "hot",
    )
    remap(tmp_path, env)


def test_rebuild_search_and_status(tmp_path):
    env = env_for(tmp_path)
    setup(tmp_path, env)

    rebuilt = json.loads(
        run_cli(tmp_path, "index", "rebuild", "--json", env_extra=env).stdout
    )
    assert rebuilt["ok"] is True
    assert rebuilt["counts"]["cold"] == 1
    assert rebuilt["counts"]["hot"] == 1

    status = json.loads(run_cli(tmp_path, "index", "status", "--json", env_extra=env).stdout)
    assert status["stale"] is False
    assert status["counts_indexed"] == {"cold": 1, "hot": 1}

    search = json.loads(
        run_cli(
            tmp_path,
            "index",
            "search",
            "--query",
            "阿奇霉素 一线治疗 儿童",
            "--library",
            "cold",
            "--json",
            env_extra=env,
        ).stdout
    )
    assert search["degraded"] is False
    assert search["items"]
    approved = json.loads(
        run_cli(tmp_path, "claims", "list", "--status", "approved", "--json", env_extra=env).stdout
    )["claims"]
    assert approved[0]["claim_id"] in {item["claim_id"] for item in search["items"]}

    again = json.loads(
        run_cli(tmp_path, "index", "rebuild", "--json", env_extra=env).stdout
    )
    assert again["corpus_hash"] == rebuilt["corpus_hash"]


def test_embedding_failure_degrades_to_gram_scan(tmp_path):
    env = env_for(tmp_path)
    setup(tmp_path, env)
    run_cli(tmp_path, "index", "rebuild", "--json", env_extra=env)

    failing = {**env, "MPPKB_EMBEDDING_BACKEND": "fail"}
    search = json.loads(
        run_cli(
            tmp_path,
            "index",
            "search",
            "--query",
            "阿奇霉素 一线治疗",
            "--library",
            "cold",
            "--json",
            env_extra=failing,
        ).stdout
    )
    assert search["degraded"] is True
    assert search["reason"]
    assert search["items"]
