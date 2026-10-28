"""Ticket 38: AE type / arm / metric extraction fields."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from support import run_cli

from mppkb.extract import validate_candidate

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINI = FIXTURES / "mini_two_pages.pdf"


def env_for(tmp_path: Path) -> dict[str, str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    ontology = tmp_path / "ontology.yaml"
    if not ontology.exists():
        shutil.copy(REPO / "ontology.yaml", ontology)
    return {
        "MPPKB_KB_DB": str(tmp_path / "data" / "kb.sqlite"),
        "MPPKB_CORPUS_DB": str(tmp_path / "data" / "corpus.sqlite"),
        "MPPKB_CORPUS_DIR": str(corpus),
        "MPPKB_ONTOLOGY": str(ontology),
    }


def inspect_pdf(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_validate_ae_fields(tmp_path):
    info = inspect_pdf(tmp_path, env_for(tmp_path))
    info["paper_id"] = info["sha256"][:16]
    section = info["sections"][0]
    payload = {
        "population": "儿童",
        "severity": "难治性",
        "intervention": "阿奇霉素",
        "attribute": "不良反应",
        "value": {"kind": "numeric", "value": 22.56, "unit": "%"},
        "direction": None,
        "evidence_type": "randomized_controlled_trial",
        "ae_type": "overall",
        "arm": "observation",
        "metric": "rate",
        "denominator": 34,
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
    assert validate_candidate(payload, info, section) == []

    bad = dict(payload, ae_type="skin")
    assert any("ae_type" in problem for problem in validate_candidate(bad, info, section))
    bad_arm = dict(payload, arm="both")
    assert any("arm" in problem for problem in validate_candidate(bad_arm, info, section))


def test_ingest_and_normalize_typed_ae_claim(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    payload = {
        "population": "儿童",
        "severity": "难治性",
        "intervention": "阿奇霉素",
        "attribute": "不良反应",
        "value": {"kind": "numeric", "value": 22.56, "unit": "%"},
        "direction": None,
        "evidence_type": "guideline",
        "ae_type": "overall",
        "arm": "observation",
        "metric": "rate",
        "denominator": 34,
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
        json.dumps({"__default__": [], section["chunk_hash"]: [payload]}, ensure_ascii=False),
        encoding="utf-8",
    )
    ingested = run_cli(
        tmp_path,
        "ingest",
        str(MINI),
        "--replay-file",
        str(replay),
        "--json",
        env_extra=env,
    )
    assert ingested.returncode == 0, ingested.stdout + ingested.stderr
    remapped = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert remapped.returncode == 0, remapped.stderr

    claims = json.loads(
        run_cli(tmp_path, "claims", "list", "--status", "approved", "--json", env_extra=env).stdout
    )
    assert len(claims["claims"]) == 1
    claim = claims["claims"][0]
    assert claim["slot"]["attribute"] == "ATTR:AE_OVERALL_RATE"
    assert claim["slot"]["arm"] == "observation"
    assert claim["value"]["unit"] == "%"
