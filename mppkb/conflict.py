"""Deterministic conflict classification (ticket 05).

Only this module decides whether two versions describe the same knowledge in a
compatible way. The model never participates; summaries are presentation only.
"""

from __future__ import annotations

from typing import Any

SUPERSEDE = "supersede"
REFINE = "refine"
COMPATIBLE = "compatible"
CONFLICT = "conflict"


def _span(value: Any) -> tuple[str, Any]:
    if not isinstance(value, dict):
        return ("unknown", None)
    kind = value.get("kind")
    try:
        if kind == "numeric":
            number = float(value.get("value"))
            return ("interval", (number, number))
        if kind == "range":
            return ("interval", (float(value.get("low")), float(value.get("high"))))
        if kind == "direction":
            return ("direction", str(value.get("value")))
    except (TypeError, ValueError):
        return ("unknown", None)
    return ("unknown", None)


def compatible(a_value: Any, a_direction: str | None, b_value: Any, b_direction: str | None) -> bool:
    """True when the two values can coexist without a recommendation clash."""
    if a_direction or b_direction:
        return bool(a_direction and b_direction and a_direction == b_direction)
    kind_a, span_a = _span(a_value)
    kind_b, span_b = _span(b_value)
    if kind_a != kind_b or kind_a == "unknown":
        return False
    if kind_a == "direction":
        return span_a == span_b
    low_a, high_a = span_a
    low_b, high_b = span_b
    return not (high_a < low_b or high_b < low_a)


def _publishers(anchors: list[dict], papers: dict[str, dict]) -> set[str]:
    publishers: set[str] = set()
    for anchor in anchors:
        if not isinstance(anchor, dict):
            continue
        paper = papers.get(str(anchor.get("doc_id")))
        if paper and paper.get("publisher"):
            publishers.add(str(paper["publisher"]))
    return publishers


def supersede_eligible(candidate: dict, current: dict, papers: dict[str, dict]) -> bool:
    """Same-publisher, newer guideline on the same slot may replace the current version."""
    if candidate.get("evidence_type") != "guideline":
        return False
    candidate_publishers = _publishers(candidate.get("anchors") or [], papers)
    current_publishers = _publishers(current.get("anchors") or [], papers)
    if not candidate_publishers or not current_publishers:
        return False
    if candidate_publishers != current_publishers:
        return False
    return str(candidate.get("effective_date") or candidate.get("created_at")) > str(
        current.get("effective_date") or current.get("created_at")
    )


def classify_claim(
    proposed: list[dict],
    current: dict | None,
    papers: dict[str, dict],
) -> dict[str, str]:
    """Return ``version_id -> category`` for all proposed versions of one claim."""
    categories: dict[str, str] = {}
    for version in proposed:
        if current is not None:
            if supersede_eligible(version, current, papers):
                categories[version["version_id"]] = SUPERSEDE
            elif compatible(
                version.get("value"),
                version.get("direction"),
                current.get("value"),
                current.get("direction"),
            ):
                categories[version["version_id"]] = REFINE
            else:
                categories[version["version_id"]] = CONFLICT
        else:
            categories[version["version_id"]] = COMPATIBLE

    # Intra-batch contradictions override any single-sided verdict.
    for index, first in enumerate(proposed):
        for second in proposed[index + 1 :]:
            if not compatible(
                first.get("value"),
                first.get("direction"),
                second.get("value"),
                second.get("direction"),
            ):
                categories[first["version_id"]] = CONFLICT
                categories[second["version_id"]] = CONFLICT
    return categories
