"""Deterministic complexity and risk routing (ticket 18).

The router is code only: same query and case context always produce the same
path and the same reasons, so the decision is auditable and replayable.
"""

from __future__ import annotations

from typing import Any

RISK_TERMS = (
    "重症",
    "难治",
    "暴发",
    "危重",
    "休克",
    "机械通气",
    "免疫抑制",
    "婴幼儿",
    "8岁以下",
    "联合用药",
    "撤市",
    "禁忌",
)
COMPLEX_TERMS = (
    "鉴别",
    "诊断标准",
    "指南更新",
    "剂量调整",
    "疗程",
    "合并症",
    "对比",
    "差异",
    "争议",
    "多药",
)
SEVERE_VALUES = ("severe", "refractory", "fulminant", "critical")


def route(query: str, case_context: dict[str, Any] | None = None) -> dict[str, Any]:
    reasons: list[str] = []
    text = str(query or "")
    for term in RISK_TERMS:
        if term in text:
            reasons.append(f"risk:{term}")
    for term in COMPLEX_TERMS:
        if term in text:
            reasons.append(f"complex:{term}")

    context = case_context or {}
    severity = str(context.get("severity") or "")
    if severity in SEVERE_VALUES:
        reasons.append(f"case:{severity}")
    age = context.get("age_years")
    if isinstance(age, (int, float)) and not isinstance(age, bool) and age < 8:
        reasons.append("case:age_under_8")

    return {
        "path": "complex" if reasons else "simple",
        "reasons": reasons,
    }
