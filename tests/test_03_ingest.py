"""Ticket 03 acceptance: extraction port, ingest pipeline, hot-store candidates."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from support import run_cli

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINI = FIXTURES / "mini_two_pages.pdf"
LONG = FIXTURES / "long_section.pdf"
BROKEN = FIXTURES / "not_a_pdf.pdf"


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    values = {
        "MPPKB_KB_DB": str(tmp_path / "data" / "kb.sqlite"),
        "MPPKB_CORPUS_DB": str(tmp_path / "data" / "corpus.sqlite"),
        "MPPKB_CORPUS_DIR": str(corpus),
    }
    values.update(extra)
    return values


def inspect_pdf(tmp_path: Path, pdf: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "inspect", str(pdf), "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def make_payload(info: dict, section: dict, **overrides) -> dict:
    text = section["text"].strip()
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
                "quote": text[:24],
            }
        ],
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


def write_replay(tmp_path: Path, mapping: dict) -> Path:
    path = tmp_path / "replay.json"
    path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return path


def ingest(tmp_path: Path, target: Path, replay: Path, env: dict[str, str], *extra: str):
    return run_cli(
        tmp_path,
        "ingest",
        str(target),
        "--replay-file",
        str(replay),
        "--json",
        *extra,
        env_extra=env,
    )


def test_ingest_creates_candidates_with_anchors(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    replay = write_replay(
        tmp_path, {section["chunk_hash"]: [make_payload(info, section)] for section in info["sections"]}
    )

    result = ingest(tmp_path, MINI, replay, env)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["papers"] == 1
    assert report["candidates"] == len(info["sections"])
    assert report["rejected"] == 0
    assert report["batch"]["cap"] == 200

    listed = json.loads(
        run_cli(tmp_path, "candidates", "list", "--json", env_extra=env).stdout
    )
    assert len(listed["candidates"]) == len(info["sections"])
    anchor = listed["candidates"][0]["payload"]["anchors"][0]
    assert anchor["page_start"] >= 1
    assert anchor["quote"]
    assert anchor["doc_id"] == info["sha256"][:16]


def test_ingest_is_idempotent(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    replay = write_replay(
        tmp_path, {section["chunk_hash"]: [make_payload(info, section)] for section in info["sections"]}
    )

    first = ingest(tmp_path, MINI, replay, env)
    second = ingest(tmp_path, MINI, replay, env)

    assert first.returncode == 0 and second.returncode == 0
    second_report = json.loads(second.stdout)
    assert second_report["candidates"] == 0
    assert second_report["skipped"] == len(info["sections"])
    listed = json.loads(
        run_cli(tmp_path, "candidates", "list", "--status", "proposed", "--json", env_extra=env).stdout
    )
    assert len(listed["candidates"]) == len(info["sections"])


def test_invalid_payload_is_rejected_with_reason(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    sections = info["sections"]
    broken = make_payload(info, sections[0])
    broken.pop("attribute")
    replay = write_replay(
        tmp_path,
        {
            "__default__": [],
            sections[0]["chunk_hash"]: [broken],
            sections[1]["chunk_hash"]: [make_payload(info, sections[1])],
        },
    )

    result = ingest(tmp_path, MINI, replay, env)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["rejected"] == 1
    assert report["candidates"] == 1
    assert report["rejections"][0]["reason"] == "invalid_schema"

    rejected = json.loads(
        run_cli(tmp_path, "candidates", "list", "--status", "rejected", "--json", env_extra=env).stdout
    )
    assert len(rejected["candidates"]) == 1
    assert rejected["candidates"][0]["reason"] == "invalid_schema"


def test_quote_not_found_is_rejected(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    section = info["sections"][0]
    payload = make_payload(info, section)
    payload["anchors"][0]["quote"] = "绝不存在的原文XYZ"
    replay = write_replay(
        tmp_path, {"__default__": [], section["chunk_hash"]: [payload]}
    )

    result = ingest(tmp_path, MINI, replay, env)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["rejected"] == 1
    assert report["rejections"][0]["reason"] == "quote_not_found"


def test_report_counts_match_candidate_list(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    replay = write_replay(
        tmp_path, {section["chunk_hash"]: [make_payload(info, section)] for section in info["sections"]}
    )

    report = json.loads(ingest(tmp_path, MINI, replay, env).stdout)
    listed = json.loads(
        run_cli(tmp_path, "candidates", "list", "--status", "proposed", "--json", env_extra=env).stdout
    )

    assert report["candidates"] == len(listed["candidates"])


def test_batch_cap_is_enforced_explicitly(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    section = info["sections"][0]
    many = [
        make_payload(info, section, confidence=index / 200.0)
        for index in range(201)
    ]
    replay = write_replay(tmp_path, {section["chunk_hash"]: many})

    result = ingest(tmp_path, MINI, replay, env)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert "batch" in report["error"]
    assert report["batch"]["over_cap"] is True


def test_invalid_payload_retries_once(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    sections = info["sections"]
    broken = make_payload(info, sections[0])
    broken.pop("attribute")
    replay = write_replay(
        tmp_path, {"__default__": [], sections[0]["chunk_hash"]: [broken]}
    )

    result = ingest(tmp_path, MINI, replay, env)

    report = json.loads(result.stdout)
    assert report["rejected"] == 1
    # one extra extraction attempt for the invalid section
    assert report["calls"] == len(sections) + 1


def test_malformed_response_reason_is_reported(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    section = info["sections"][0]
    replay = write_replay(
        tmp_path, {"__default__": [], section["chunk_hash"]: "not a list"}
    )

    result = ingest(tmp_path, MINI, replay, env)

    report = json.loads(result.stdout)
    assert report["rejected"] == 1
    assert report["rejections"][0]["reason"] == "malformed_response"


def test_missing_replay_entry_is_reported(tmp_path):
    env = env_for(tmp_path)
    replay = write_replay(tmp_path, {})

    result = ingest(tmp_path, MINI, replay, env)

    report = json.loads(result.stdout)
    assert report["rejected"] == len(report["rejections"]) >= 1
    assert report["rejections"][0]["reason"] == "no_replay_entry"


def test_broken_pdf_fails_with_readable_error(tmp_path):
    env = env_for(tmp_path)
    replay = write_replay(tmp_path, {"__default__": []})

    result = ingest(tmp_path, BROKEN, replay, env)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["errors"]
    assert "pdf" in report["errors"][0]


def test_directory_ingest_processes_each_pdf(tmp_path):
    env = env_for(tmp_path)
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    shutil.copy(MINI, pdfs / "a.pdf")
    shutil.copy(LONG, pdfs / "b.pdf")
    replay = write_replay(tmp_path, {"__default__": []})

    result = ingest(tmp_path, pdfs, replay, env)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["papers"] == 2
    assert report["sections"] > 0
    assert report["candidates"] == 0


def test_real_extractor_requires_api_key(tmp_path):
    env = env_for(tmp_path)
    replay = write_replay(tmp_path, {"__default__": []})

    result = ingest(tmp_path, MINI, replay, env, "--extractor", "real")

    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["candidates"] == 0
    assert report["rejections"]
    assert all(item["reason"] == "extractor_error" for item in report["rejections"])
    assert "API_KEY" in report["rejections"][0]["detail"]
