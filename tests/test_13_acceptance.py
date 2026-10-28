"""Ticket 13 acceptance: the vertical slice, end to end."""

from __future__ import annotations

import json
from pathlib import Path

from support import run_cli

REPO = Path(__file__).resolve().parents[1]
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
        "MPPKB_ONTOLOGY": str(REPO / "ontology.yaml"),
        "MPPKB_REVIEWER": "acceptance",
    }
    values.update(extra)
    return values


def inspect(tmp_path: Path, pdf: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "inspect", str(pdf), "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def payload(info: dict, section: dict, **overrides) -> dict:
    base = {
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
    base.update(overrides)
    return base


def ingest_pdf(tmp_path: Path, pdf: Path, env: dict[str, str], items: list[dict]) -> dict:
    info = inspect(tmp_path, pdf, env)
    section = info["sections"][0]
    replay = tmp_path / f"replay-{pdf.stem}.json"
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: items}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = run_cli(
        tmp_path, "ingest", str(pdf), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return {"report": json.loads(result.stdout), "info": info, "section": section}


def remap(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def ask(tmp_path: Path, env: dict[str, str], query: str) -> dict:
    result = run_cli(tmp_path, "answer", "--query", query, "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_vertical_slice_end_to_end(tmp_path):
    env = env_for(tmp_path)

    # 1. ingest one PDF -> candidate with page anchor
    info = inspect(tmp_path, MINI, env)
    section = info["sections"][0]
    replay = tmp_path / "replay-mini.json"
    replay.write_text(
        json.dumps(
            {
                "__default__": [],
                section["chunk_hash"]: [
                    payload(info, section),
                    payload(info, section, intervention="左氧氟沙星"),
                ],
            }
        ),
        encoding="utf-8",
    )
    ingested = run_cli(
        tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env
    )
    report = json.loads(ingested.stdout)
    assert report["candidates"] == 2
    candidates = json.loads(
        run_cli(tmp_path, "candidates", "list", "--json", env_extra=env).stdout
    )["candidates"]
    anchor = candidates[0]["payload"]["anchors"][0]
    assert anchor["page_start"] >= 1 and anchor["quote"]

    # 2. idempotent re-ingest
    again = run_cli(
        tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert json.loads(again.stdout)["skipped"] == len(info["sections"])

    # 3. conflicting candidate -> queue + report
    remap(tmp_path, env)
    long_info = inspect(tmp_path, LONG, env)
    long_section = long_info["sections"][0]
    conflict_replay = tmp_path / "replay-long.json"
    conflict_replay.write_text(
        json.dumps(
            {
                "__default__": [],
                long_section["chunk_hash"]: [
                    payload(
                        long_info,
                        long_section,
                        value={"kind": "direction", "value": "alternative"},
                        direction="alternative",
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    run_cli(tmp_path, "ingest", str(LONG), "--replay-file", str(conflict_replay), "--json", env_extra=env)
    conflict_report = remap(tmp_path, env)
    assert len(conflict_report["conflicts"]) == 1
    queue = json.loads(run_cli(tmp_path, "review", "list", "--json", env_extra=env).stdout)["items"]
    conflict_item = next(item for item in queue if item["status"] == "conflicted")

    # 4. review approve -> library version bump
    approved = run_cli(
        tmp_path, "review", "approve", conflict_item["claim_id"], "--json", env_extra=env
    )
    assert approved.returncode == 0, approved.stderr
    status = json.loads(run_cli(tmp_path, "status", "--json", env_extra=env).stdout)
    assert status["library_version"] == 3

    # 5. rebuild verification
    verified = run_cli(tmp_path, "rebuild", "--verify", "--json", env_extra=env)
    assert verified.returncode == 0, verified.stdout

    # 6. answer uses cold claims with page citations
    answer = ask(tmp_path, env, "阿奇霉素 一线治疗 儿童")
    assert answer["sections"] and answer["citations"]
    assert all(citation["page"] >= 1 for citation in answer["citations"])

    # 7. P0 exception overrides with a fixed warning
    p0_answer = ask(tmp_path, env, "左氧氟沙星 儿童")
    assert p0_answer["p0_warning"] is not None
    assert p0_answer["sections"][0]["name"] == "安全警示"

    # 8. cache invalidates when the library changes
    before = ask(tmp_path, env, "阿奇霉素 一线治疗 儿童")
    import pymupdf

    new_pdf = tmp_path / "new_guideline.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "一、总论\n儿童肺炎支原体肺炎红霉素治疗指南", fontname="china-s", fontsize=12)
    document.set_metadata({"title": "测试指南", "subject": "publisher=中华医学会;evidence_type=guideline"})
    document.save(str(new_pdf))
    document.close()
    new_info = inspect(tmp_path, new_pdf, env)
    new_section = new_info["sections"][0]
    replay = tmp_path / "replay-new.json"
    replay.write_text(
        json.dumps(
            {
                "__default__": [],
                new_section["chunk_hash"]: [
                    payload(new_info, new_section, intervention="红霉素")
                ],
            }
        ),
        encoding="utf-8",
    )
    run_cli(
        tmp_path, "ingest", str(new_pdf), "--replay-file", str(replay), "--json", env_extra=env
    )
    remap(tmp_path, env)
    after = ask(tmp_path, env, "阿奇霉素 一线治疗 儿童")
    assert before["cache_hit"] is True and after["cache_hit"] is False

    # failure paths: broken PDF, refusal, invalid reason code
    broken = run_cli(
        tmp_path, "ingest", str(BROKEN), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert broken.returncode == 1
    refused = ask(tmp_path, env, "量子纠缠 超导")
    assert refused["refused"] is True
    invalid_reason = run_cli(
        tmp_path,
        "review",
        "reject",
        conflict_item["claim_id"],
        "--reason",
        "nonsense",
        "--json",
        env_extra=env,
    )
    assert invalid_reason.returncode == 1
