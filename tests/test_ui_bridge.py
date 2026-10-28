"""Ticket integration: external MPP_Project frontend backed by the mppkb kernel."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mppkb.config import load_config
from mppkb.ui_bridge import KernelBridge, _value_text

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
        "MPPKB_REVIEWER": "bridge-tester",
        "MPPKB_RETRIEVAL_MODE": "lexical",
    }
    values.update(extra)
    return values


def seed_claim(tmp_path: Path, env: dict[str, str]) -> None:
    info = json.loads(run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env).stdout)
    section = info["sections"][0]
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
    }
    replay = tmp_path / "replay-bridge.json"
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: [payload]}, ensure_ascii=False),
        encoding="utf-8",
    )
    ingest = run_cli(
        tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert ingest.returncode == 0, ingest.stdout + ingest.stderr
    remap = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert remap.returncode == 0, remap.stderr


def test_bridge_catalog_and_analyze_contract(tmp_path, monkeypatch):
    env = env_for(tmp_path)
    seed_claim(tmp_path, env)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    bridge = KernelBridge(load_config())
    try:
        catalog = bridge.catalog()
        assert catalog["nodes"]
        assert catalog["evidence_types"][0]["key"] == "guideline"
        assert catalog["default_question"]

        result = bridge.analyze("阿奇霉素 儿童 一线治疗")
        assert result["model_used"] is True
        assert result["evidence"]
        assert len(result["reasoning_steps"]) == 6
        assert result["summary"]["evidence_count"] == len(result["evidence"])
        assert "Source[1]" in result["answer_markdown"]
        assert result["edges"] == []
    finally:
        bridge.kb.close()


def test_value_text_formats_numeric_and_direction():
    assert _value_text({"kind": "numeric", "value": 10, "unit": "mg/kg"}, None) == "10 mg/kg"
    assert _value_text({"kind": "range", "low": 3, "high": 4, "unit": "d"}, None) == "3–4 d"
    assert _value_text({"kind": "direction", "value": "first_line"}, None) == "首选"


def test_evidence_edges_follow_slot_values_and_years():
    def node(node_id, value, year, p0=False):
        return {
            "id": node_id,
            "_group": ("DRUG:AZITHROMYCIN", "ATTR:DOSE"),
            "_value": value,
            "_year": year,
            "_p0": p0,
        }

    nodes = [
        node("a", "10 mg/kg/day", 2019),
        node("b", "10 mg/kg/day", 2023),
        node("c", "5 mg/kg/day", 2023),
        node("p0", "2 mg/kg/day", 2023, p0=True),
    ]
    edges = KernelBridge._build_edges(None, nodes)
    relations = {(edge["source"], edge["target"], edge["relation"]) for edge in edges}
    assert ("b", "a", "supports") in relations
    assert ("c", "a", "updates") in relations
    assert ("c", "b", "cautions") in relations
    assert any(edge["source"] == "p0" and edge["relation"] == "cautions" for edge in edges)


def _replay(
    tmp_path: Path,
    env: dict[str, str],
    section: dict,
    value: int,
    name: str,
    pdf: Path = MINI,
) -> None:
    info = json.loads(run_cli(tmp_path, "inspect", str(pdf), "--json", env_extra=env).stdout)
    payload = {
        "population": "儿童",
        "severity": None,
        "intervention": "阿奇霉素",
        "attribute": "剂量",
        "value": {"kind": "numeric", "value": value, "unit": "mg/kg/day"},
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
    replay = tmp_path / name
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: [payload]}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = run_cli(
        tmp_path, "ingest", str(pdf), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_queue_page_facets_filters_and_pagination(tmp_path, monkeypatch):
    env = env_for(tmp_path)
    info = json.loads(run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env).stdout)
    _replay(tmp_path, env, info["sections"][0], 10, "replay-page-first.json")
    run_cli(tmp_path, "remap", "--json", env_extra=env)
    long_info = json.loads(
        run_cli(tmp_path, "inspect", str(LONG), "--json", env_extra=env).stdout
    )
    _replay(
        tmp_path,
        env,
        long_info["sections"][0],
        20,
        "replay-page-second.json",
        pdf=LONG,
    )
    run_cli(tmp_path, "remap", "--json", env_extra=env)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    bridge = KernelBridge(load_config())
    try:
        page = bridge.queue_page(page=1, page_size=5, group_by="intervention")
        assert page["total"] >= 1
        assert page["pages"] >= 1
        assert page["items"]
        assert page["facets"]["interventions"]
        if page["facets"]["severities"]:
            severity = page["facets"]["severities"][0]["key"]
            severity_filtered = bridge.queue_page(page=1, page_size=5, severity=severity)
            assert all(
                item["severity_key"] == severity for item in severity_filtered["items"]
            )
        intervention = page["facets"]["interventions"][0]["key"]
        filtered = bridge.queue_page(page=1, page_size=5, intervention=intervention)
        assert filtered["total"] >= 1
        assert all(item["intervention_key"] == intervention for item in filtered["items"])
        grouped = bridge.queue_page(page=1, page_size=5, group_by="severity")
        assert grouped["group_by"] == "severity"
        assert all("group" in item for item in grouped["items"])
        clamped = bridge.queue_page(page=99, page_size=5)
        assert clamped["page"] == clamped["pages"]
        searched = bridge.queue_page(q="阿奇霉素", page_size=5)
        assert searched["total"] >= 1

        keys = bridge.queue_keys()
        assert keys["total"] == bridge.queue_page(page_size=100)["total"]
        assert len(keys["items"]) == keys["total"]
        csv_text = bridge.queue_csv()
        assert "claim_id" in csv_text
        assert keys["items"][0]["claim_id"] in csv_text
        reasons = bridge.reason_codes()
        assert {item["code"] for item in reasons["items"]} >= {
            "insufficient_evidence",
            "outdated",
        }
    finally:
        bridge.kb.close()


def test_batch_impact_previews_replacement_and_applies(tmp_path, monkeypatch):
    env = env_for(tmp_path)
    info = json.loads(run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env).stdout)
    sections = info["sections"]
    _replay(tmp_path, env, sections[0], 10, "replay-first.json")
    run_cli(tmp_path, "remap", "--json", env_extra=env)
    long_info = json.loads(
        run_cli(tmp_path, "inspect", str(LONG), "--json", env_extra=env).stdout
    )
    _replay(
        tmp_path,
        env,
        long_info["sections"][0],
        20,
        "replay-second.json",
        pdf=LONG,
    )
    run_cli(tmp_path, "remap", "--json", env_extra=env)
    answer_cli = run_cli(
        tmp_path, "answer", "--query", "阿奇霉素 剂量 儿童", "--json", env_extra=env
    )
    assert answer_cli.returncode == 0, answer_cli.stderr
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    bridge = KernelBridge(load_config())
    try:
        pending = [
            item
            for item in bridge.queue()["items"]
            if item["status"] in ("proposed", "conflicted")
        ]
        assert pending, "second value should land in the review queue"
        item = pending[0]
        assigned = bridge.assign(
            [{"claim_id": item["claim_id"], "version_id": item["version_id"]}], "reviewer-A"
        )
        assert assigned["succeeded"] == 1
        assert bridge.queue_page(assignee="reviewer-A")["total"] == 1
        assert "reviewer-A" in bridge.reviewers()["items"]
        assert bridge.queue_page(assignee="__unassigned")["total"] >= 0

        reassigned = bridge.assign(
            [{"claim_id": item["claim_id"], "version_id": item["version_id"]}], "reviewer-B"
        )
        assert reassigned["succeeded"] == 1
        assert reassigned["results"][0]["previous_reviewer"] == "reviewer-A"
        assert bridge.queue_page(assignee="reviewer-B")["total"] == 1
        audit_events = bridge.kb.execute(
            "SELECT COUNT(*) FROM kb_audit WHERE action = 'assign_reviewer'"
        ).fetchone()[0]
        assert audit_events == 2

        old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat().replace("+00:00", "Z")
        bridge.kb.execute(
            "UPDATE review_assignments SET assigned_at = ? WHERE version_id = ?",
            (old, item["version_id"]),
        )
        bridge.kb.commit()
        sla_page = bridge.queue_page(assignee="reviewer-B")
        assert sla_page["items"][0]["sla"]["overdue"] is True
        assert bridge.sla()["overdue"] >= 1
        assert bridge.queue_page(sla="overdue")["total"] >= 1

        sent = []

        class _FakeResponse:
            def read(self):
                return b""

        def fake_urlopen(request, timeout=10):
            sent.append(request)
            return _FakeResponse()

        monkeypatch.setattr("mppkb.ui_bridge.urllib.request.urlopen", fake_urlopen)
        monkeypatch.setenv("MPPKB_SLA_WEBHOOK_URL", "http://example.invalid/sla")
        notified = bridge.notify_sla()
        assert notified["ok"] is True
        assert "webhook" in notified["channels"]
        payload = json.loads(sent[0].data.decode("utf-8"))
        assert payload["overdue"] >= 1
        audit_actions = {
            row["action"]
            for row in bridge.kb.execute("SELECT DISTINCT action FROM kb_audit").fetchall()
        }
        assert "sla_notify" in audit_actions
        bridge.kb.execute("DELETE FROM meta WHERE key = 'sla_last_notify'")
        bridge.kb.commit()
        first_tick = bridge.sla_tick(cooldown_hours=100)
        second_tick = bridge.sla_tick(cooldown_hours=100)
        assert first_tick["notified"] is True
        assert second_tick["reason"] == "cooldown"

        preview = bridge.batch_impact(
            [
                {
                    "claim_id": item["claim_id"],
                    "version_id": item["version_id"],
                    "action": "approve",
                }
            ]
        )
        assert preview["summary"]["changes"].get("replace") == 1
        assert preview["summary"]["cache_entries"] >= 0
        assert preview["summary"]["answer_log_affected"] >= 1
        history = preview["items"][0]["answer_log"]["items"]
        assert history and history[0]["question"]
        assert history[0]["diffs"]
        regenerated = bridge.regenerate(history[0]["seq"])
        assert regenerated["ok"] is True
        assert regenerated["answer"]
        batch_regenerated = bridge.regenerate_many([history[0]["seq"]])
        assert batch_regenerated["succeeded"] == 1

        monkeypatch.setenv("MPPKB_ANSWER_WRITEBACK", "1")
        old_seq = history[0]["seq"]
        writeback = bridge.writeback(item["claim_id"], item["version_id"])
        assert writeback["written"] >= 1
        comparison = bridge.compare(old_seq)
        assert comparison["new"] is not None
        assert "claims_changed" in comparison["diff"]
        bridge.kb.execute(
            "UPDATE answer_log SET created_at = ? WHERE seq = ?",
            (
                (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z"),
                old_seq,
            ),
        )
        bridge.kb.commit()
        purged = bridge.purge_questions(days=1)
        assert purged["purged"] >= 1
        question_row = bridge.kb.execute(
            "SELECT question FROM answer_log WHERE seq = ?", (old_seq,)
        ).fetchone()
        assert question_row is not None and question_row["question"] is None

        applied = bridge.batch_decide(
            [
                {
                    "claim_id": item["claim_id"],
                    "version_id": item["version_id"],
                    "action": "reject",
                    "reason": "other",
                    "note": "自定义：证据版本过旧",
                }
            ]
        )
        assert applied["succeeded"] == 1 and applied["failed"] == 0
        rejected = bridge.kb.execute(
            "SELECT status FROM claim_versions WHERE version_id = ?",
            (item["version_id"],),
        ).fetchone()
        assert rejected["status"] == "rejected"

        ledger = bridge.ledger(limit=20)["items"]
        entry = next(row for row in ledger if row["version_id"] == item["version_id"])
        assert entry["decision"] == "rejected"
        assert entry["assigned_reviewer"] == "reviewer-B"
        assert entry["reviewer"] == "bridge-tester"
        assert entry["reason_code"] == "other"
        assert entry["review_note"] == "自定义：证据版本过旧"
        reviews_csv_text = bridge.reviews_export()
        assert "claim_id" in reviews_csv_text
        assert item["claim_id"] in reviews_csv_text
        assert "reviewer-B" in reviews_csv_text
        assert "自定义：证据版本过旧" in reviews_csv_text
    finally:
        bridge.kb.close()
