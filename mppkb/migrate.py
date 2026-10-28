"""Legacy knowledge migration (ticket 11).

Imports the hand-written nodes and case statements. Records with no page/quote
anchor cannot enter the cold library: they are stored as ``held`` so the gap is
visible instead of silent. Migration is idempotent and writes the ``migrated``
event so the ledger can rebuild the same state.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from .clock import now_iso
from .config import Config
from .db import ensure_databases, library_version, set_meta
from .ontology import Ontology, OntologyError, load_ontology


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _direction(record: dict) -> str:
    text = " ".join(
        str(record.get(key) or "")
        for key in (
            "recommendation_direction",
            "main_findings",
            "title",
            "statement",
            "clinical_use",
        )
    )
    if "避免" in text or "不推荐" in text:
        return "avoid"
    if "首选" in text or "推荐" in text:
        return "first_line"
    return "alternative"


NODE_CRITERIA = {
    "GUIDE-2023-CMA-MPP": "CRIT:MPP_TREATMENT",
    "GUIDE-2024-CAP": "CRIT:CAP_TREATMENT",
    "META-2020-QIU": "CRIT:MPP_COMBINATION",
    "META-2020-SUN": "CRIT:MPP_STEROID_DOSE",
    "RCT-2025-XU-MCMP": "CRIT:MPP_STEROID_DOSE",
    "OBS-2024-TONGJI": "CRIT:MPP_TREATMENT",
    "REVIEW-2025-NARRATIVE": "CRIT:MPP_STEROID_SAFETY",
    "CASE-2025-PE": "CRIT:MPP_COMPLICATION",
}
CASE_CRITERIA = {
    "MPP-DX-001": ("CRIT:MPP_DIAGNOSIS", "ATTR:DIAGNOSTIC_CRITERIA"),
    "MPP-SEV-001": ("CRIT:MPP_SEVERITY", "ATTR:SEVERITY_ASSESSMENT"),
    "MPP-RISK-001": ("CRIT:MPP_RISK", "ATTR:REFRACTORY_RISK"),
    "MPP-TX-001": ("CRIT:MPP_TREATMENT", "ATTR:TREATMENT_PRINCIPLE"),
    "MPP-IMG-001": ("CRIT:MPP_IMAGING", "ATTR:IMAGING"),
    "MPP-FU-001": ("CRIT:MPP_FOLLOW_UP", "ATTR:FOLLOW_UP"),
}


def _non_drug_payload(record: dict, kind: str) -> dict[str, Any]:
    """Map heterogeneous legacy knowledge onto criterion entities + text value."""
    legacy_id = str(record.get("id") or "")
    if kind == "statement":
        criterion, attribute = CASE_CRITERIA.get(
            legacy_id, ("CRIT:MPP_TREATMENT", "ATTR:TREATMENT_PRINCIPLE")
        )
        text = record.get("statement") or record.get("clinical_use") or ""
    else:
        criterion = NODE_CRITERIA.get(legacy_id, "CRIT:MPP_TREATMENT")
        attribute = "ATTR:EVIDENCE_SUMMARY"
        text = record.get("main_findings") or record.get("title") or ""
    return {
        "population": record.get("population") or "儿童",
        "severity": None,
        "intervention": criterion,
        "attribute": attribute,
        "value": {"kind": "text", "value": str(text)},
        "direction": None,
        "evidence_type": record.get("evidence_type"),
        "anchors": record.get("anchors") or [],
    }


def _payload(record: dict, kind: str) -> dict[str, Any]:
    if not record.get("attribute") and not record.get("value"):
        return _non_drug_payload(record, kind)
    direction = _direction(record)
    value = record.get("value") or {"kind": "direction", "value": direction}
    return {
        "population": record.get("population") or "儿童",
        "severity": record.get("severity"),
        "intervention": record.get("intervention") or "",
        "attribute": record.get("attribute") or "",
        "value": value,
        "direction": record.get("direction")
        or (direction if value.get("kind") == "direction" else None),
        "evidence_type": record.get("evidence_type"),
        "anchors": record.get("anchors") or [],
    }


def _records(cfg: Config) -> tuple[list[tuple[str, dict]], list[dict]]:
    base = Path(cfg.legacy_dir)
    nodes = _load_json(base / "evidence_nodes.json", [])
    statements = _load_json(base / "case" / "MycoplasmaPneumonia" / "knowledge_base.json", [])
    edges = _load_json(base / "evidence_edges.json", [])
    records: list[tuple[str, dict]] = []
    if isinstance(nodes, list):
        records.extend(("node", item) for item in nodes if isinstance(item, dict))
    if isinstance(statements, list):
        records.extend(("statement", item) for item in statements if isinstance(item, dict))
    return records, edges if isinstance(edges, list) else []


def _load_anchors(cfg: Config) -> dict[str, list]:
    """Optional AI-drafted anchor overlay keyed by legacy id."""
    path = Path(cfg.migration_anchors)
    if not path.is_file():
        return {}
    data = _load_json(path, {})
    if not isinstance(data, dict):
        return {}
    anchors = data.get("anchors")
    return anchors if isinstance(anchors, dict) else {}


def _migrate_record(
    kb,
    ontology: Ontology,
    kind: str,
    record: dict,
    now: str,
    report: dict[str, Any],
    claim_map: dict[str, str],
) -> None:
    legacy_id = str(record.get("id") or f"{kind}-{len(claim_map) + 1}")
    payload = _payload(record, kind)
    term_ids = {term.term_id for term in ontology.terms}

    population = ontology.map_text(payload["population"], "population")
    if population is None:
        population = ontology.map_text("儿童", "population")
    raw_intervention = str(payload["intervention"] or "")
    if raw_intervention.startswith("CRIT:"):
        intervention = raw_intervention if raw_intervention in term_ids else None
    else:
        intervention = ontology.map_text(raw_intervention, "drug")
    raw_attribute = str(payload["attribute"] or "")
    if raw_attribute.startswith("ATTR:"):
        attribute = raw_attribute if raw_attribute in term_ids else None
    else:
        attribute = ontology.map_text(raw_attribute, "attribute")
    severity_text = payload["severity"]
    severity = ontology.map_text(severity_text, "severity") if severity_text else None
    if not (population and intervention and attribute) or (severity_text and not severity):
        report["failed"].append({"id": legacy_id, "reason": "unmapped"})
        return

    slot_key = f"{population}|{severity or ''}|{intervention}|{attribute}"
    claim_id = _sha16(slot_key)
    value_json = _canonical(payload["value"])
    version_id = _sha16(claim_id + value_json + (payload["direction"] or ""))
    anchors = payload["anchors"] if isinstance(payload["anchors"], list) else []
    status = "approved" if anchors else "held"
    reason = "missing_anchors" if not anchors else None
    anchor_source = (
        "ai-draft"
        if any(isinstance(anchor, dict) and anchor.get("ai_drafted") for anchor in anchors)
        else ("legacy" if anchors else None)
    )

    with kb:
        existing = kb.execute(
            "SELECT 1 FROM claim_versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        if existing is not None:
            report["skipped"] += 1
            claim_map[legacy_id] = claim_id
            return
        kb.execute(
            "INSERT OR IGNORE INTO claims(claim_id, slot_key, population, severity, "
            "intervention, attribute, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (claim_id, slot_key, population, severity, intervention, attribute, now),
        )
        kb.execute(
            "INSERT INTO claim_versions(version_id, claim_id, value_json, direction, status, "
            "evidence_type, provenance, anchors_json, ontology_version, created_at, "
            "decided_at, decided_by, reason_code) "
            "VALUES (?, ?, ?, ?, ?, ?, 'migration', ?, ?, ?, ?, 'migration', ?)",
            (
                version_id,
                claim_id,
                value_json,
                payload["direction"],
                status,
                payload["evidence_type"],
                _canonical(anchors),
                ontology.version,
                now,
                now,
                reason,
            ),
        )
        snapshot = _canonical(
            {
                "status": status,
                "value": payload["value"],
                "direction": payload["direction"],
                "evidence_type": payload["evidence_type"],
                "provenance": "migration",
                "ontology_version": ontology.version,
                "anchors": anchors,
            }
        )
        kb.execute(
            "INSERT INTO claim_events(event_type, claim_id, version_id, actor, payload_json, "
            "occurred_at) VALUES ('migrated', ?, ?, 'system', ?, ?)",
            (claim_id, version_id, snapshot, now),
        )
        kb.execute(
            "INSERT INTO kb_audit(event_seq, action, actor, detail_json, created_at) "
            "VALUES (NULL, 'migrate', 'system', ?, ?)",
            (
                _canonical(
                    {
                        "legacy_id": legacy_id,
                        "claim_id": claim_id,
                        "version_id": version_id,
                        "status": status,
                        "reason": reason,
                        "anchor_source": anchor_source,
                    }
                ),
                now,
            ),
        )
        if status == "approved":
            effective = record.get("effective_date") or now[:10]
            kb.execute(
                "UPDATE claim_versions SET effective_date = COALESCE(effective_date, ?), "
                "published_date = COALESCE(published_date, ?), "
                "retrieved_date = COALESCE(retrieved_date, ?) WHERE version_id = ?",
                (effective, record.get("published_date"), now[:10], version_id),
            )
            from .promote import _create_snapshot

            snapshot_id = _create_snapshot(kb, now, "migration", [version_id], effective)
            kb.execute(
                "UPDATE claim_events SET payload_json = ? WHERE event_type = 'migrated' "
                "AND version_id = ?",
                (
                    _canonical(
                        {
                            "status": status,
                            "value": payload["value"],
                            "direction": payload["direction"],
                            "evidence_type": payload["evidence_type"],
                            "provenance": "migration",
                            "ontology_version": ontology.version,
                            "anchors": anchors,
                            "snapshot_id": snapshot_id,
                            "effective_date": effective,
                        }
                    ),
                    version_id,
                ),
            )
            report["migrated"] += 1
            set_meta(kb, "library_version", library_version(kb) + 1)
        else:
            report["held"] += 1
        claim_map[legacy_id] = claim_id


def _migrate_edges(corpus, edges: list[dict], claim_map: dict[str, str], now: str) -> int:
    mapped = 0
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = claim_map.get(str(edge.get("source")))
        target = claim_map.get(str(edge.get("target")))
        if not (source and target):
            continue
        relation = str(edge.get("relation") or "supports")
        relation_id = _sha16(source + target + relation)
        with corpus:
            corpus.execute(
                "INSERT OR IGNORE INTO relations(relation_id, from_claim, to_claim, relation, "
                "note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    relation_id,
                    source,
                    target,
                    relation,
                    str(edge.get("description") or ""),
                    now,
                ),
            )
        mapped += 1
    return mapped


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _fail(args: Any, message: str, code: int = 1) -> int:
    if getattr(args, "json", False):
        _emit({"ok": False, "error": message})
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def run(args: Any, cfg: Config) -> int:
    try:
        ontology = load_ontology(cfg.ontology)
    except OntologyError as exc:
        return _fail(args, str(exc))

    kb, corpus = ensure_databases(cfg)
    now = now_iso()
    records, edges = _records(cfg)
    report: dict[str, Any] = {
        "ok": True,
        "migrated": 0,
        "held": 0,
        "skipped": 0,
        "failed": [],
        "edges": 0,
        "anchors_applied": 0,
    }
    overlays = _load_anchors(cfg)
    claim_map: dict[str, str] = {}
    for kind, record in records:
        record_id = str(record.get("id"))
        if not record.get("anchors") and overlays.get(record_id):
            record = {**record, "anchors": overlays[record_id]}
            report["anchors_applied"] += 1
        _migrate_record(kb, ontology, kind, record, now, report, claim_map)
    report["edges"] = _migrate_edges(corpus, edges, claim_map, now)
    report["library_version"] = library_version(kb)

    if getattr(args, "json", False):
        _emit(report)
    else:
        for key in ("migrated", "held", "skipped", "edges", "library_version"):
            print(f"{key}: {report[key]}")
        for failure in report["failed"]:
            print(f"failed {failure['id']}: {failure['reason']}")
    return 0
