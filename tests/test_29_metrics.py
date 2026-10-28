"""Ticket 29 acceptance: metrics, six test sets, promotion gate and monitoring."""

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
        "MPPKB_REVIEWER": "metrics-tester",
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


def remap(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def ask(tmp_path: Path, env: dict[str, str], query: str) -> dict:
    result = run_cli(tmp_path, "answer", "--query", query, "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def setup(tmp_path: Path, env: dict[str, str]) -> None:
    import pymupdf

    info = inspect_pdf(tmp_path, MINI, env)
    section = info["sections"][0]
    ingest(tmp_path, MINI, env, [payload_for(info, section)], "first")
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
            }
        ],
        "dose",
    )

    levo_pdf = tmp_path / "levo.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "儿童肺炎支原体肺炎 左氧氟沙星 用药警示", fontname="china-s", fontsize=12)
    document.set_metadata({"title": "耐药支原体", "subject": "publisher=中华医学会;evidence_type=guideline"})
    document.save(str(levo_pdf))
    document.close()
    levo_info = inspect_pdf(tmp_path, levo_pdf, env)
    levo_section = levo_info["sections"][0]
    ingest(
        tmp_path,
        levo_pdf,
        env,
        [payload_for(levo_info, levo_section, intervention="左氧氟沙星")],
        "levo",
    )
    remap(tmp_path, env)

    hot_pdf = tmp_path / "hot.pdf"
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
        "hot",
    )
    remap(tmp_path, env)


def test_report_and_gate(tmp_path):
    env = env_for(tmp_path)
    setup(tmp_path, env)
    ask(tmp_path, env, "阿奇霉素 一线治疗 儿童")

    payload = json.loads(
        run_cli(tmp_path, "metrics", "report", "--json", env_extra=env).stdout
    )
    assert payload["counts"]["approved"] >= 3
    assert payload["metrics"]["citation_accuracy"] == 1.0
    assert payload["metrics"]["avg_latency_ms"] is not None
    assert len(payload["test_sets"]) == 6
    assert payload["gate"]["ok"] is True

    gate = json.loads(
        run_cli(tmp_path, "metrics", "gate", "--json", env_extra=env).stdout
    )
    assert gate["ok"] is True
    ids = {check["id"] for check in gate["checks"]}
    assert {
        "no_unresolved_p0_conflicts",
        "conflict_reports_complete",
        "reviewer_signed",
        "snapshots_complete",
        "cache_versioned",
        "audit_trail",
        "no_regression",
    } <= ids


def test_stale_cache_fails_gate_and_blocks_approval(tmp_path):
    env = env_for(tmp_path)
    setup(tmp_path, env)
    ask(tmp_path, env, "阿奇霉素 一线治疗 儿童")

    connection = sqlite3.connect(env["MPPKB_KB_DB"])
    connection.execute(
        "INSERT INTO retrieval_cache(cache_key, cache_type, value_json, library_version, "
        "embedding_model, created_at, hit_count) VALUES ('stale', 'retrieval', '{}', 0, 'fts5', 'x', 0)"
    )
    connection.commit()
    connection.close()

    gate = run_cli(tmp_path, "metrics", "gate", "--json", env_extra=env)
    assert gate.returncode == 1, gate.stdout
    failed = {check["id"] for check in json.loads(gate.stdout)["checks"] if not check["passed"]}
    assert "cache_versioned" in failed

    queue = json.loads(run_cli(tmp_path, "review", "list", "--json", env_extra=env).stdout)["items"]
    conflicted = next(item for item in queue if item["status"] == "conflicted")
    blocked = run_cli(
        tmp_path,
        "review",
        "approve",
        conflicted["claim_id"],
        "--version-id",
        conflicted["version_id"],
        "--json",
        env_extra={**env, "MPPKB_GATE_ENFORCE": "1"},
    )
    assert blocked.returncode != 0
    assert "gate" in (blocked.stdout + blocked.stderr).lower()


def test_eval_six_sets(tmp_path):
    env = env_for(tmp_path)
    setup(tmp_path, env)

    cases = [
        {
            "set": "snapshot_regression",
            "question": "阿奇霉素 一线治疗 儿童",
            "expect": {"path": "simple", "refused": False, "citation": True},
        },
        {
            "set": "conflict_six_types",
            "question": "阿奇霉素 剂量 儿童",
            "expect": {"refused": False, "citation": True},
        },
        {
            "set": "safety_exception",
            "question": "左氧氟沙星 儿童 一线治疗",
            "expect": {"refused": False, "citation": True},
        },
        {
            "set": "hot_update",
            "question": "重症 阿奇霉素 一线治疗 儿童",
            "expect": {"path": "complex", "citation": True},
        },
        {
            "set": "patient_data_conflict",
            "question": "阿奇霉素 一线治疗",
            "context": {"severity": "severe"},
            "expect": {"path": "complex", "citation": True},
        },
        {
            "set": "edge_cases",
            "question": "量子纠缠 超导 拓扑",
            "expect": {"refused": True, "citation": False},
        },
    ]
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

    payload = json.loads(
        run_cli(
            tmp_path, "metrics", "eval", "--cases", str(cases_file), "--json", env_extra=env
        ).stdout
    )
    assert len(payload["testsets"]) == 6
    for name, summary in payload["testsets"].items():
        assert summary["cases"] == 1
        assert summary["routing_accuracy"] == 1.0, name
        assert summary["refusal_accuracy"] == 1.0, name
        assert summary["citation_accuracy"] == 1.0, name
