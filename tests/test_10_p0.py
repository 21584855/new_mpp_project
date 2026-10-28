"""Ticket 10 acceptance: P0 safety exceptions, warning injection, confirm/withdraw."""

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
        "MPPKB_REVIEWER": "p0-tester",
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
        "intervention": "左氧氟沙星",
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


def setup_p0_claim(tmp_path: Path, env: dict[str, str]) -> None:
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    replay = tmp_path / "replay.json"
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: [make_payload(info, section)]}),
        encoding="utf-8",
    )
    ingest = run_cli(
        tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert ingest.returncode == 0, ingest.stdout + ingest.stderr
    remap = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert remap.returncode == 0, remap.stderr


def ask(tmp_path: Path, env: dict[str, str], query: str = "左氧氟沙星 儿童 一线治疗") -> dict:
    result = run_cli(tmp_path, "answer", "--query", query, "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_p0_rules_are_seeded(tmp_path):
    env = env_for(tmp_path)

    result = run_cli(tmp_path, "p0", "list", "--json", env_extra=env)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["seeded"] >= 5
    ids = {rule["rule_id"] for rule in payload["rules"]}
    assert "P0:QUINOLONE_UNDER_18" in ids
    assert any("四环素" in rule["template"] for rule in payload["rules"])


def test_p0_claim_is_promoted_and_warned(tmp_path):
    env = env_for(tmp_path)
    setup_p0_claim(tmp_path, env)

    approved = json.loads(
        run_cli(tmp_path, "claims", "list", "--status", "approved", "--json", env_extra=env).stdout
    )["claims"]
    assert len(approved) == 1
    assert approved[0]["provenance"] == "p0_exception"

    answer = ask(tmp_path, env)
    assert answer["p0_warning"] is not None
    assert answer["sections"][0]["name"] == "安全警示"
    assert "喹诺酮" in answer["sections"][0]["text"]
    assert answer["recommendation_suppressed"] is True
    assert all(section.get("suppressed") for section in answer["sections"][1:])


def test_p0_withdrawal_removes_warning(tmp_path):
    env = env_for(tmp_path)
    setup_p0_claim(tmp_path, env)
    queue = json.loads(
        run_cli(tmp_path, "review", "list", "--json", env_extra=env).stdout
    )["items"]
    assert len(queue) == 1
    assert queue[0]["provenance"] == "p0_exception"

    withdrawn = run_cli(
        tmp_path,
        "review",
        "reject",
        queue[0]["claim_id"],
        "--version-id",
        queue[0]["version_id"],
        "--reason",
        "outdated",
        "--json",
        env_extra=env,
    )

    assert withdrawn.returncode == 0, withdrawn.stderr
    assert json.loads(withdrawn.stdout)["withdrawn"] is True
    assert ask(tmp_path, env)["p0_warning"] is None


def test_p0_confirmation_clears_queue(tmp_path):
    env = env_for(tmp_path)
    setup_p0_claim(tmp_path, env)
    queue = json.loads(
        run_cli(tmp_path, "review", "list", "--json", env_extra=env).stdout
    )["items"]

    confirmed = run_cli(
        tmp_path,
        "review",
        "approve",
        queue[0]["claim_id"],
        "--version-id",
        queue[0]["version_id"],
        "--json",
        env_extra=env,
    )

    assert confirmed.returncode == 0, confirmed.stderr
    assert json.loads(confirmed.stdout)["confirmed"] is True
    assert json.loads(run_cli(tmp_path, "review", "list", "--json", env_extra=env).stdout)["items"] == []
    approved = json.loads(
        run_cli(tmp_path, "claims", "list", "--status", "approved", "--json", env_extra=env).stdout
    )["claims"]
    assert approved[0]["provenance"] == "manual"


def test_adult_quinolone_is_not_a_p0_exception(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    replay = tmp_path / "replay-adult.json"
    replay.write_text(
        json.dumps(
            {
                "__default__": [],
                section["chunk_hash"]: [make_payload(info, section, population="成人")],
            }
        ),
        encoding="utf-8",
    )
    run_cli(tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env)
    run_cli(tmp_path, "remap", "--json", env_extra=env)

    approved = json.loads(
        run_cli(tmp_path, "claims", "list", "--status", "approved", "--json", env_extra=env).stdout
    )["claims"]
    assert len(approved) == 1
    assert approved[0]["provenance"] == "auto"
    answer = ask(tmp_path, env, "左氧氟沙星 成人 一线治疗")
    assert answer["p0_warning"] is None
    assert answer["recommendation_suppressed"] is False


def test_withdrawal_restores_the_superseded_version(tmp_path):
    env = env_for(tmp_path)
    # Seed the rules, then disable them so the first version can be approved.
    run_cli(tmp_path, "p0", "list", "--json", env_extra=env)
    db_path = env["MPPKB_KB_DB"]

    def set_enabled(enabled: int) -> None:
        connection = sqlite3.connect(db_path)
        connection.execute("UPDATE p0_rules SET enabled = ?", (enabled,))
        connection.commit()
        connection.close()

    set_enabled(0)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    first_replay = tmp_path / "replay-first.json"
    first_replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: [make_payload(info, section)]}),
        encoding="utf-8",
    )
    run_cli(tmp_path, "ingest", str(MINI), "--replay-file", str(first_replay), "--json", env_extra=env)
    run_cli(tmp_path, "remap", "--json", env_extra=env)
    first = json.loads(
        run_cli(tmp_path, "claims", "list", "--status", "approved", "--json", env_extra=env).stdout
    )["claims"][0]

    set_enabled(1)
    long_info = json.loads(
        run_cli(tmp_path, "inspect", str(LONG), "--json", env_extra=env).stdout
    )
    long_section = long_info["sections"][0]
    second_replay = tmp_path / "replay-second.json"
    second_replay.write_text(
        json.dumps(
            {
                "__default__": [],
                long_section["chunk_hash"]: [
                    make_payload(
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
    run_cli(tmp_path, "ingest", str(LONG), "--replay-file", str(second_replay), "--json", env_extra=env)
    run_cli(tmp_path, "remap", "--json", env_extra=env)

    queue = json.loads(
        run_cli(tmp_path, "review", "list", "--json", env_extra=env).stdout
    )["items"]
    p0_item = next(item for item in queue if item["provenance"] == "p0_exception")
    withdrawn = run_cli(
        tmp_path,
        "review",
        "reject",
        p0_item["claim_id"],
        "--version-id",
        p0_item["version_id"],
        "--reason",
        "outdated",
        "--json",
        env_extra=env,
    )
    assert withdrawn.returncode == 0, withdrawn.stderr
    payload = json.loads(withdrawn.stdout)
    assert payload["restored_version_id"] == first["version_id"]

    approved = json.loads(
        run_cli(tmp_path, "claims", "list", "--status", "approved", "--json", env_extra=env).stdout
    )["claims"]
    assert [item["version_id"] for item in approved] == [first["version_id"]]
    assert ask(tmp_path, env)["p0_warning"] is None
    verified = run_cli(tmp_path, "rebuild", "--verify", "--json", env_extra=env)
    assert verified.returncode == 0, verified.stdout


def test_p0_warning_is_relevant_to_the_mentioned_drug(tmp_path):
    env = env_for(tmp_path)
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    payloads = [
        make_payload(
            info,
            section,
            intervention="阿奇霉素",
            attribute="剂量",
            value={"kind": "numeric", "value": 10, "unit": "mg/kg/day"},
            direction=None,
        ),
        make_payload(
            info,
            section,
            intervention="多西环素",
            attribute="剂量",
            value={"kind": "numeric", "value": 2, "unit": "mg/kg/d"},
            direction=None,
        ),
    ]
    replay = tmp_path / "replay-two-drugs.json"
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: payloads}, ensure_ascii=False),
        encoding="utf-8",
    )
    ingest = run_cli(
        tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert ingest.returncode == 0, ingest.stdout + ingest.stderr
    run_cli(tmp_path, "remap", "--json", env_extra=env)

    azithromycin = ask(tmp_path, env, "阿奇霉素 儿童 剂量")
    assert azithromycin["p0_warning"] is None
    assert azithromycin["recommendation_suppressed"] is False

    doxycycline = ask(tmp_path, env, "多西环素 儿童 剂量")
    assert doxycycline["p0_warning"] is not None
    assert doxycycline["recommendation_suppressed"] is True
