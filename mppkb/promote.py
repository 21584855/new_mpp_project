"""Risk-tier promotion, review decisions and hot-store expiry (tickets 05–06).

Classification is delegated to :mod:`mppkb.conflict`; this module applies the
outcome to the ledger, promotes low-risk versions, and records every decision
with actor, time and diff.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from .clock import now_iso
from .config import Config
from .conflict import CONFLICT, classify_claim
from .db import ensure_databases, library_version, set_meta
from .p0 import active_rules, rule_applies

PENDING_STATUSES = ("proposed", "conflicted", "held", "unsupported")
QUEUE_STATUSES = ("proposed", "conflicted", "unsupported")
REASON_CODES = (
    "insufficient_evidence",
    "extraction_error",
    "term_mismatch",
    "duplicate",
    "outdated",
    "other",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _sampled(version_id: str, rate: float) -> bool:
    if rate <= 0:
        return False
    bucket = int(hashlib.sha256((version_id + ":sample").encode("utf-8")).hexdigest(), 16)
    return bucket % 10000 < round(rate * 10000)


def _event(kb, event_type: str, claim_id: str | None, version_id: str | None, payload: dict, now: str) -> None:
    kb.execute(
        "INSERT INTO claim_events(event_type, claim_id, version_id, actor, payload_json, "
        "occurred_at) VALUES (?, ?, ?, 'system', ?, ?)",
        (event_type, claim_id, version_id, _canonical(payload), now),
    )


def _audit(kb, action: str, actor: str, detail: dict, now: str) -> None:
    kb.execute(
        "INSERT INTO kb_audit(event_seq, action, actor, detail_json, created_at) "
        "VALUES (NULL, ?, ?, ?, ?)",
        (action, actor, _canonical(detail), now),
    )


def _snapshot_id(version_ids: list[str], now: str, reviewer: str) -> str:
    raw = "|".join(sorted(version_ids)) + "|" + now + "|" + (reviewer or "")
    return "SNAP-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _create_snapshot(kb, now: str, reviewer: str, version_ids: list[str], effective_date: str | None) -> str:
    """Create an immutable cold snapshot and supersede the previous active one."""
    kb.execute(
        "UPDATE kb_snapshots SET status = 'superseded' "
        "WHERE library = 'cold' AND status = 'active'"
    )
    snapshot_id = _snapshot_id(version_ids, now, reviewer)
    approved = [
        row["version_id"]
        for row in kb.execute(
            "SELECT version_id FROM claim_versions WHERE status = 'approved'"
        ).fetchall()
    ]
    snapshot_ids = sorted(set(version_ids) | set(approved))
    kb.execute(
        "INSERT OR REPLACE INTO kb_snapshots(snapshot_id, library, created_at, effective_date, "
        "reviewer, status, claim_ids_json, eval_json, note) "
        "VALUES (?, 'cold', ?, ?, ?, 'active', ?, NULL, NULL)",
        (
            snapshot_id,
            now,
            effective_date,
            reviewer,
            _canonical(snapshot_ids),
        ),
    )
    return snapshot_id


def _apply_dates(kb, version_id: str, version: dict, now: str) -> str:
    effective = version.get("effective_date") or now[:10]
    kb.execute(
        "UPDATE claim_versions SET published_date = COALESCE(published_date, ?), "
        "effective_date = COALESCE(effective_date, ?), "
        "retrieved_date = COALESCE(retrieved_date, ?) WHERE version_id = ?",
        (version.get("published_date"), effective, now[:10], version_id),
    )
    return effective


def _link_supersede(kb, old_version_id: str, new_version_id: str) -> None:
    kb.execute(
        "UPDATE claim_versions SET superseded_by = ? WHERE version_id = ?",
        (new_version_id, old_version_id),
    )
    kb.execute(
        "UPDATE claim_versions SET supersedes = ? WHERE version_id = ?",
        (old_version_id, new_version_id),
    )


def _version(row) -> dict[str, Any]:
    data = {
        "version_id": row["version_id"],
        "claim_id": row["claim_id"],
        "value": json.loads(row["value_json"]),
        "direction": row["direction"],
        "evidence_type": row["evidence_type"],
        "provenance": row["provenance"],
        "anchors": json.loads(row["anchors_json"]),
        "created_at": row["created_at"],
        "status": row["status"],
    }
    try:
        data["intervention"] = row["intervention"]
    except (IndexError, KeyError):
        data["intervention"] = None
    try:
        data["population"] = row["population"]
    except (IndexError, KeyError):
        data["population"] = None
    for column in ("published_date", "effective_date", "retrieved_date"):
        try:
            data[column] = row[column]
        except (IndexError, KeyError):
            data[column] = None
    return data


def _paper_lookup(corpus, kb) -> dict[str, dict]:
    doc_ids: set[str] = set()
    for row in kb.execute("SELECT anchors_json FROM claim_versions").fetchall():
        try:
            anchors = json.loads(row["anchors_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        for anchor in anchors:
            if isinstance(anchor, dict) and anchor.get("doc_id"):
                doc_ids.add(str(anchor["doc_id"]))
    if not doc_ids:
        return {}
    placeholders = ",".join("?" for _ in doc_ids)
    rows = corpus.execute(
        f"SELECT paper_id, publisher, year FROM papers WHERE paper_id IN ({placeholders})",
        tuple(doc_ids),
    ).fetchall()
    return {
        row["paper_id"]: {"publisher": row["publisher"], "year": row["year"]}
        for row in rows
    }


def expire_hot(kb, cfg: Config, now: str) -> int:
    """Expire undecided hot versions past the TTL; held versions are exempt."""
    cutoff = _parse_time(now) - timedelta(days=cfg.ttl_days)
    rows = kb.execute(
        "SELECT version_id, claim_id, created_at FROM claim_versions "
        "WHERE status IN ('proposed', 'conflicted', 'unsupported')"
    ).fetchall()
    expired = 0
    with kb:
        for row in rows:
            try:
                created = _parse_time(row["created_at"])
            except ValueError:
                continue
            if created >= cutoff:
                continue
            kb.execute(
                "UPDATE claim_versions SET status = 'expired' WHERE version_id = ?",
                (row["version_id"],),
            )
            _event(
                kb,
                "expired",
                row["claim_id"],
                row["version_id"],
                {"reason": "ttl", "ttl_days": cfg.ttl_days},
                now,
            )
            expired += 1
    return expired


def classify_and_promote(kb, corpus, cfg: Config, now: str) -> dict[str, Any]:
    claim_ids = [
        row["claim_id"]
        for row in kb.execute(
            "SELECT DISTINCT claim_id FROM claim_versions WHERE status IN ('approved', 'proposed')"
        ).fetchall()
    ]
    papers = _paper_lookup(corpus, kb)
    p0_rules = active_rules(kb)
    report: dict[str, Any] = {
        "conflicts": [],
        "promoted": 0,
        "superseded": 0,
        "sampled": 0,
    }

    with kb:
        for claim_id in claim_ids:
            rows = kb.execute(
                "SELECT v.*, c.intervention, c.population FROM claim_versions v "
                "JOIN claims c ON c.claim_id = v.claim_id "
                "WHERE v.claim_id = ? AND v.status IN ('approved', 'proposed') "
                "ORDER BY v.created_at, v.version_id",
                (claim_id,),
            ).fetchall()
            versions = [_version(row) for row in rows]
            current = next(
                (version for version in versions if version["status"] == "approved"), None
            )
            proposed = [version for version in versions if version["status"] == "proposed"]
            if not proposed:
                continue

            categories = classify_claim(proposed, current, papers)

            for version in proposed:
                if categories[version["version_id"]] != CONFLICT:
                    continue
                kb.execute(
                    "UPDATE claim_versions SET status = 'conflicted' WHERE version_id = ?",
                    (version["version_id"],),
                )
                _event(
                    kb,
                    "conflict_classified",
                    claim_id,
                    version["version_id"],
                    {"category": CONFLICT, "against": current["version_id"] if current else "batch"},
                    now,
                )
                report["conflicts"].append(
                    {"claim_id": claim_id, "version_id": version["version_id"]}
                )

            for version in proposed:
                rule = p0_rules.get(version["intervention"])
                if rule is not None and not rule_applies(rule, version.get("population")):
                    rule = None
                if rule is not None:
                    if current is not None and current["version_id"] != version["version_id"]:
                        kb.execute(
                            "UPDATE claim_versions SET status = 'superseded' WHERE version_id = ?",
                            (current["version_id"],),
                        )
                        _link_supersede(kb, current["version_id"], version["version_id"])
                        _event(
                            kb,
                            "superseded",
                            claim_id,
                            current["version_id"],
                            {"replaced_by": version["version_id"]},
                            now,
                        )
                        report["superseded"] += 1
                    effective = _apply_dates(kb, version["version_id"], version, now)
                    kb.execute(
                        "UPDATE claim_versions SET status = 'approved', "
                        "provenance = 'p0_exception', decided_at = ?, decided_by = 'system' "
                        "WHERE version_id = ?",
                        (now, version["version_id"]),
                    )
                    snapshot_id = _create_snapshot(
                        kb, now, "system", [version["version_id"]], effective
                    )
                    _event(
                        kb,
                        "p0_triggered",
                        claim_id,
                        version["version_id"],
                        {
                            "rule_id": rule["rule_id"],
                            "template": rule["template"],
                            "snapshot_id": snapshot_id,
                            "effective_date": effective,
                        },
                        now,
                    )
                    _audit(
                        kb,
                        "p0_override",
                        "system",
                        {
                            "claim_id": claim_id,
                            "version_id": version["version_id"],
                            "rule_id": rule["rule_id"],
                        },
                        now,
                    )
                    set_meta(kb, "library_version", library_version(kb) + 1)
                    report["promoted"] += 1
                    report["p0_triggered"] = report.get("p0_triggered", 0) + 1
                    current = {**version, "status": "approved", "provenance": "p0_exception"}
                    continue

                category = categories[version["version_id"]]
                if category == CONFLICT:
                    continue
                if category in ("supersede", "refine"):
                    _event(
                        kb,
                        "conflict_classified",
                        claim_id,
                        version["version_id"],
                        {"category": category},
                        now,
                    )
                if version["evidence_type"] not in cfg.evidence_min:
                    continue
                if not version["anchors"]:
                    continue

                if current is not None and current["version_id"] != version["version_id"]:
                    kb.execute(
                        "UPDATE claim_versions SET status = 'superseded' WHERE version_id = ?",
                        (current["version_id"],),
                    )
                    _link_supersede(kb, current["version_id"], version["version_id"])
                    _event(
                        kb,
                        "superseded",
                        claim_id,
                        current["version_id"],
                        {"replaced_by": version["version_id"]},
                        now,
                    )
                    report["superseded"] += 1

                effective = _apply_dates(kb, version["version_id"], version, now)
                sampled = _sampled(version["version_id"], cfg.sample_rate)
                kb.execute(
                    "UPDATE claim_versions SET status = 'approved', decided_at = ?, "
                    "decided_by = 'system' WHERE version_id = ?",
                    (now, version["version_id"]),
                )
                snapshot_id = _create_snapshot(
                    kb, now, "system", [version["version_id"]], effective
                )
                _event(
                    kb,
                    "auto_promoted",
                    claim_id,
                    version["version_id"],
                    {
                        "category": category,
                        "sampled": sampled,
                        "sample_rate": cfg.sample_rate,
                        "snapshot_id": snapshot_id,
                        "effective_date": effective,
                    },
                    now,
                )
                _audit(
                    kb,
                    "promote",
                    "system",
                    {
                        "claim_id": claim_id,
                        "version_id": version["version_id"],
                        "category": category,
                        "sampled": sampled,
                    },
                    now,
                )
                set_meta(kb, "library_version", library_version(kb) + 1)
                report["promoted"] += 1
                if sampled:
                    report["sampled"] += 1
                current = {
                    **version,
                    "status": "approved",
                }

    return report


def _resolve_version(kb, claim_id: str, version_id: str | None):
    if version_id:
        return kb.execute(
            "SELECT * FROM claim_versions WHERE claim_id = ? AND version_id = ?",
            (claim_id, version_id),
        ).fetchone()
    placeholders = ",".join("?" for _ in PENDING_STATUSES)
    return kb.execute(
        f"SELECT * FROM claim_versions WHERE claim_id = ? AND "
        f"(status IN ({placeholders}) OR (status = 'approved' AND provenance = 'p0_exception')) "
        "ORDER BY created_at DESC, version_id DESC LIMIT 1",
        (claim_id, *PENDING_STATUSES),
    ).fetchone()


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _fail(args: Any, message: str, code: int = 1) -> int:
    if getattr(args, "json", False):
        _emit({"ok": False, "error": message})
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def _queue_item(row) -> dict[str, Any]:
    return {
        "claim_id": row["claim_id"],
        "version_id": row["version_id"],
        "status": row["status"],
        "reason": row["reason_code"],
        "slot": {
            "population": row["population"],
            "severity": row["severity"],
            "intervention": row["intervention"],
            "attribute": row["attribute"],
            "arm": row["arm"],
        },
        "value": json.loads(row["value_json"]),
        "direction": row["direction"],
        "evidence_type": row["evidence_type"],
        "provenance": row["provenance"],
        "anchors": json.loads(row["anchors_json"]),
        "created_at": row["created_at"],
    }


def list_queue(kb) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in QUEUE_STATUSES)
    rows = kb.execute(
        "SELECT v.*, c.population, c.severity, c.intervention, c.attribute, c.arm "
        "FROM claim_versions v JOIN claims c ON c.claim_id = v.claim_id "
        f"WHERE v.status IN ({placeholders}) "
        "OR (v.status = 'approved' AND v.provenance = 'p0_exception') "
        "ORDER BY v.created_at, v.version_id",
        QUEUE_STATUSES,
    ).fetchall()
    return [_queue_item(row) for row in rows]


def _gate_enforced() -> bool:
    return os.environ.get("MPPKB_GATE_ENFORCE", "").strip().lower() in ("1", "true", "yes")


def decide(
    kb,
    cfg: Config,
    action: str,
    claim_id: str,
    version_id: str | None = None,
    reason: str | None = None,
    note: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Apply one review decision atomically; used by CLI and the web console."""
    now = now or now_iso()
    reviewer = cfg.reviewer
    row = _resolve_version(kb, claim_id, version_id)
    if row is None:
        raise LookupError(f"no pending version for claim {claim_id}")

    p0_confirmation = row["status"] == "approved" and row["provenance"] == "p0_exception"
    if action == "approve" and not p0_confirmation and _gate_enforced():
        from .metrics import gate as gate_check

        gate_result = gate_check(kb)
        if not gate_result["ok"]:
            failed = ", ".join(
                check["id"] for check in gate_result["checks"] if not check["passed"]
            )
            raise ValueError(f"promotion gate failed: {failed}")

    with kb:
        kb.execute("DELETE FROM answer_cache")
        kb.execute("DELETE FROM retrieval_cache")
        if row["status"] == "approved" and row["provenance"] == "p0_exception":
            if action == "approve":
                kb.execute(
                    "UPDATE claim_versions SET provenance = 'manual', decided_by = ?, "
                    "decided_at = ?, review_note = ? WHERE version_id = ?",
                    (reviewer, now, note, row["version_id"]),
                )
                _event(
                    kb,
                    "p0_confirmed",
                    claim_id,
                    row["version_id"],
                    {"reviewer": reviewer, "note": note},
                    now,
                )
                _audit(
                    kb,
                    "p0_confirmed",
                    reviewer,
                    {"claim_id": claim_id, "version_id": row["version_id"], "note": note},
                    now,
                )
                return {
                    "ok": True,
                    "claim_id": claim_id,
                    "version_id": row["version_id"],
                    "status": "approved",
                    "confirmed": True,
                    "note": note,
                }
            if action == "reject":
                if reason not in REASON_CODES:
                    raise ValueError(f"reason must be one of {', '.join(REASON_CODES)}")
                restore = kb.execute(
                    "SELECT version_id FROM claim_versions "
                    "WHERE superseded_by = ? AND status = 'superseded'",
                    (row["version_id"],),
                ).fetchone()
                kb.execute(
                    "UPDATE claim_versions SET status = 'rejected', decided_by = ?, "
                    "decided_at = ?, reason_code = ?, review_note = ? WHERE version_id = ?",
                    (reviewer, now, reason, note, row["version_id"]),
                )
                payload: dict[str, Any] = {"reviewer": reviewer, "reason": reason, "note": note}
                restored_version_id = None
                snapshot_id = None
                if restore is not None:
                    restored_version_id = restore["version_id"]
                    kb.execute(
                        "UPDATE claim_versions SET status = 'approved', superseded_by = NULL "
                        "WHERE version_id = ?",
                        (restored_version_id,),
                    )
                    snapshot_id = _create_snapshot(
                        kb, now, reviewer, [restored_version_id], now[:10]
                    )
                    payload.update(
                        {
                            "restored_version_id": restored_version_id,
                            "snapshot_id": snapshot_id,
                        }
                    )
                _event(kb, "p0_withdrawn", claim_id, row["version_id"], payload, now)
                _audit(
                    kb,
                    "p0_withdrawn",
                    reviewer,
                    {
                        "claim_id": claim_id,
                        "version_id": row["version_id"],
                        "reason": reason,
                        "restored_version_id": restored_version_id,
                        "snapshot_id": snapshot_id,
                    },
                    now,
                )
                return {
                    "ok": True,
                    "claim_id": claim_id,
                    "version_id": row["version_id"],
                    "status": "rejected",
                    "withdrawn": True,
                    "restored_version_id": restored_version_id,
                    "snapshot_id": snapshot_id,
                }
            raise ValueError("only approve or reject is meaningful for a P0 exception")

        if action == "approve":
            previous = kb.execute(
                "SELECT version_id FROM claim_versions WHERE claim_id = ? AND status = 'approved'",
                (claim_id,),
            ).fetchall()
            for other in previous:
                if other["version_id"] == row["version_id"]:
                    continue
                kb.execute(
                    "UPDATE claim_versions SET status = 'superseded' WHERE version_id = ?",
                    (other["version_id"],),
                )
                _link_supersede(kb, other["version_id"], row["version_id"])
                _event(
                    kb,
                    "superseded",
                    claim_id,
                    other["version_id"],
                    {"replaced_by": row["version_id"]},
                    now,
                )
            version_data = _version(row)
            effective = _apply_dates(kb, row["version_id"], version_data, now)
            kb.execute(
                "UPDATE claim_versions SET status = 'approved', decided_at = ?, "
                "decided_by = ?, reason_code = NULL, review_note = ? WHERE version_id = ?",
                (now, reviewer, note, row["version_id"]),
            )
            snapshot_id = _create_snapshot(kb, now, reviewer, [row["version_id"]], effective)
            _event(
                kb,
                "reviewed_approved",
                claim_id,
                row["version_id"],
                {
                    "reviewer": reviewer,
                    "snapshot_id": snapshot_id,
                    "effective_date": effective,
                    "note": note,
                },
                now,
            )
            _audit(
                kb,
                "approve",
                reviewer,
                {
                    "claim_id": claim_id,
                    "version_id": row["version_id"],
                    "snapshot_id": snapshot_id,
                    "note": note,
                },
                now,
            )
            set_meta(kb, "library_version", library_version(kb) + 1)
            return {
                "ok": True,
                "claim_id": claim_id,
                "version_id": row["version_id"],
                "snapshot_id": snapshot_id,
                "library_version": library_version(kb),
            }

        if action == "reject":
            if reason not in REASON_CODES:
                raise ValueError(f"reason must be one of {', '.join(REASON_CODES)}")
            kb.execute(
                "UPDATE claim_versions SET status = 'rejected', decided_at = ?, "
                "decided_by = ?, reason_code = ?, review_note = ? WHERE version_id = ?",
                (now, reviewer, reason, note, row["version_id"]),
            )
            _event(
                kb,
                "reviewed_rejected",
                claim_id,
                row["version_id"],
                {"reviewer": reviewer, "reason": reason, "note": note},
                now,
            )
            _audit(
                kb,
                "reject",
                reviewer,
                {"claim_id": claim_id, "version_id": row["version_id"], "reason": reason},
                now,
            )
            return {
                "ok": True,
                "claim_id": claim_id,
                "version_id": row["version_id"],
                "status": "rejected",
                "reason": reason,
            }

        if action == "hold":
            kb.execute(
                "UPDATE claim_versions SET status = 'held', decided_at = ?, "
                "decided_by = ?, review_note = ? WHERE version_id = ?",
                (now, reviewer, note, row["version_id"]),
            )
            _event(
                kb,
                "reviewed_held",
                claim_id,
                row["version_id"],
                {"reviewer": reviewer, "note": note},
                now,
            )
            _audit(
                kb,
                "hold",
                reviewer,
                {"claim_id": claim_id, "version_id": row["version_id"], "note": note},
                now,
            )
            return {
                "ok": True,
                "claim_id": claim_id,
                "version_id": row["version_id"],
                "status": "held",
                "note": note,
            }

    raise ValueError(f"unknown review command: {action}")


