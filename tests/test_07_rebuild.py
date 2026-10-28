"""Ticket 07 acceptance: ledger replay and rebuild verification."""

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
        "MPPKB_REVIEWER": "tester",
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


def remap(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def verify(tmp_path: Path, env: dict[str, str]):
    result = run_cli(tmp_path, "rebuild", "--verify", "--json", env_extra=env)
    return result


def test_empty_ledger_verifies(tmp_path):
    env = env_for(tmp_path)

    result = verify(tmp_path, env)

    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["versions"] == 0
    assert payload["library_version"] == 0


def test_verification_matches_derived_state(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(
        tmp_path,
        env,
        [
            make_payload(info, section),
            make_payload(info, section, intervention="红霉素", evidence_type="observational_study"),
        ],
    )
    remap(tmp_path, env)
    queue = json.loads(
        run_cli(tmp_path, "review", "list", "--json", env_extra=env).stdout
    )["items"]
    assert len(queue) == 1
    approved = run_cli(
        tmp_path, "review", "approve", queue[0]["claim_id"], "--json", env_extra=env
    )
    assert approved.returncode == 0, approved.stderr

    result = verify(tmp_path, env)

    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["versions"] == 2
    assert payload["library_version"] == 2

    again = verify(tmp_path, env)
    assert again.returncode == 0
    assert json.loads(again.stdout) == payload


def test_tampered_status_is_detected(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(tmp_path, env, [make_payload(info, section)])
    remap(tmp_path, env)

    connection = sqlite3.connect(env["MPPKB_KB_DB"])
    connection.execute(
        "UPDATE claim_versions SET status = 'rejected' WHERE status = 'approved'"
    )
    connection.commit()
    connection.close()

    result = verify(tmp_path, env)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert any(mismatch["field"] == "status" for mismatch in payload["mismatches"])


def test_tampered_library_version_is_detected(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(tmp_path, env, [make_payload(info, section)])
    remap(tmp_path, env)

    connection = sqlite3.connect(env["MPPKB_KB_DB"])
    connection.execute("UPDATE meta SET value = '99' WHERE key = 'library_version'")
    connection.commit()
    connection.close()

    result = verify(tmp_path, env)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert any(mismatch["field"] == "library_version" for mismatch in payload["mismatches"])


def test_same_value_anchor_merge_keeps_approval_and_verifies(tmp_path):
    env = env_for(tmp_path)
    mini_info = inspect_pdf(tmp_path, env)
    mini_section = mini_info["sections"][0]
    ingest(tmp_path, env, [make_payload(mini_info, mini_section)])
    remap(tmp_path, env)

    long_pdf = FIXTURES / "long_section.pdf"
    long_info = json.loads(
        run_cli(tmp_path, "inspect", str(long_pdf), "--json", env_extra=env).stdout
    )
    long_section = long_info["sections"][0]
    replay = tmp_path / "replay-long.json"
    replay.write_text(
        json.dumps(
            {"__default__": [], long_section["chunk_hash"]: [make_payload(long_info, long_section)]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ingested = run_cli(
        tmp_path, "ingest", str(long_pdf), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert ingested.returncode == 0, ingested.stdout + ingested.stderr

    report = remap(tmp_path, env)
    assert report["anchors_merged"] == 1

    approved = json.loads(
        run_cli(tmp_path, "claims", "list", "--status", "approved", "--json", env_extra=env).stdout
    )["claims"]
    assert len(approved) == 1
    assert len(approved[0]["anchors"]) == 2

    result = verify(tmp_path, env)
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["ok"] is True
