"""Claim scope: treatment KB by default, lab thresholds behind a switch (ticket 32 A)."""

from __future__ import annotations

TREATMENT_ATTRIBUTES = frozenset(
    {
        "ATTR:DOSE",
        "ATTR:DURATION",
        "ATTR:FIRST_LINE",
        "ATTR:SECOND_LINE",
        "ATTR:CONTRAINDICATION",
        "ATTR:COMBINATION",
        "ATTR:FOLLOW_UP",
        "ATTR:ADVERSE_EVENT",
        "ATTR:GLUCOCORTICOID_USE",
        "ATTR:ANTIBIOTIC_ESCALATION",
        "ATTR:RESISTANCE",
        "ATTR:DISCHARGE_CRITERIA",
        "ATTR:REFERRAL",
        "ATTR:EVIDENCE_SUMMARY",
        "ATTR:REFRACTORY_RISK",
        "ATTR:TREATMENT_PRINCIPLE",
    }
)

LAB_ATTRIBUTES = frozenset(
    {
        "ATTR:CRP",
        "ATTR:LDH",
        "ATTR:PCT",
        "ATTR:BLOOD_GLUCOSE",
        "ATTR:WBC",
        "ATTR:NEUTROPHIL",
        "ATTR:PLATELET",
        "ATTR:BLOOD_GAS",
        "ATTR:ESR",
        "ATTR:ALBUMIN",
        "ATTR:ALT",
        "ATTR:AST",
        "ATTR:D_DIMER",
        "ATTR:OXYGEN_SATURATION",
        "ATTR:MP_IGM",
        "ATTR:MP_PCR",
    }
)

TREATMENT_LABELS = "一线治疗、二线治疗、剂量、疗程、禁忌、联合治疗、随访、不良反应、抗生素升级、耐药"
LAB_LABELS = "C反应蛋白、乳酸脱氢酶、降钙素原、血糖、白细胞计数、中性粒细胞、血小板、血气分析、红细胞沉降率、白蛋白、ALT、AST、D-二聚体、MP-IgM、MP-PCR"


def attribute_scope(attribute_id: str | None) -> str:
    if attribute_id and attribute_id.startswith(("ATTR:AE_", "ATTR:DOSE_")):
        return "treatment"
    if attribute_id in TREATMENT_ATTRIBUTES:
        return "treatment"
    if attribute_id in LAB_ATTRIBUTES:
        return "lab"
    return "other"


def allow_attribute(attribute_id: str | None, include_labs: bool) -> bool:
    scope = attribute_scope(attribute_id)
    if scope == "treatment":
        return True
    return include_labs and scope == "lab"
