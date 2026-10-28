"""Ticket 26 acceptance: immutable snapshots, dates and version chain."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pymupdf
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
        "MPPKB_REVIEWER": "snapshot-tester",
    }
    values.update(extra)
    return values


def make_pdf(path: Path, text: str, publisher: str = "中华医学会") -> Path:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontname="china-s", fontsize=12)
    document.set_metadata(
        {"title": "测试指南", "subject": f"publisher={publisher};evidence_type=guideline"}
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


def remap(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def claims(tmp_path: Path, env: dict[str, str], status: str) -> list[dict]:
    result = run_cli(tmp_path, "claims", "list", "--status", status, "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["claims"]


def test_promotion_creates_snapshot_and_dates(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, MINI, env)
    section = info["sections"][0]
    ingest(tmp_path, MINI, env, [payload_for(info, section)])
    remap(tmp_path, env)

    approved = claims(tmp_path, env, "approved")
    assert len(approved) == 1
    assert approved[0]["effective_date"]
    assert approved[0]["retrieved_date"]

    answer = run_cli(
        tmp_path, "answer", "--query", "阿奇霉素 一线治疗 儿童", "--json", env_extra=env
    )
    payload = json.loads(answer.stdout)
    assert payload["snapshot_id"]

    verified = run_cli(tmp_path, "rebuild", "--verify", "--json", env_extra=env)
    assert verified.returncode == 0, verified.stdout
    assert json.loads(verified.stdout)["snapshots"] >= 1


def test_supersede_links_versions_and_snapshots(tmp_path):
    env = env_for(tmp_path)
    old_pdf = make_pdf(tmp_path / "old.pdf", "一、总论\n儿童肺炎支原体肺炎诊疗指南 2020")
    old = inspect_pdf(tmp_path, old_pdf, env)
    old_section = old["sections"][0]
    ingest(
        tmp_path,
        old_pdf,
        env,
        [payload_for(old, old_section, effective_date="2020-01-01")],
    )
    remap(tmp_path, env)

    new_pdf = make_pdf(tmp_path / "new.pdf", "一、总论\n儿童肺炎支原体肺炎诊疗指南 2024 更新")
    new = inspect_pdf(tmp_path, new_pdf, env)
    new_section = new["sections"][0]
    ingest(
        tmp_path,
        new_pdf,
        env,
        [
            payload_for(
                new,
                new_section,
                value={"kind": "direction", "value": "alternative"},
                direction="alternative",
                effective_date="2024-01-01",
            )
        ],
    )
    report = remap(tmp_path, env)
    assert report["superseded"] == 1

    approved = claims(tmp_path, env, "approved")
    superseded = claims(tmp_path, env, "superseded")
    assert len(approved) == 1 and len(superseded) == 1
    assert approved[0]["supersedes"] == superseded[0]["version_id"]
    assert superseded[0]["superseded_by"] == approved[0]["version_id"]
    assert approved[0]["effective_date"] == "2024-01-01"

    connection = sqlite3.connect(env["MPPKB_KB_DB"])
    rows = connection.execute(
        "SELECT status FROM kb_snapshots ORDER BY created_at"
    ).fetchall()
    connection.close()
    statuses = [row[0] for row in rows]
    assert statuses.count("active") == 1
    assert "superseded" in statuses


def test_older_effective_date_is_a_conflict_not_supersede(tmp_path):
    env = env_for(tmp_path)
    old_pdf = make_pdf(tmp_path / "old.pdf", "一、总论\n儿童肺炎支原体肺炎诊疗指南 2020")
    old = inspect_pdf(tmp_path, old_pdf, env)
    old_section = old["sections"][0]
    ingest(
        tmp_path,
        old_pdf,
        env,
        [payload_for(old, old_section, effective_date="2020-01-01")],
    )
    remap(tmp_path, env)

    earlier_pdf = make_pdf(tmp_path / "earlier.pdf", "一、总论\n儿童肺炎支原体肺炎诊疗指南 2018")
    earlier = inspect_pdf(tmp_path, earlier_pdf, env)
    earlier_section = earlier["sections"][0]
    ingest(
        tmp_path,
        earlier_pdf,
        env,
        [
            payload_for(
                earlier,
                earlier_section,
                value={"kind": "direction", "value": "alternative"},
                direction="alternative",
                effective_date="2018-01-01",
            )
        ],
    )

    report = remap(tmp_path, env)

    assert report["superseded"] == 0
    assert len(report["conflicts"]) == 1
    assert claims(tmp_path, env, "conflicted")
