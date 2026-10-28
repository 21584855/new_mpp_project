"""Ticket 04 acceptance: ontology seed, append-only vocabulary, normalisation."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from support import run_cli

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINI = FIXTURES / "mini_two_pages.pdf"


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    ontology = tmp_path / "ontology.yaml"
    if not ontology.exists():
        shutil.copy(REPO / "ontology.yaml", ontology)
    values = {
        "MPPKB_KB_DB": str(tmp_path / "data" / "kb.sqlite"),
        "MPPKB_CORPUS_DB": str(tmp_path / "data" / "corpus.sqlite"),
        "MPPKB_CORPUS_DIR": str(corpus),
        "MPPKB_ONTOLOGY": str(ontology),
    }
    values.update(extra)
    return values


def inspect_pdf(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env)
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


def ingest_with_replay(tmp_path: Path, env: dict[str, str], mapping: dict) -> None:
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    result = run_cli(
        tmp_path,
        "ingest",
        str(MINI),
        "--replay-file",
        str(replay),
        "--json",
        env_extra=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ontology_seed_is_loaded(tmp_path):
    env = env_for(tmp_path)

    result = run_cli(tmp_path, "ontology", "list", "--json", env_extra=env)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["version"] == 3
    assert len(payload["terms"]) >= 100
    kinds = {term["kind"] for term in payload["terms"]}
    assert {"drug", "attribute", "population", "severity", "criterion"} <= kinds


def test_ontology_add_is_append_only(tmp_path):
    env = env_for(tmp_path)

    added = run_cli(
        tmp_path,
        "ontology",
        "add",
        "--kind",
        "drug",
        "--term",
        "DRUG:TEST_DRUG",
        "--label",
        "测试药",
        "--json",
        env_extra=env,
    )
    duplicated = run_cli(
        tmp_path,
        "ontology",
        "add",
        "--kind",
        "drug",
        "--term",
        "DRUG:TEST_DRUG",
        "--label",
        "测试药",
        "--json",
        env_extra=env,
    )

    assert added.returncode == 0, added.stderr
    assert json.loads(added.stdout)["version"] == 4
    assert duplicated.returncode == 1
    assert "append-only" in json.loads(duplicated.stdout)["error"]


def test_ingest_then_remap_creates_claim(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest_with_replay(
        tmp_path, env, {"__default__": [], section["chunk_hash"]: [make_payload(info, section)]}
    )

    remapped = run_cli(tmp_path, "remap", "--json", env_extra=env)

    assert remapped.returncode == 0, remapped.stderr
    report = json.loads(remapped.stdout)
    assert report["normalized"] == 1
    assert report["claims_created"] == 1

    claims = json.loads(
        run_cli(tmp_path, "claims", "list", "--status", "approved", "--json", env_extra=env).stdout
    )
    assert len(claims["claims"]) == 1
    claim = claims["claims"][0]
    assert claim["slot"]["population"] == "POP:CHILDREN"
    assert claim["slot"]["intervention"] == "DRUG:AZITHROMYCIN"
    assert claim["slot"]["attribute"] == "ATTR:FIRST_LINE"
    assert claim["value"]["value"] == "first_line"
    assert claim["status"] == "approved"


def test_unmapped_candidate_waits_for_ontology_extension(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest_with_replay(
        tmp_path,
        env,
        {
            "__default__": [],
            section["chunk_hash"]: [make_payload(info, section, intervention="测试药")],
        },
    )

    first = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["unsupported"] == 1

    unsupported = json.loads(
        run_cli(tmp_path, "candidates", "list", "--status", "unsupported", "--json", env_extra=env).stdout
    )
    assert len(unsupported["candidates"]) == 1

    added = run_cli(
        tmp_path,
        "ontology",
        "add",
        "--kind",
        "drug",
        "--term",
        "DRUG:TEST_DRUG",
        "--label",
        "测试药",
        "--json",
        env_extra=env,
    )
    assert added.returncode == 0, added.stderr

    second = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["normalized"] == 1

    claims = json.loads(
        run_cli(tmp_path, "claims", "list", "--json", env_extra=env).stdout
    )
    assert len(claims["claims"]) == 1
    assert claims["claims"][0]["slot"]["intervention"] == "DRUG:TEST_DRUG"


def test_same_slot_same_value_merges_anchors(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    first, second = info["sections"][0], info["sections"][1]
    ingest_with_replay(
        tmp_path,
        env,
        {
            "__default__": [],
            first["chunk_hash"]: [make_payload(info, first)],
            second["chunk_hash"]: [make_payload(info, second)],
        },
    )

    remapped = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert remapped.returncode == 0, remapped.stderr

    claims = json.loads(
        run_cli(tmp_path, "claims", "list", "--status", "approved", "--json", env_extra=env).stdout
    )
    assert len(claims["claims"]) == 1
    assert len(claims["claims"][0]["anchors"]) == 2


def test_same_slot_different_value_creates_two_versions(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    first, second = info["sections"][0], info["sections"][1]
    ingest_with_replay(
        tmp_path,
        env,
        {
            "__default__": [],
            first["chunk_hash"]: [make_payload(info, first)],
            second["chunk_hash"]: [
                make_payload(
                    info,
                    second,
                    value={"kind": "direction", "value": "alternative"},
                    direction="alternative",
                )
            ],
        },
    )

    remapped = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert remapped.returncode == 0, remapped.stderr

    claims = json.loads(
        run_cli(tmp_path, "claims", "list", "--status", "conflicted", "--json", env_extra=env).stdout
    )
    assert len(claims["claims"]) == 2
    claim_ids = {claim["claim_id"] for claim in claims["claims"]}
    assert len(claim_ids) == 1


def test_remap_is_idempotent(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest_with_replay(
        tmp_path, env, {"__default__": [], section["chunk_hash"]: [make_payload(info, section)]}
    )

    first = run_cli(tmp_path, "remap", "--json", env_extra=env)
    second = run_cli(tmp_path, "remap", "--json", env_extra=env)

    assert first.returncode == 0 and second.returncode == 0
    assert json.loads(first.stdout)["normalized"] == 1
    assert json.loads(second.stdout)["candidates"] == 0


def test_lab_claim_is_filtered_by_default_and_allowed_with_flag(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    lab = make_payload(
        info,
        section,
        attribute="血糖",
        value={"kind": "numeric", "value": 6.5, "unit": "mmol/L"},
        direction=None,
    )
    ingest_with_replay(tmp_path, env, {"__default__": [], section["chunk_hash"]: [lab]})

    first = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert first.returncode == 0, first.stderr
    report = json.loads(first.stdout)
    assert report["filtered"] == 1
    assert report["lab_filtered"] == 1
    claims = json.loads(
        run_cli(tmp_path, "claims", "list", "--json", env_extra=env).stdout
    )["claims"]
    assert claims == []

    second = run_cli(tmp_path, "remap", "--include-labs", "--json", env_extra=env)
    assert second.returncode == 0, second.stderr
    report = json.loads(second.stdout)
    assert report["normalized"] == 1
    claims = json.loads(
        run_cli(tmp_path, "claims", "list", "--json", env_extra=env).stdout
    )["claims"]
    assert len(claims) == 1
    assert claims[0]["slot"]["attribute"] == "ATTR:BLOOD_GLUCOSE"


