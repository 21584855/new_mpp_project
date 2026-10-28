"""Ticket 06 acceptance: risk-tier promotion, review decisions, TTL, sampling."""

from __future__ import annotations

import json
from pathlib import Path

from support import run_cli

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINI = FIXTURES / "mini_two_pages.pdf"

DRUG_LABELS = [
    "阿奇霉素",
    "红霉素",
    "克拉霉素",
    "罗红霉素",
    "多西环素",
    "米诺环素",
    "四环素",
    "左氧氟沙星",
    "莫西沙星",
    "环丙沙星",
    "甲泼尼龙",
    "泼尼松",
    "泼尼松龙",
    "地塞米松",
    "氢化可的松",
    "静脉注射免疫球蛋白",
    "支气管镜",
    "支气管肺泡灌洗",
    "对乙酰氨基酚",
    "布洛芬",
]


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


def ingest(tmp_path: Path, env: dict[str, str], payloads: list[dict]) -> dict:
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    mapping = {"__default__": [], section["chunk_hash"]: payloads}
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    result = run_cli(
        tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return info


def remap(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def claims(tmp_path: Path, env: dict[str, str], status: str) -> list[dict]:
    result = run_cli(tmp_path, "claims", "list", "--status", status, "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["claims"]


def review_list(tmp_path: Path, env: dict[str, str]) -> list[dict]:
    result = run_cli(tmp_path, "review", "list", "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["items"]


def test_high_evidence_is_auto_promoted(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(tmp_path, env, [make_payload(info, section)])

    report = remap(tmp_path, env)

    assert report["promoted"] == 1
    approved = claims(tmp_path, env, "approved")
    assert len(approved) == 1
    assert approved[0]["decided_by"] == "system"
    status = json.loads(run_cli(tmp_path, "status", "--json", env_extra=env).stdout)
    assert status["library_version"] == 1


def test_low_evidence_waits_in_queue(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(
        tmp_path, env, [make_payload(info, section, evidence_type="observational_study")]
    )

    report = remap(tmp_path, env)

    assert report["promoted"] == 0
    queue = review_list(tmp_path, env)
    assert len(queue) == 1
    assert queue[0]["status"] == "proposed"


def test_manual_approve_promotes_and_bumps_version(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(
        tmp_path, env, [make_payload(info, section, evidence_type="observational_study")]
    )
    remap(tmp_path, env)
    claim_id = review_list(tmp_path, env)[0]["claim_id"]

    result = run_cli(
        tmp_path, "review", "approve", claim_id, "--json", env_extra=env
    )

    assert result.returncode == 0, result.stderr
    approved = claims(tmp_path, env, "approved")
    assert len(approved) == 1
    assert approved[0]["decided_by"] == "tester"
    status = json.loads(run_cli(tmp_path, "status", "--json", env_extra=env).stdout)
    assert status["library_version"] == 1


def test_reject_requires_a_reason_code(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(
        tmp_path, env, [make_payload(info, section, evidence_type="observational_study")]
    )
    remap(tmp_path, env)
    claim_id = review_list(tmp_path, env)[0]["claim_id"]

    invalid = run_cli(
        tmp_path, "review", "reject", claim_id, "--reason", "nonsense", "--json", env_extra=env
    )
    rejected = run_cli(
        tmp_path, "review", "reject", claim_id, "--reason", "outdated", "--json", env_extra=env
    )

    assert invalid.returncode == 1
    assert "reason" in json.loads(invalid.stdout)["error"]
    assert rejected.returncode == 0, rejected.stderr
    assert len(claims(tmp_path, env, "rejected")) == 1


def test_hold_removes_from_queue(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(
        tmp_path, env, [make_payload(info, section, evidence_type="observational_study")]
    )
    remap(tmp_path, env)
    claim_id = review_list(tmp_path, env)[0]["claim_id"]

    result = run_cli(tmp_path, "review", "hold", claim_id, "--json", env_extra=env)

    assert result.returncode == 0, result.stderr
    assert review_list(tmp_path, env) == []
    assert len(claims(tmp_path, env, "held")) == 1


def test_ttl_expires_undecided_hot_versions(tmp_path):
    env = env_for(tmp_path, MPPKB_NOW="2026-01-01T00:00:00Z")
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    ingest(
        tmp_path, env, [make_payload(info, section, evidence_type="observational_study")]
    )
    remap(tmp_path, env)
    assert len(review_list(tmp_path, env)) == 1

    later = {**env, "MPPKB_NOW": "2026-06-01T00:00:00Z"}
    assert review_list(tmp_path, later) == []
    assert len(claims(tmp_path, later, "expired")) == 1


def test_auto_promotion_sampling_is_deterministic(tmp_path):
    payloads = []
    sample_counts = []
    for run_index in range(2):
        root = tmp_path / f"run{run_index}"
        root.mkdir()
        env = env_for(root)
        info = inspect_pdf(root, env)
        section = info["sections"][0]
        payloads = [
            make_payload(info, section, intervention=label) for label in DRUG_LABELS
        ]
        ingest(root, env, payloads)
        report = remap(root, env)
        assert report["promoted"] == len(DRUG_LABELS)
        sample_counts.append(report["sampled"])

    assert 0 <= sample_counts[0] <= 6
    assert sample_counts[0] == sample_counts[1]
