"""Normalisation of extracted candidates into claim versions (ticket 04).

Candidates carry free text; normalisation maps them onto the controlled
vocabulary, derives the stable claim identity (the slot), and records the
version. Positional knowledge that cannot be mapped stays ``unsupported`` until
the vocabulary grows and ``remap`` is run again.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any

from .clock import now_iso
from .config import Config
from .db import ensure_databases
from .ontology import Ontology, OntologyError, load_ontology, sync
from .scope import allow_attribute, attribute_scope
from .promote import classify_and_promote, expire_hot


class NormalizeError(Exception):
    """Raised when normalisation cannot proceed."""


AE_TYPE_ALIASES = {
    "overall": "overall",
    "总": "overall",
    "总体": "overall",
    "rash": "rash",
    "皮疹": "rash",
    "gi": "gi",
    "胃肠道": "gi",
    "消化道": "gi",
    "liver": "liver",
    "肝功能": "liver",
    "肝损伤": "liver",
    "local": "local",
    "局部": "local",
    "other": "other",
}
AE_ARM_ALIASES = {
    "observation": "observation",
    "观察组": "observation",
    "研究组": "observation",
    "治疗组": "observation",
    "control": "control",
    "对照组": "control",
    "常规组": "control",
    "unspecified": "",
}
AE_METRICS = {"rate", "count"}


def _typed_ae_attribute(
    payload: dict[str, Any], attribute: str | None, term_ids: set[str]
) -> tuple[str | None, str]:
    """Map an AE candidate with type/arm/metric fields to a typed attribute."""
    if attribute != "ATTR:ADVERSE_EVENT":
        return attribute, ""
    raw_type = str(payload.get("ae_type") or "").strip().lower()
    ae_type = AE_TYPE_ALIASES.get(raw_type, raw_type if raw_type in AE_TYPE_ALIASES else "")
    metric = str(payload.get("metric") or "").strip().lower()
    if not ae_type or metric not in AE_METRICS:
        return attribute, ""
    typed = f"ATTR:AE_{ae_type.upper()}_{metric.upper()}"
    if typed not in term_ids:
        return attribute, ""
    raw_arm = str(payload.get("arm") or "").strip().lower()
    arm = AE_ARM_ALIASES.get(raw_arm, raw_arm if raw_arm in ("observation", "control") else "")
    if arm == "unspecified":
        arm = ""
    return typed, arm


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _merge_anchors(existing: list[dict], incoming: list[dict]) -> list[dict]:
    seen: set[str] = set()
    merged: list[dict] = []
    for anchor in list(existing) + list(incoming):
        key = _canonical(anchor)
        if key in seen:
            continue
        seen.add(key)
        merged.append(anchor)
    return merged


def _event_snapshot(
    claim_id: str,
    version_id: str,
    payload: dict,
    ontology: Ontology,
    anchors: list[dict],
) -> str:
    return _canonical(
        {
            "claim_id": claim_id,
            "version_id": version_id,
            "value": payload.get("value"),
            "direction": payload.get("direction"),
            "evidence_type": payload.get("evidence_type"),
            "provenance": "auto",
            "ontology_version": ontology.version,
            "anchors": anchors,
        }
    )


def normalize_candidates(
    kb, ontology: Ontology, now: str | None = None, include_labs: bool = False
) -> dict[str, int]:
    now = now or now_iso()
    rows = kb.execute(
        "SELECT candidate_id, paper_id, chunk_hash, payload_json FROM candidates "
        "WHERE status IN ('proposed', 'unsupported') "
        + (
            " OR (status = 'rejected' AND reason IN ('lab_filtered', 'non_treatment'))"
            if include_labs
            else ""
        )
        + " ORDER BY created_at, candidate_id"
    ).fetchall()

    counts = {
        "candidates": len(rows),
        "normalized": 0,
        "unsupported": 0,
        "claims_created": 0,
        "versions_created": 0,
        "anchors_merged": 0,
        "filtered": 0,
        "lab_filtered": 0,
    }

    with kb:
        term_ids = {term.term_id for term in ontology.terms}
        for row in rows:
            payload = json.loads(row["payload_json"])
            population = ontology.map_text(payload.get("population"), "population")
            intervention = ontology.map_text(payload.get("intervention"), "drug")
            attribute = ontology.map_text(payload.get("attribute"), "attribute")
            attribute, arm = _typed_ae_attribute(payload, attribute, term_ids)
            severity_text = payload.get("severity")
            severity = ontology.map_text(severity_text, "severity") if severity_text else None

            if not (population and intervention and attribute) or (severity_text and not severity):
                kb.execute(
                    "UPDATE candidates SET status = 'unsupported', reason = 'unmapped_term' "
                    "WHERE candidate_id = ?",
                    (row["candidate_id"],),
                )
                counts["unsupported"] += 1
                continue

            scope = attribute_scope(attribute)
            if not allow_attribute(attribute, include_labs):
                reason = "lab_filtered" if scope == "lab" else "non_treatment"
                kb.execute(
                    "UPDATE candidates SET status = 'rejected', reason = ? "
                    "WHERE candidate_id = ?",
                    (reason, row["candidate_id"]),
                )
                counts["filtered"] += 1
                if scope == "lab":
                    counts["lab_filtered"] += 1
                continue

            slot_key = f"{population}|{severity or ''}|{intervention}|{attribute}"
            if arm:
                slot_key += f"|{arm}"
            claim_id = _sha16(slot_key)
            value_json = _canonical(payload.get("value"))
            direction = payload.get("direction")
            version_id = _sha16(claim_id + value_json + (direction or ""))
            anchors = payload.get("anchors") or []

            cursor = kb.execute(
                "INSERT OR IGNORE INTO claims(claim_id, slot_key, population, severity, "
                "intervention, attribute, arm, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    claim_id,
                    slot_key,
                    population,
                    severity,
                    intervention,
                    attribute,
                    arm,
                    now,
                ),
            )
            if cursor.rowcount:
                counts["claims_created"] += 1

            existing = kb.execute(
                "SELECT anchors_json FROM claim_versions WHERE version_id = ?",
                (version_id,),
            ).fetchone()
            merged_existing = False
            if existing is None:
                kb.execute(
                    "INSERT INTO claim_versions(version_id, claim_id, value_json, direction, "
                    "status, evidence_type, provenance, anchors_json, ontology_version, "
                    "created_at, published_date, effective_date) "
                    "VALUES (?, ?, ?, ?, 'proposed', ?, 'auto', ?, ?, ?, ?, ?)",
                    (
                        version_id,
                        claim_id,
                        value_json,
                        direction,
                        payload.get("evidence_type"),
                        _canonical(anchors),
                        ontology.version,
                        now,
                        payload.get("published_date"),
                        payload.get("effective_date"),
                    ),
                )
                counts["versions_created"] += 1
                event_anchors = anchors
            else:
                current = json.loads(existing["anchors_json"])
                merged = _merge_anchors(current, anchors)
                event_anchors = merged
                if len(merged) != len(current):
                    kb.execute(
                        "UPDATE claim_versions SET anchors_json = ? WHERE version_id = ?",
                        (_canonical(merged), version_id),
                    )
                    counts["anchors_merged"] += 1
                    merged_existing = True
                else:
                    continue

            event_type = "anchors_merged" if merged_existing else "extract_proposed"
            kb.execute(
                "INSERT INTO claim_events(event_type, claim_id, version_id, actor, "
                "payload_json, occurred_at) VALUES (?, ?, ?, 'system', ?, ?)",
                (
                    event_type,
                    claim_id,
                    version_id,
                    _event_snapshot(claim_id, version_id, payload, ontology, event_anchors),
                    now,
                ),
            )
            kb.execute(
                "UPDATE candidates SET status = 'normalized', reason = NULL "
                "WHERE candidate_id = ?",
                (row["candidate_id"],),
            )
            counts["normalized"] += 1

    return counts


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _fail(args: Any, message: str) -> int:
    if getattr(args, "json", False):
        _emit({"ok": False, "error": message})
    else:
        print(f"error: {message}", file=sys.stderr)
    return 1


def run(args: Any, cfg: Config) -> int:
    if getattr(args, "command", "") != "remap":
        return _fail(args, f"unsupported normalize command: {getattr(args, 'command', '')}")

    try:
        ontology = load_ontology(cfg.ontology)
    except OntologyError as exc:
        return _fail(args, str(exc))

    kb, corpus = ensure_databases(cfg)
    try:
        sync(kb, ontology)
    except OntologyError as exc:
        return _fail(args, str(exc))

    now = now_iso()
    expired = expire_hot(kb, cfg, now)
    include_labs = bool(getattr(args, "include_labs", False)) or os.environ.get(
        "MPPKB_INCLUDE_LABS", ""
    ).strip().lower() in ("1", "true", "yes")
    counts = normalize_candidates(kb, ontology, now, include_labs)
    promotion = classify_and_promote(kb, corpus, cfg, now)
    payload = {"ok": True, **counts, "expired": expired, **promotion}
    if getattr(args, "json", False):
        _emit(payload)
    else:
        for key, value in counts.items():
            print(f"{key}: {value}")
        print(f"expired: {expired}")
        print(f"conflicts: {len(promotion['conflicts'])}")
        print(f"promoted: {promotion['promoted']}")
        print(f"superseded: {promotion['superseded']}")
        print(f"sampled: {promotion['sampled']}")
    return 0
