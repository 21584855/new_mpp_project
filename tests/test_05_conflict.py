"""Ticket 05 acceptance: deterministic conflict classification."""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
from support import run_cli

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINI = FIXTURES / "mini_two_pages.pdf"


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    values = {
        "MPPKB_KB_DB": str(tmp_path / "data" / "kb.sqlite"),
        "MPPKB_CORPUS_DB": str(tmp_path / "data" / "corpus.sqlite"),
        "MPPKB_CORPUS_DIR": str(corpus),
        "MPPKB_ONTOLOGY": str(Path(__file__).resolve().parents[1] / "ontology.yaml"),
    }
    values.update(extra)
    return values


def make_pdf(path: Path, text: str, publisher: str = "中华医学会") -> Path:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontname="china-s", fontsize=12)
    document.set_metadata(
        {
            "title": "测试指南",
            "subject": f"publisher={publisher};evidence_type=guideline",
        }
    )
    document.save(str(path))
    document.close()
    return path


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


def ingest(tmp_path: Path, pdf: Path, env: dict[str, str], mapping: dict) -> None:
    replay = tmp_path / f"replay-{pdf.stem}.json"
    replay.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    result = run_cli(
        tmp_path, "ingest", str(pdf), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert result.returncode == 0, result.stdout + result.stderr


def remap(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def claims(tmp_path: Path, env: dict[str, str], status: str) -> list[dict]:
    result = run_cli(
        tmp_path, "claims", "list", "--status", status, "--json", env_extra=env
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["claims"]


def test_conflicting_value_goes_to_queue(tmp_path):
    env = env_for(tmp_path)
    first = inspect_pdf(tmp_path, MINI, env)
    section = first["sections"][0]
    ingest(tmp_path, MINI, env, {"__default__": [], section["chunk_hash"]: [payload_for(first, section)]})
    promoted = remap(tmp_path, env)
    assert promoted["promoted"] == 1

    other_pdf = make_pdf(tmp_path / "other.pdf", "一、总论\n儿童肺炎支原体肺炎二线治疗")
    other = inspect_pdf(tmp_path, other_pdf, env)
    other_section = other["sections"][0]
    ingest(
        tmp_path,
        other_pdf,
        env,
        {
            "__default__": [],
            other_section["chunk_hash"]: [
                payload_for(
                    other,
                    other_section,
                    value={"kind": "direction", "value": "alternative"},
                    direction="alternative",
                )
            ],
        },
    )

    report = remap(tmp_path, env)

    assert report["promoted"] == 0
    assert len(report["conflicts"]) == 1
    conflicted = claims(tmp_path, env, "conflicted")
    assert len(conflicted) == 1
    assert conflicted[0]["direction"] == "alternative"

    # Re-running does not reclassify or duplicate the queue.
    again = remap(tmp_path, env)
    assert again["conflicts"] == []
    assert len(claims(tmp_path, env, "conflicted")) == 1


def test_intra_batch_conflict_reports_both_versions(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    first, second = info["sections"][0], info["sections"][1]
    ingest(
        tmp_path,
        MINI,
        env,
        {
            "__default__": [],
            first["chunk_hash"]: [payload_for(info, first)],
            second["chunk_hash"]: [
                payload_for(
                    info,
                    second,
                    value={"kind": "direction", "value": "avoid"},
                    direction="avoid",
                )
            ],
        },
    )

    report = remap(tmp_path, env)

    assert len(report["conflicts"]) == 2
    assert len(claims(tmp_path, env, "conflicted")) == 2


def test_overlapping_range_is_refinement(tmp_path):
    env = env_for(tmp_path)
    first = inspect_pdf(tmp_path, MINI, env)
    section = first["sections"][0]
    ingest(
        tmp_path,
        MINI,
        env,
        {
            "__default__": [],
            section["chunk_hash"]: [
                payload_for(
                    first,
                    section,
                    attribute="剂量",
                    value={"kind": "range", "low": 2, "high": 5, "unit": "mg/kg/d"},
                    direction=None,
                )
            ],
        },
    )
    assert remap(tmp_path, env)["promoted"] == 1

    other_pdf = make_pdf(tmp_path / "narrow.pdf", "一、剂量\n儿童阿奇霉素剂量细化")
    other = inspect_pdf(tmp_path, other_pdf, env)
    other_section = other["sections"][0]
    ingest(
        tmp_path,
        other_pdf,
        env,
        {
            "__default__": [],
            other_section["chunk_hash"]: [
                payload_for(
                    other,
                    other_section,
                    attribute="剂量",
                    value={"kind": "range", "low": 3, "high": 4, "unit": "mg/kg/d"},
                    direction=None,
                )
            ],
        },
    )

    report = remap(tmp_path, env)

    assert report["conflicts"] == []
    assert report["promoted"] == 1
    assert report["superseded"] == 1
    assert len(claims(tmp_path, env, "approved")) == 1
    assert len(claims(tmp_path, env, "superseded")) == 1


def test_same_publisher_newer_guideline_supersedes(tmp_path):
    env = env_for(tmp_path)
    old_pdf = make_pdf(tmp_path / "old.pdf", "一、总论\n儿童肺炎支原体肺炎诊疗指南 2019")
    old = inspect_pdf(tmp_path, old_pdf, env)
    old_section = old["sections"][0]
    ingest(
        tmp_path,
        old_pdf,
        env,
        {"__default__": [], old_section["chunk_hash"]: [payload_for(old, old_section)]},
    )
    assert remap(tmp_path, env)["promoted"] == 1

    new_pdf = make_pdf(tmp_path / "new.pdf", "一、总论\n儿童肺炎支原体肺炎诊疗指南 2024 更新")
    new = inspect_pdf(tmp_path, new_pdf, env)
    new_section = new["sections"][0]
    ingest(
        tmp_path,
        new_pdf,
        env,
        {
            "__default__": [],
            new_section["chunk_hash"]: [
                payload_for(
                    new,
                    new_section,
                    value={"kind": "direction", "value": "alternative"},
                    direction="alternative",
                )
            ],
        },
    )

    report = remap(tmp_path, env)

    assert report["conflicts"] == []
    assert report["promoted"] == 1
    assert report["superseded"] == 1
    assert len(claims(tmp_path, env, "approved")) == 1
    assert len(claims(tmp_path, env, "superseded")) == 1