def _version_public(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "version_id": row["version_id"],
        "status": row["status"],
        "provenance": row["provenance"],
        "evidence_type": row["evidence_type"],
        "value": json.loads(row["value_json"] or "{}"),
        "direction": row["direction"],
        "created_at": row["created_at"],
        "decided_by": row["decided_by"],
        "reason_code": row["reason_code"],
        "review_note": row["review_note"],
    }


def answers_impact(kb, claim_id: str, version_id: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Historical answers whose cited claims include this claim/version, with value diffs."""
    versions = {str(version_id)} if version_id else set()
    current = kb.execute(
        "SELECT version_id, value_json FROM claim_versions WHERE claim_id = ? AND status = 'approved'",
        (claim_id,),
    ).fetchall()
    current_values = {
        row["version_id"]: json.loads(row["value_json"] or "{}") for row in current
    }
    versions.update(current_values)
    target = None
    if version_id:
        target = kb.execute(
            "SELECT value_json FROM claim_versions WHERE claim_id = ? AND version_id = ?",
            (claim_id, version_id),
        ).fetchone()
    target_value = json.loads(target["value_json"]) if target is not None else None
    rows = kb.execute(
        "SELECT seq, case_id, question_hash, question, snapshot_id, record_json, "
        "created_at, parent_seq, superseded_by, auto_writeback "
        "FROM answer_log ORDER BY seq DESC"
    ).fetchall()
    matches = []
    for row in rows:
        try:
            record = json.loads(row["record_json"] or "{}")
        except json.JSONDecodeError:
            continue
        matched = [
            claim
            for claim in record.get("claims") or []
            if claim.get("claim_id") == claim_id
            or claim.get("version_id") in versions
        ]
        if not matched:
            continue
        diffs = []
        for claim in matched:
            old_value = claim.get("value")
            if claim.get("claim_id") == claim_id and target_value is not None:
                new_value = target_value
            else:
                new_value = current_values.get(claim.get("version_id"))
            diffs.append(
                {
                    "claim_id": claim.get("claim_id"),
                    "version_id": claim.get("version_id"),
                    "old_value": old_value,
                    "new_value": new_value,
                    "changed": _canonical(old_value) != _canonical(new_value),
                }
            )
        matches.append(
            {
                "seq": row["seq"],
                "claim_id": claim_id,
                "case_id": row["case_id"],
                "question": row["question"] or "",
                "question_hash": row["question_hash"],
                "snapshot_id": row["snapshot_id"],
                "library_version": record.get("library_version"),
                "created_at": row["created_at"],
                "parent_seq": row["parent_seq"],
                "superseded_by": row["superseded_by"],
                "auto_writeback": bool(row["auto_writeback"]),
                "matched_version_ids": [claim.get("version_id") for claim in matched],
                "refused": bool(record.get("refused")),
                "diffs": diffs,
            }
        )
    return {"total": len(matches), "items": matches[: max(1, int(limit))]}


def writeback_answers(kb, cfg: Config, claim_id: str, version_id: str | None = None) -> dict[str, Any]:
    """Re-run historical answers affected by a claim decision and keep a version chain."""
    from .answer import answer

    impact = answers_impact(kb, claim_id, version_id)
    written = []
    for item in impact["items"]:
        seq = item["seq"]
        question = item.get("question") or ""
        if not question:
            continue
        row = kb.execute(
            "SELECT superseded_by FROM answer_log WHERE seq = ?", (seq,)
        ).fetchone()
        if row is not None and row["superseded_by"]:
            continue
        answer(kb, cfg, question, item.get("case_id"))
        new_seq = kb.execute("SELECT MAX(seq) FROM answer_log").fetchone()[0]
        with kb:
            kb.execute(
                "UPDATE answer_log SET superseded_by = ?, auto_writeback = 1 WHERE seq = ?",
                (new_seq, seq),
            )
            kb.execute("UPDATE answer_log SET parent_seq = ? WHERE seq = ?", (seq, new_seq))
        written.append({"seq": seq, "new_seq": new_seq})
    return {"ok": True, "written": len(written), "items": written}


def purge_answer_questions(kb, retention_days: int | None = None) -> int:
    """Null out stored question text older than the retention window (hash kept)."""
    if retention_days is None:
        try:
            retention_days = max(1, int(os.environ.get("MPPKB_QUESTION_RETENTION_DAYS", "30")))
        except ValueError:
            retention_days = 30
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat().replace(
        "+00:00", "Z"
    )
    with kb:
        cursor = kb.execute(
            "UPDATE answer_log SET question = NULL WHERE question IS NOT NULL AND created_at < ?",
            (cutoff,),
        )
    return int(cursor.rowcount or 0)


def impact_set(
    kb,
    claim_id: str,
    version_id: str | None = None,
    action: str = "approve",
) -> dict[str, Any]:
    """What changes in the cold library if this decision is applied.

    Returns the current approved version, the proposed/pending version, a
    human-readable change list and the number of retrieval-cache entries that
    become stale when the library version moves.
    """
    row = _resolve_version(kb, claim_id, version_id)
    if row is None:
        raise LookupError(f"no pending version for claim {claim_id}")
    claim = kb.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
    if claim is None:
        raise LookupError(f"claim not found: {claim_id}")
    current = kb.execute(
        "SELECT * FROM claim_versions WHERE claim_id = ? AND status = 'approved' "
        "ORDER BY created_at DESC, version_id DESC LIMIT 1",
        (claim_id,),
    ).fetchone()
    current_public = _version_public(current)
    proposed_public = _version_public(row)
    changes: list[dict[str, Any]] = []
    if action == "reject":
        changes.append({"kind": "reject", "kept": current_public})
    elif action == "hold":
        changes.append({"kind": "hold", "kept": current_public})
    elif current is None:
        changes.append({"kind": "new", "to": proposed_public["value"]})
    elif current["version_id"] == row["version_id"]:
        changes.append({"kind": "noop"})
    elif _canonical(json.loads(current["value_json"])) != _canonical(
        json.loads(row["value_json"])
    ):
        changes.append(
            {
                "kind": "replace",
                "from": current_public["value"],
                "to": proposed_public["value"],
            }
        )
    else:
        changes.append({"kind": "evidence_update", "value": proposed_public["value"]})
    cache_entries = kb.execute("SELECT COUNT(*) FROM retrieval_cache").fetchone()[0]
    return {
        "claim_id": claim_id,
        "version_id": row["version_id"],
        "action": action,
        "slot": {
            "population": claim["population"],
            "severity": claim["severity"],
            "intervention": claim["intervention"],
            "attribute": claim["attribute"],
        },
        "current": current_public,
        "proposed": proposed_public,
        "changes": changes,
        "cache_entries": int(cache_entries),
        "answer_log": answers_impact(kb, claim_id, row["version_id"]),
    }


def batch_impact(kb, items: list[dict[str, Any]]) -> dict[str, Any]:
    impacts = []
    counts: dict[str, int] = {}
    cache_entries = 0
    answer_log_affected = 0
    for item in items:
        impact = impact_set(
            kb,
            str(item.get("claim_id") or ""),
            item.get("version_id"),
            str(item.get("action") or "approve"),
        )
        impacts.append(impact)
        cache_entries = max(cache_entries, int(impact.get("cache_entries") or 0))
        answer_log_affected += int((impact.get("answer_log") or {}).get("total") or 0)
        for change in impact.get("changes") or []:
            kind = str(change.get("kind"))
            counts[kind] = counts.get(kind, 0) + 1
    return {
        "ok": True,
        "items": impacts,
        "summary": {
            "total": len(impacts),
            "changes": counts,
            "cache_entries": cache_entries,
            "answer_log_affected": answer_log_affected,
        },
    }


def batch_decide(kb, cfg: Config, items: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for item in items:
        claim_id = str(item.get("claim_id") or "")
        try:
            impact = impact_set(
                kb, claim_id, item.get("version_id"), str(item.get("action") or "approve")
            )
            result = decide(
                kb,
                cfg,
                str(item.get("action") or "approve"),
                claim_id,
                item.get("version_id"),
                item.get("reason"),
                item.get("note"),
            )
            results.append({"ok": True, "impact": impact, "result": result})
        except (LookupError, ValueError) as exc:
            results.append(
                {
                    "ok": False,
                    "claim_id": claim_id,
                    "version_id": item.get("version_id"),
                    "error": str(exc),
                }
            )
    succeeded = sum(1 for item in results if item["ok"])
    return {
        "ok": succeeded == len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": results,
    }


def assign_reviewer(
    kb,
    claim_id: str,
    version_id: str,
    reviewer: str,
    assigned_by: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    row = _resolve_version(kb, claim_id, version_id)
    if row is None:
        raise LookupError(f"no pending version for claim {claim_id}")
    reviewer = str(reviewer or "").strip()
    if not reviewer:
        raise ValueError("reviewer is required")
    now = now_iso()
    previous = kb.execute(
        "SELECT reviewer FROM review_assignments WHERE version_id = ?", (version_id,)
    ).fetchone()
    with kb:
        kb.execute(
            "INSERT INTO review_assignments(claim_id, version_id, reviewer, assigned_by, "
            "assigned_at, note) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(version_id) DO UPDATE SET reviewer = excluded.reviewer, "
            "assigned_by = excluded.assigned_by, assigned_at = excluded.assigned_at, "
            "note = excluded.note",
            (claim_id, version_id, reviewer, assigned_by, now, note),
        )
        _audit(
            kb,
            "assign_reviewer",
            assigned_by or reviewer,
            {
                "claim_id": claim_id,
                "version_id": version_id,
                "previous_reviewer": previous["reviewer"] if previous else None,
                "reviewer": reviewer,
            },
            now,
        )
    return {
        "ok": True,
        "claim_id": claim_id,
        "version_id": version_id,
        "reviewer": reviewer,
        "previous_reviewer": previous["reviewer"] if previous else None,
        "assigned_at": now,
    }


def assign_batch(
    kb,
    items: list[dict[str, Any]],
    reviewer: str,
    assigned_by: str | None = None,
) -> dict[str, Any]:
    results = []
    for item in items:
        try:
            results.append(
                assign_reviewer(
                    kb,
                    str(item.get("claim_id") or ""),
                    str(item.get("version_id") or ""),
                    reviewer,
                    assigned_by,
                )
            )
        except (LookupError, ValueError) as exc:
            results.append(
                {
                    "ok": False,
                    "claim_id": item.get("claim_id"),
                    "version_id": item.get("version_id"),
                    "error": str(exc),
                }
            )
    succeeded = sum(1 for item in results if item["ok"])
    return {
        "ok": succeeded == len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": results,
    }


def list_assignments(kb) -> dict[str, dict[str, Any]]:
    rows = kb.execute(
        "SELECT version_id, claim_id, reviewer, assigned_by, assigned_at, note "
        "FROM review_assignments"
    ).fetchall()
    return {row["version_id"]: dict(row) for row in rows}


def review_sla_hours() -> int:
    try:
        return max(1, int(os.environ.get("MPPKB_REVIEW_SLA_HOURS", "24")))
    except ValueError:
        return 24


def review_sla_warn_hours() -> int:
    try:
        return max(1, int(os.environ.get("MPPKB_REVIEW_SLA_WARN_HOURS", "4")))
    except ValueError:
        return 4


def assignment_sla(assigned_at: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    try:
        assigned = datetime.fromisoformat(str(assigned_at).replace("Z", "+00:00"))
    except ValueError:
        assigned = now
    due = assigned + timedelta(hours=review_sla_hours())
    hours_left = (due - now).total_seconds() / 3600.0
    return {
        "due_at": due.isoformat().replace("+00:00", "Z"),
        "hours_left": round(hours_left, 2),
        "overdue": hours_left < 0,
        "due_soon": 0 <= hours_left <= review_sla_warn_hours(),
    }


def reviewers(kb) -> list[str]:
    names = {
        row["reviewer"]
        for row in kb.execute("SELECT DISTINCT reviewer FROM review_assignments").fetchall()
        if row["reviewer"]
    }
    names.update(
        row["decided_by"]
        for row in kb.execute(
            "SELECT DISTINCT decided_by FROM claim_versions WHERE decided_by IS NOT NULL"
        ).fetchall()
        if row["decided_by"]
    )
    return sorted(names)


def _ledger_rows(kb, limit: int = 100, reviewer: str = "", action: str = "") -> list[Any]:
    clauses = ["v.decided_at IS NOT NULL"]
    params: list[Any] = []
    if reviewer:
        clauses.append("(v.decided_by = ? OR a.reviewer = ?)")
        params.extend([reviewer, reviewer])
    if action:
        clauses.append("v.status = ?")
        params.append(action)
    params.append(max(1, min(int(limit or 100), 5000)))
    return kb.execute(
        "SELECT v.version_id, v.claim_id, v.status, v.provenance, v.evidence_type, "
        "v.decided_at, v.decided_by, v.reason_code, v.review_note, v.value_json, "
        "c.population, c.severity, c.intervention, c.attribute, "
        "a.reviewer AS assigned_reviewer "
        "FROM claim_versions v JOIN claims c ON c.claim_id = v.claim_id "
        "LEFT JOIN review_assignments a ON a.version_id = v.version_id "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY v.decided_at DESC, v.version_id DESC LIMIT ?",
        params,
    ).fetchall()


def review_ledger(
    kb, limit: int = 100, reviewer: str = "", action: str = ""
) -> list[dict[str, Any]]:
    items = []
    for row in _ledger_rows(kb, limit, reviewer, action):
        items.append(
            {
                "claim_id": row["claim_id"],
                "version_id": row["version_id"],
                "decision": row["status"],
                "reviewer": row["decided_by"],
                "assigned_reviewer": row["assigned_reviewer"],
                "decided_at": row["decided_at"],
                "reason_code": row["reason_code"],
                "review_note": row["review_note"],
                "provenance": row["provenance"],
                "evidence_type": row["evidence_type"],
                "population": row["population"],
                "severity": row["severity"],
                "intervention": row["intervention"],
                "attribute": row["attribute"],
                "value": json.loads(row["value_json"] or "{}"),
            }
        )
    return items


def reviews_csv(kb, reviewer: str = "", action: str = "") -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "claim_id",
            "version_id",
            "decision",
            "reviewer",
            "assigned_reviewer",
            "decided_at",
            "reason_code",
            "review_note",
            "provenance",
            "evidence_type",
            "population",
            "severity",
            "intervention",
            "attribute",
            "value",
        ]
    )
    for row in _ledger_rows(kb, 5000, reviewer, action):
        writer.writerow(
            [
                row["claim_id"],
                row["version_id"],
                row["status"],
                row["decided_by"],
                row["assigned_reviewer"],
                row["decided_at"],
                row["reason_code"],
                row["review_note"],
                row["provenance"],
                row["evidence_type"],
                row["population"],
                row["severity"],
                row["intervention"],
                row["attribute"],
                _canonical(json.loads(row["value_json"] or "{}")),
            ]
        )
    return "\ufeff" + output.getvalue()


def run(args: Any, cfg: Config) -> int:
    now = now_iso()
    kb, corpus = ensure_databases(cfg)
    expire_hot(kb, cfg, now)
    command = getattr(args, "review_command", "list")

    if command == "list":
        items = list_queue(kb)
        if getattr(args, "json", False):
            _emit({"ok": True, "items": items})
        elif not items:
            print("queue empty")
        else:
            for item in items:
                print(
                    f"{item['claim_id']}  {item['version_id']}  {item['status']:<10} "
                    f"{item['slot']['population']}|{item['slot']['intervention']}|"
                    f"{item['slot']['attribute']}  {_canonical(item['value'])}"
                )
        return 0

    claim_id = getattr(args, "claim_id", "")
    version_id = getattr(args, "version_id", None)
    reason = getattr(args, "reason", None)
    note = getattr(args, "note", None)
    try:
        payload = decide(kb, cfg, command, claim_id, version_id, reason, note, now)
    except (LookupError, ValueError) as exc:
        return _fail(args, str(exc))

    if getattr(args, "json", False):
        _emit(payload)
    else:
        print(payload)
    return 0
