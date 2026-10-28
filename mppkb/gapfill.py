"""Gap fill: safe retrieval for missing treatment element groups.

The fill is deliberately read-only. It never promotes hot claims or turns a
document passage into a cold claim; it surfaces:
- hot candidates that need review, and
- raw corpus passages that can be reviewed as source leads.

The answer layer decides how to present these; nothing here mutates the KB.
"""

from __future__ import annotations

import json
from typing import Any

GROUP_ATTRIBUTES = {
    "定位": ("ATTR:FIRST_LINE", "ATTR:SECOND_LINE"),
    "剂量": (
        "ATTR:DOSE",
        "ATTR:DOSE_PER_KG",
        "ATTR:DOSE_PER_KG_PER_DAY",
        "ATTR:DOSE_FIXED",
        "ATTR:DOSE_FIXED_PER_DAY",
        "ATTR:DOSE_VOLUME",
        "ATTR:DOSE_VOLUME_PER_KG",
    ),
    "疗程": ("ATTR:DURATION",),
    "联合": ("ATTR:COMBINATION",),
    "禁忌/安全": ("ATTR:CONTRAINDICATION",),
    "用法": ("ATTR:ROUTE", "ATTR:GLUCOCORTICOID_USE"),
}

GROUP_TERMS = {
    "定位": ("首选", "一线", "二线", "备选"),
    "剂量": ("剂量", "mg", "按体重"),
    "疗程": ("疗程", "天"),
    "联合": ("联合", "合用", "联用"),
    "禁忌/安全": ("禁忌", "慎用", "不良反应"),
    "用法": ("用法", "给药", "口服", "静脉", "静滴", "频次"),
}

_SOURCE_CACHE: dict[tuple, list[dict[str, Any]]] = {}


def _value_text(value: Any, direction: str | None) -> str:
    if direction:
        return str(direction)
    if not isinstance(value, dict):
        return str(value)
    kind = value.get("kind")
    if kind in ("numeric", "text"):
        text = str(value.get("value"))
        unit = str(value.get("unit") or "")
        return f"{text} {unit}".strip()
    if kind == "range":
        text = f"{value.get('low')}–{value.get('high')}"
        unit = str(value.get("unit") or "")
        return f"{text} {unit}".strip()
    if kind == "direction":
        return str(value.get("value"))
    return json.dumps(value, ensure_ascii=False)


def _trim_quote(text: str, terms: list[str], limit: int = 220) -> str:
    clean = " ".join(str(text or "").split())
    positions = [clean.find(term) for term in terms if term and clean.find(term) >= 0]
    start = min(positions) if positions else 0
    start = max(0, start - 60)
    quote = clean[start : start + limit]
    return quote + "…" if start + limit < len(clean) else quote


def _hot_candidates(
    kb, drug_id: str, groups: list[str], limit: int = 3
) -> list[dict[str, Any]]:
    attributes = [attr for group in groups for attr in GROUP_ATTRIBUTES.get(group, ())]
    if not attributes:
        return []
    placeholders = ",".join("?" for _ in attributes)
    rows = kb.execute(
        "SELECT v.version_id, v.claim_id, v.value_json, v.direction, v.status, "
        "v.evidence_type, v.anchors_json, c.attribute "
        "FROM claim_versions v JOIN claims c ON c.claim_id = v.claim_id "
        "WHERE v.status IN ('proposed', 'conflicted') AND c.intervention = ? "
        f"AND c.attribute IN ({placeholders}) LIMIT ?",
        (drug_id, *attributes, limit),
    ).fetchall()
    results = []
    for row in rows:
        anchors = json.loads(row["anchors_json"] or "[]")
        anchor = anchors[0] if anchors else {}
        results.append(
            {
                "version_id": row["version_id"],
                "claim_id": row["claim_id"],
                "status": row["status"],
                "attribute": row["attribute"],
                "value": _value_text(json.loads(row["value_json"] or "{}"), row["direction"]),
                "doc_id": anchor.get("doc_id"),
                "page": anchor.get("page_start"),
                "quote": _trim_quote(
                    str(anchor.get("quote") or ""), GROUP_TERMS.get(groups[0], [])
                ),
            }
        )
    return results


def _source_hints(
    corpus,
    drug_label: str,
    groups: list[str],
    limit: int = 2,
) -> list[dict[str, Any]]:
    if not drug_label:
        return []
    cache_key = (id(corpus), drug_label, tuple(groups), limit)
    if cache_key in _SOURCE_CACHE:
        return list(_SOURCE_CACHE[cache_key])
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for group in groups:
        for term in GROUP_TERMS.get(group, ()):
            if len(results) >= limit:
                return results
            rows = corpus.execute(
                "SELECT c.chunk_id, c.paper_id, c.section_path, c.page_start, c.page_end, c.text, "
                "p.title, p.year, p.publisher, p.evidence_type "
                "FROM chunks_fts f JOIN chunks c ON c.chunk_id = f.chunk_id "
                "LEFT JOIN papers p ON p.paper_id = c.paper_id "
                "WHERE chunks_fts MATCH ? LIMIT 60",
                (term,),
            ).fetchall()
            for row in rows:
                if row["chunk_id"] in seen:
                    continue
                if drug_label not in str(row["text"] or ""):
                    continue
                seen.add(row["chunk_id"])
                results.append(
                    {
                        "chunk_id": row["chunk_id"],
                        "doc_id": row["paper_id"],
                        "title": row["title"],
                        "year": row["year"],
                        "publisher": row["publisher"],
                        "evidence_type": row["evidence_type"],
                        "section_path": row["section_path"],
                        "page": row["page_start"],
                        "quote": _trim_quote(row["text"], [drug_label, term]),
                        "quote_full": str(row["text"] or ""),
                    }
                )
                if len(results) >= limit:
                    _SOURCE_CACHE[cache_key] = list(results)
                    return results
    _SOURCE_CACHE[cache_key] = list(results)
    return results


def fill_gaps(
    kb,
    corpus,
    service,
    gap_details: list[dict[str, Any]],
    limit: int = 2,
) -> list[dict[str, Any]]:
    """Return safe fill options for up to ``limit`` gap entries."""
    fills: list[dict[str, Any]] = []
    for detail in gap_details[:limit]:
        drug_id = str(detail.get("drug_id") or "")
        groups = list(detail.get("missing") or [])
        drug_label = str(detail.get("drug") or "")
        if not drug_id or not groups:
            continue
        hot = _hot_candidates(kb, drug_id, groups)
        sources = _source_hints(corpus, drug_label, groups)
        if hot:
            action = "hot_pending"
            message = f"热库有 {len(hot)} 条待审补充；审核通过前不进入回答"
        elif sources:
            action = "source_hint"
            message = f"原文有 {len(sources)} 条线索，尚未形成 claim；建议补审"
        else:
            action = "no_source"
            message = "冷库、热库和原文均未检出可直接补入的条目"
        fills.append(
            {
                "drug": drug_label,
                "drug_id": drug_id,
                "missing": groups,
                "action": action,
                "message": message,
                "hot_candidates": hot,
                "source_hints": sources,
            }
        )
    return fills
