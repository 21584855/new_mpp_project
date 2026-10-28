"""Ledger replay and rebuild verification (ticket 07).

The cold library is a materialised view over ``claim_events``. Replay walks the
ledger in order and derives the current state; nothing here calls a model or the
network. ``rebuild --verify`` compares the derived state with the stored rows and
fails loudly on the first structural difference.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .config import Config
from .db import ensure_databases, get_meta


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _merge_anchors(current: list[dict], new: list[dict]) -> list[dict]:
    seen = {json.dumps(anchor, sort_keys=True, ensure_ascii=False) for anchor in current}
    merged = list(current)
    for anchor in new:
        key = json.dumps(anchor, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            merged.append(anchor)
            seen.add(key)
    return merged


def derive(kb) -> tuple[dict[str, dict[str, Any]], int, dict[str, list[str]]]:
    """Replay the ledger into ``version_id -> state``, approvals, and snapshots."""
    state: dict[str, dict[str, Any]] = {}
    snapshots: dict[str, list[str]] = {}
    approvals = 0

    for event in kb.execute("SELECT * FROM claim_events ORDER BY seq ASC").fetchall():
        event_type = event["event_type"]
        version_id = event["version_id"]
        claim_id = event["claim_id"]
        try:
            payload = json.loads(event["payload_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}

        if event_type == "extract_proposed":
            state[version_id] = {
                "claim_id": claim_id,
                "status": "proposed",
                "anchors": payload.get("anchors") or [],
            }
        elif event_type == "anchors_merged":
            if version_id in state:
                state[version_id]["anchors"] = _merge_anchors(
                    state[version_id].get("anchors") or [], payload.get("anchors") or []
                )
        elif event_type == "migrated":
            status = str(payload.get("status") or "approved")
            state[version_id] = {
                "claim_id": claim_id,
                "status": status,
                "anchors": payload.get("anchors") or [],
            }
            if status == "approved":
                approvals += 1
        elif event_type in ("auto_promoted", "reviewed_approved"):
            if version_id not in state:
                state[version_id] = {"claim_id": claim_id, "status": "proposed", "anchors": []}
            for other_id, other in state.items():
                if (
                    other_id != version_id
                    and other.get("claim_id") == claim_id
                    and other.get("status") == "approved"
                ):
                    other["status"] = "superseded"
            state[version_id]["status"] = "approved"
            approvals += 1
        elif event_type == "p0_triggered":
            if version_id not in state:
                state[version_id] = {"claim_id": claim_id, "status": "proposed", "anchors": []}
            for other_id, other in state.items():
                if (
                    other_id != version_id
                    and other.get("claim_id") == claim_id
                    and other.get("status") == "approved"
                ):
                    other["status"] = "superseded"
            state[version_id]["status"] = "approved"
            approvals += 1
        elif event_type == "p0_confirmed":
            if version_id in state:
                state[version_id]["status"] = "approved"
        elif event_type == "p0_withdrawn":
            if version_id in state:
                state[version_id]["status"] = "rejected"
            restored = payload.get("restored_version_id") if isinstance(payload, dict) else None
            if restored and restored in state:
                state[restored]["status"] = "approved"
        elif event_type == "reviewed_rejected":
            if version_id in state:
                state[version_id]["status"] = "rejected"
        elif event_type == "reviewed_held":
            if version_id in state:
                state[version_id]["status"] = "held"
        elif event_type == "conflict_classified":
            if payload.get("category") == "conflict" and version_id in state:
                state[version_id]["status"] = "conflicted"
        elif event_type == "superseded":
            if version_id in state:
                state[version_id]["status"] = "superseded"
        elif event_type == "expired":
            if version_id in state:
                state[version_id]["status"] = "expired"

        snapshot_id = payload.get("snapshot_id") if isinstance(payload, dict) else None
        if snapshot_id and event_type in (
            "migrated",
            "auto_promoted",
            "reviewed_approved",
            "p0_triggered",
            "p0_withdrawn",
        ):
            snapshots[snapshot_id] = sorted(
                vid for vid, item in state.items() if item.get("status") == "approved"
            )

    return state, approvals, snapshots


def verify(kb) -> dict[str, Any]:
    derived, approvals, snapshots = derive(kb)
    stored = {
        row["version_id"]: row
        for row in kb.execute(
            "SELECT version_id, claim_id, status, anchors_json FROM claim_versions"
        ).fetchall()
    }
    mismatches: list[dict[str, Any]] = []

    for version_id, state in sorted(derived.items()):
        row = stored.get(version_id)
        if row is None:
            mismatches.append(
                {
                    "version_id": version_id,
                    "field": "missing_row",
                    "expected": "present",
                    "actual": "absent",
                }
            )
            continue
        if row["status"] != state["status"]:
            mismatches.append(
                {
                    "version_id": version_id,
                    "field": "status",
                    "expected": state["status"],
                    "actual": row["status"],
                }
            )
        try:
            stored_anchors = json.loads(row["anchors_json"])
        except (TypeError, json.JSONDecodeError):
            stored_anchors = None
        if _canonical(stored_anchors) != _canonical(state["anchors"]):
            mismatches.append(
                {
                    "version_id": version_id,
                    "field": "anchors",
                    "expected": state["anchors"],
                    "actual": stored_anchors,
                }
            )

    for version_id in sorted(stored.keys() - derived.keys()):
        mismatches.append(
            {
                "version_id": version_id,
                "field": "unexpected_row",
                "expected": "absent",
                "actual": "present",
            }
        )

    stored_library_version = int(get_meta(kb, "library_version", "0") or 0)
    if stored_library_version != approvals:
        mismatches.append(
            {
                "version_id": None,
                "field": "library_version",
                "expected": approvals,
                "actual": stored_library_version,
            }
        )

    stored_snapshots = {
        row["snapshot_id"]: json.loads(row["claim_ids_json"])
        for row in kb.execute("SELECT snapshot_id, claim_ids_json FROM kb_snapshots").fetchall()
    }
    for snapshot_id, claim_ids in sorted(snapshots.items()):
        stored_ids = stored_snapshots.get(snapshot_id)
        if stored_ids is None:
            mismatches.append(
                {
                    "version_id": snapshot_id,
                    "field": "missing_snapshot",
                    "expected": claim_ids,
                    "actual": None,
                }
            )
        elif sorted(stored_ids) != sorted(claim_ids):
            mismatches.append(
                {
                    "version_id": snapshot_id,
                    "field": "snapshot_claim_ids",
                    "expected": claim_ids,
                    "actual": stored_ids,
                }
            )
    for snapshot_id in sorted(stored_snapshots.keys() - snapshots.keys()):
        mismatches.append(
            {
                "version_id": snapshot_id,
                "field": "unexpected_snapshot",
                "expected": None,
                "actual": stored_snapshots[snapshot_id],
            }
        )

    return {
        "ok": not mismatches,
        "verified": not mismatches,
        "versions": len(derived),
        "snapshots": len(snapshots),
        "library_version": approvals,
        "mismatches": mismatches,
    }


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _fail(args: Any, message: str, code: int = 1) -> int:
    if getattr(args, "json", False):
        _emit({"ok": False, "error": message})
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def run(args: Any, cfg: Config) -> int:
    kb, _corpus = ensure_databases(cfg)
    result = verify(kb)

    if getattr(args, "json", False):
        _emit(result)
    else:
        print(f"versions: {result['versions']}")
        print(f"library_version: {result['library_version']}")
        if result["mismatches"]:
            for mismatch in result["mismatches"]:
                print(
                    f"mismatch {mismatch['version_id']}: {mismatch['field']} "
                    f"expected={mismatch['expected']!r} actual={mismatch['actual']!r}"
                )
        else:
            print("verified")

    return 0 if result["ok"] else 1
