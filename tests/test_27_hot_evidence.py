"""Ticket 27 acceptance: P2 hot evidence block with diff, impact and review trigger."""

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
        "MPPKB_REVIEWER": "hot-tester",
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


def ingest(tmp_path: Path, pdf: Path, env: dict[str, str], payloads: list[dict]) -> None:
    info = inspect_pdf(tmp_path, pdf, env)
    section = info["sections"][0]
    replay = tmp_path / f"replay-{pdf.stem}-{len(payloads)}.json"
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: payloads}), encoding="utf-8"
    )
    result = run_cli(
        tmp_path, "ingest", str(pdf), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert result.returncode == 0, result.stdout + result.stderr


def remap(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def ask(tmp_path: Path, env: dict[str, str], query: str) -> dict:
    result = run_cli(tmp_path, "answer", "--query", query, "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_high_tier_hot_update_has_diff_impact_and_trigger(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    section = info["sections"][0]
    ingest(tmp_path, MINI, env, [payload_for(info, section)])

    long_info = inspect_pdf(tmp_path, LONG, env)
    long_section = long_info["sections"][0]
    ingest(
        tmp_path,
        LONG,
        env,
        [
            {
                "population": "儿童",
                "severity": None,
                "intervention": "阿奇霉素",
                "attribute": "剂量",
                "value": {"kind": "numeric", "value": 10, "unit": "mg/kg/day"},
                "direction": None,
                "evidence_type": "guideline",
                "anchors": [
                    {
                        "doc_id": long_info["sha256"][:16],
                        "page_start": long_section["page_start"],
                        "page_end": long_section["page_end"],
                        "quote": long_section["text"].strip()[:24],
                    }
                ],
                "confidence": 0.8,
            }
        ],
    )
    remap(tmp_path, env)

    hot_pdf = tmp_path / "hot.pdf"
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "更新指南：阿奇霉素改为备选", fontname="china-s", fontsize=12)
    document.set_metadata(
        {"title": "更新指南", "subject": "publisher=中华医学会;evidence_type=guideline"}
    )
    document.save(str(hot_pdf))
    document.close()
    hot_info = inspect_pdf(tmp_path, hot_pdf, env)
    hot_section = hot_info["sections"][0]
    ingest(
        tmp_path,
        hot_pdf,
        env,
        [
            payload_for(
                hot_info,
                hot_section,
                value={"kind": "direction", "value": "alternative"},
                direction="alternative",
            )
        ],
    )
    report = remap(tmp_path, env)
    assert report["conflicts"], report

    answer = ask(tmp_path, env, "阿奇霉素 一线治疗 儿童")

    assert answer["review_triggered"] is True
    assert answer["evidence_status_block"]["status"] == "pending_updates"
    assert len(answer["hot_updates"]) == 1
    update = answer["hot_updates"][0]
    assert "direction" in update["diff"]["changed"]
    assert update["diff"]["old_text"] == "首选"
    assert update["diff"]["new_text"] == "备选"
    assert update["impact"], update
    assert any(item["attribute"] in ("剂量", "ATTR:DOSE") for item in update["impact"])

    sections_text = " ".join(section["text"] for section in answer["sections"])
    assert "首选" in sections_text
    assert "备选" not in sections_text

    connection = sqlite3.connect(env["MPPKB_KB_DB"])
    count = connection.execute(
        "SELECT COUNT(*) FROM kb_audit WHERE action = 'p2_hot_evidence'"
    ).fetchone()[0]
    connection.close()
    assert count >= 1


def test_low_tier_hot_version_is_hidden(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    section = info["sections"][0]
    ingest(
        tmp_path,
        MINI,
        env,
        [
            {
                "population": "儿童",
                "severity": None,
                "intervention": "阿奇霉素",
                "attribute": "疗程",
                "value": {"kind": "numeric", "value": 3, "unit": "days"},
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
        ],
    )
    remap(tmp_path, env)

    long_info = inspect_pdf(tmp_path, LONG, env)
    long_section = long_info["sections"][0]
    ingest(
        tmp_path,
        LONG,
        env,
        [
            {
                "population": "儿童",
                "severity": None,
                "intervention": "阿奇霉素",
                "attribute": "疗程",
                "value": {"kind": "numeric", "value": 5, "unit": "days"},
                "direction": None,
                "evidence_type": "case_report",
                "anchors": [
                    {
                        "doc_id": long_info["sha256"][:16],
                        "page_start": long_section["page_start"],
                        "page_end": long_section["page_end"],
                        "quote": long_section["text"].strip()[:24],
                    }
                ],
            }
        ],
    )
    remap(tmp_path, env)

    answer = ask(tmp_path, env, "阿奇霉素 疗程 儿童")
    assert answer["hot_updates"] == []
    assert answer["review_triggered"] is False


def test_p0_warning_and_hot_update_coexist(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    section = info["sections"][0]
    ingest(
        tmp_path,
        MINI,
        env,
        [payload_for(info, section, intervention="左氧氟沙星")],
    )
    long_info = inspect_pdf(tmp_path, LONG, env)
    long_section = long_info["sections"][0]
    ingest(
        tmp_path,
        LONG,
        env,
        [
            {
                "population": "儿童",
                "severity": None,
                "intervention": "阿奇霉素",
                "attribute": "一线治疗",
                "value": {"kind": "direction", "value": "first_line"},
                "direction": "first_line",
                "evidence_type": "guideline",
                "anchors": [
                    {
                        "doc_id": long_info["sha256"][:16],
                        "page_start": long_section["page_start"],
                        "page_end": long_section["page_end"],
                        "quote": long_section["text"].strip()[:24],
                    }
                ],
            }
        ],
    )
    remap(tmp_path, env)

    hot_pdf = tmp_path / "hot-p0.pdf"
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "更新指南：阿奇霉素改为备选", fontname="china-s", fontsize=12)
    document.set_metadata({"title": "更新指南", "subject": "publisher=中华医学会;evidence_type=guideline"})
    document.save(str(hot_pdf))
    document.close()
    hot_info = inspect_pdf(tmp_path, hot_pdf, env)
    hot_section = hot_info["sections"][0]
    ingest(
        tmp_path,
        hot_pdf,
        env,
        [
            payload_for(
                hot_info,
                hot_section,
                value={"kind": "direction", "value": "alternative"},
                direction="alternative",
            )
        ],
    )
    remap(tmp_path, env)

    answer = ask(tmp_path, env, "左氧氟沙星 儿童 一线治疗")
    assert answer["p0_warning"] is not None
    assert answer["recommendation_suppressed"] is True
    assert answer["hot_updates"], answer
