"""Evidence service and answer synthesis (ticket 09).

The answer is built only from approved cold claims. Retrieval is deterministic
(2-gram overlap over claim labels, values and anchor quotes), synthesis is rule
based, and every conclusion carries a page-level citation. Hot (unapproved)
versions never enter the body; they can only appear as an explicit note.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any

from .config import Config
from .conflict import _span, compatible
from .db import connect, ensure_databases, get_meta, library_version
from .gapfill import fill_gaps
from .ontology import load_ontology
from .routing import route
from .text import to_grams
from .clock import now_iso

DISCLAIMER = "本回答为研究演示，不构成临床决策依据。"
DIRECTION_LABELS = {"first_line": "首选", "alternative": "备选", "avoid": "避免"}
SECTION_ORDER = ("评估", "严重度", "治疗", "随访")
SECTION_BY_ATTRIBUTE = {
    "ATTR:DIAGNOSTIC_CRITERIA": "评估",
    "ATTR:IMAGING": "评估",
    "ATTR:CLINICAL_MANIFESTATION": "评估",
    "ATTR:LAB_TEST": "评估",
    "ATTR:SEVERITY_ASSESSMENT": "严重度",
    "ATTR:REFRACTORY_RISK": "严重度",
    "ATTR:FOLLOW_UP": "随访",
    "ATTR:DISCHARGE_CRITERIA": "随访",
    "ATTR:EVIDENCE_SUMMARY": "治疗",
    "ATTR:TREATMENT_PRINCIPLE": "治疗",
}
TREATMENT_ATTRIBUTES = frozenset(
    {
        "ATTR:FIRST_LINE",
        "ATTR:SECOND_LINE",
        "ATTR:CONTRAINDICATION",
        "ATTR:COMBINATION",
        "ATTR:DOSE",
        "ATTR:DURATION",
        "ATTR:ROUTE",
        "ATTR:TREATMENT_PRINCIPLE",
        "ATTR:EVIDENCE_SUMMARY",
    }
)
ASSESS_ATTRIBUTES = frozenset(
    {
        "ATTR:DIAGNOSTIC_CRITERIA",
        "ATTR:IMAGING",
        "ATTR:CLINICAL_MANIFESTATION",
        "ATTR:LAB_TEST",
        "ATTR:SEVERITY_ASSESSMENT",
        "ATTR:REFRACTORY_RISK",
    }
)
FOLLOWUP_ATTRIBUTES = frozenset({"ATTR:FOLLOW_UP", "ATTR:DISCHARGE_CRITERIA"})
TREATMENT_ELEMENT_GROUPS = {
    "ATTR:FIRST_LINE": "定位",
    "ATTR:SECOND_LINE": "定位",
    "ATTR:DOSE": "剂量",
    "ATTR:DOSE_PER_KG": "剂量",
    "ATTR:DOSE_PER_KG_PER_DAY": "剂量",
    "ATTR:DOSE_FIXED": "剂量",
    "ATTR:DOSE_FIXED_PER_DAY": "剂量",
    "ATTR:DOSE_VOLUME": "剂量",
    "ATTR:DOSE_VOLUME_PER_KG": "剂量",
    "ATTR:DURATION": "疗程",
    "ATTR:COMBINATION": "联合",
    "ATTR:CONTRAINDICATION": "禁忌/安全",
    "ATTR:ROUTE": "用法",
    "ATTR:GLUCOCORTICOID_USE": "用法",
}
TREATMENT_ELEMENT_ORDER = ("定位", "剂量", "疗程", "联合", "禁忌/安全", "用法")
EVIDENCE_RANK = {
    "guideline": 4,
    "systematic_review": 3,
    "randomized_controlled_trial": 2,
    "observational_study": 1,
    "narrative_review": 0,
    "case_report": 0,
}


def _naturalize_summary_text(text: str) -> str:
    text = str(text or "").strip().rstrip("。；;")
    for suffix, replacement in (
        ("：避免", "应避免"),
        ("：首选", "为首选"),
        ("：备选", "为备选"),
    ):
        if text.endswith(suffix):
            return text[: -len(suffix)] + replacement
    if "：" in text:
        text = text.replace("：", "为", 1)
    return text


def _build_conclusion(
    items: list[dict[str, Any]], p0_warning: dict[str, Any] | None
) -> str:
    """Deterministic, citation-preserving conclusion summary.

    Groups approved claims into plain-language buckets instead of exposing a
    semicolon-joined fragment list.
    """
    if p0_warning:
        template = str(p0_warning.get("template") or "").strip()
        return (
            f"安全警示：{template}（P0 安全例外）。为避免风险，本次不输出治疗推荐；"
            "请先核对患者年龄/禁忌与原文。"
        )
    if not items:
        return "没有足够的已审核证据形成结论。"
    groups: dict[str, list[dict[str, Any]]] = {
        "评估方面": [],
        "治疗方面": [],
        "安全性方面": [],
        "随访方面": [],
    }
    for item in items:
        attribute = str(item.get("attribute") or "")
        if attribute in ASSESS_ATTRIBUTES:
            groups["评估方面"].append(item)
        elif attribute in FOLLOWUP_ATTRIBUTES:
            groups["随访方面"].append(item)
        elif attribute in TREATMENT_ATTRIBUTES:
            groups["治疗方面"].append(item)
        elif attribute.startswith("ATTR:AE_") or attribute == "ATTR:ADVERSE_EVENT":
            groups["安全性方面"].append(item)
        else:
            groups["治疗方面"].append(item)
    lines: list[str] = ["综合当前已审核证据，主要结论如下："]
    for label in ("评估方面", "治疗方面", "安全性方面", "随访方面"):
        group = groups[label]
        if not group:
            continue
        parts = []
        for item in group[:4]:
            sentence = _naturalize_summary_text(item.get("text") or "")
            if not sentence:
                continue
            parts.append(f"{sentence}[Source[{item.get('source_no')}]]")
        if len(group) > 4 and parts:
            parts[-1] = parts[-1].rstrip("。") + "等"
        if parts:
            lines.append(f"{label}：" + "；".join(parts) + "。")
    if not lines:
        return "没有足够的已审核证据形成结论。"
    lines.append("以上为当前已审核证据的自动总结；具体诊疗需结合患者情况并核对原文。")
    return "\n".join(lines)


def _snapshot_label(snapshot_id: str | None) -> str:
    if not snapshot_id:
        return "未记录"
    raw = str(snapshot_id)
    if raw.startswith("SNAP-"):
        raw = raw[5:]
    return f"快照 {raw[:8]}"


def _status_summary(
    version: int,
    hot_updates: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    gap_count: int = 0,
) -> str:
    parts = [f"知识库 v{version}", "冷库已审核"]
    if hot_updates:
        parts.append(f"{len(hot_updates)} 条热库待审")
    elif conflicts:
        parts.append(f"{len(conflicts)} 处冲突")
    else:
        parts.append("无待审更新")
    if gap_count:
        parts.append(f"{gap_count} 项证据缺口")
    return " · ".join(parts)


GAP_SUGGESTIONS = {
    "定位": "查一线/二线、首选/备选的适用场景",
    "剂量": "查按体重或固定日剂量、年龄上限",
    "疗程": "查疗程天数与停药指征",
    "联合": "查联合用药方案与先后顺序",
    "禁忌/安全": "查禁忌人群、不良反应与监测",
    "用法": "查给药途径、频次和配制方法",
}
CITATION_EXEMPT_PREFIXES = (
    "综合当前",
    "以上为",
    "具体诊疗",
    "没有足够",
    "安全警示",
    "已找到该药",
    "提示：",
)


def _evidence_gap_details(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    details = []
    for element in elements:
        missing = list(element.get("missing") or [])
        if not missing:
            continue
        details.append(
            {
                "drug": element.get("drug"),
                "drug_id": element.get("drug_id"),
                "missing": missing,
                "suggestion": "；".join(
                    f"{group}（{GAP_SUGGESTIONS.get(group, '查原文')}）" for group in missing
                ),
            }
        )
    return details


def _citation_check(conclusion: str, citation_nos: set[int]) -> dict[str, Any]:
    """Check every evidence-bearing conclusion sentence has a real Source.

    Boilerplate and safety sentences are explicitly exempt; the check reports
    unsupported sentences and dangling Source numbers instead of hiding them.
    """
    raw = str(conclusion or "").strip()
    segments = [part.strip() for part in re.split(r"[。；\n]+", raw) if part.strip()]
    checked = [seg for seg in segments if not seg.startswith(CITATION_EXEMPT_PREFIXES)]
    unsupported: list[str] = []
    invalid: list[str] = []
    cited = 0
    for segment in checked:
        refs = [int(value) for value in re.findall(r"Source\[(\d+)\]", segment)]
        if not refs:
            unsupported.append(segment)
            continue
        cited += 1
        for ref in refs:
            if ref not in citation_nos:
                invalid.append(f"Source[{ref}]")
    total = len(checked)
    coverage = round(cited / total, 4) if total else 1.0
    return {
        "sentences": total,
        "cited": cited,
        "coverage": coverage,
        "unsupported": unsupported,
        "invalid_refs": invalid,
        "defects": len(unsupported) + len(invalid),
    }


def _evidence_status_block(
    version: int,
    snapshot_id: str | None,
    retrieval: dict[str, Any],
    hot_updates: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    evidence_gaps: list[str],
    gap_details: list[dict[str, Any]] | None = None,
    citation_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gap_details = gap_details or []
    return {
        "status": (
            "pending_updates"
            if hot_updates
            else "conflicts"
            if conflicts
            else "current"
        ),
        "summary": _status_summary(version, hot_updates, conflicts, len(gap_details)),
        "library_version": version,
        "snapshot_id": snapshot_id,
        "snapshot_label": _snapshot_label(snapshot_id),
        "retrieval_mode": retrieval.get("retrieval_mode", "lexical"),
        "candidate_claims": len(retrieval.get("items") or []),
        "hot_updates": hot_updates,
        "hot_count": len(hot_updates),
        "conflicts": conflicts,
        "conflict_count": len(conflicts),
        "evidence_gaps": evidence_gaps,
        "gap_count": len(gap_details),
        "gap_details": gap_details,
        "citation_check": citation_check or {},
    }


def _detect_conflicts(
    claims: list[dict[str, Any]], service: "EvidenceService"
) -> list[dict[str, Any]]:
    """Find incompatible claims on the same slot and context.

    Text-only values are not compared: the deterministic conflict module only
    has meaningful semantics for numeric, range and direction values.
    """
    conflicts: list[dict[str, Any]] = []
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for claim in claims:
        key = (
            claim.get("intervention"),
            claim.get("attribute"),
            claim.get("population"),
            claim.get("severity"),
        )
        groups.setdefault(key, []).append(claim)
    for key, items in groups.items():
        for index, first in enumerate(items):
            first_kind, _ = _span(first.get("value"))
            if first_kind == "unknown":
                continue
            for second in items[index + 1 :]:
                second_kind, _ = _span(second.get("value"))
                if second_kind == "unknown":
                    continue
                if compatible(
                    first.get("value"),
                    first.get("direction"),
                    second.get("value"),
                    second.get("direction"),
                ):
                    continue
                conflicts.append(
                    {
                        "intervention": key[0],
                        "intervention_label": service._label(key[0]),
                        "attribute": key[1],
                        "attribute_label": service._label(key[1]),
                        "population": key[2],
                        "population_label": service._label(key[2]),
                        "severity": key[3],
                        "severity_label": service._label(key[3]),
                        "value_a": _value_text(first.get("value") or {}, first.get("direction")),
                        "value_b": _value_text(second.get("value") or {}, second.get("direction")),
                        "claim_a": first.get("claim_id"),
                        "claim_b": second.get("claim_id"),
                        "source_a": first.get("source_no"),
                        "source_b": second.get("source_no"),
                    }
                )
    return conflicts


def _missing_treatment_groups(
    service: "EvidenceService", query: str, items: list[dict[str, Any]]
) -> list[str]:
    drugs = service._mentioned_drugs(query)
    if not drugs:
        return []
    present = {
        TREATMENT_ELEMENT_GROUPS.get(str(item["row"].get("attribute") or ""))
        for item in items
    }
    return [group for group in TREATMENT_ELEMENT_ORDER if group not in present]


EXPANSION_TERMS = {
    "定位": "一线 二线",
    "剂量": "剂量",
    "疗程": "疗程",
    "联合": "联合用药",
    "禁忌/安全": "禁忌",
    "用法": "用法",
}
GROUP_QUERY_TERMS = {
    "定位": ("一线", "二线", "首选", "备选", "定位", "选择", "地位"),
    "剂量": ("剂量", "用量", "按体重", "mg"),
    "疗程": ("疗程", "天数", "几天", "用药时间"),
    "联合": ("联合", "合用", "联用"),
    "禁忌/安全": ("禁忌", "安全", "注意", "慎用", "不良反应"),
    "用法": ("用法", "给药", "口服", "静脉", "静滴", "频次"),
}


def _requested_treatment_groups(query: str) -> list[str]:
    """Infer which treatment element groups the question explicitly needs."""
    requested = [
        group
        for group in TREATMENT_ELEMENT_ORDER
        if any(term in query for term in GROUP_QUERY_TERMS[group])
    ]
    if not requested and any(term in query for term in ("治疗", "用药", "抗感染", "方案")):
        requested = list(TREATMENT_ELEMENT_ORDER)
    return requested


def _planned_query(query: str) -> tuple[str, list[str]]:
    groups = _requested_treatment_groups(query)
    terms = [EXPANSION_TERMS[group] for group in groups if group in EXPANSION_TERMS]
    if not terms:
        return query, []
    return f"{query} {' '.join(terms)}".strip(), groups


def _merge_retrieval(primary: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    seen = {str(item["row"]["version_id"]) for item in primary.get("items") or []}
    merged = list(primary.get("items") or [])
    for item in extra.get("items") or []:
        version_id = str(item["row"]["version_id"])
        if version_id in seen:
            continue
        seen.add(version_id)
        merged.append(item)
    return {**primary, "items": merged}


def _collect_treatment_elements(
    kb,
    service,
    query: str,
    existing_versions: set[str],
    start_source_no: int,
    fallback_drugs: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect per-drug treatment elements from approved claims.

    This is a structured evidence summary, not a prescription: it lists what
    the cold library knows and explicitly reports missing element groups.
    If the question does not name a drug, the top retrieved drugs are used as
    a fallback so a general treatment question still shows concrete elements.
    """
    drugs = sorted(service._mentioned_drugs(query))
    if not drugs and fallback_drugs:
        drugs = list(dict.fromkeys(fallback_drugs))[:3]
    if not drugs:
        return [], [], []
    attributes = list(TREATMENT_ELEMENT_GROUPS)
    drug_slots = ",".join("?" for _ in drugs)
    attr_slots = ",".join("?" for _ in attributes)
    rows = kb.execute(
        "SELECT v.version_id, v.value_json, v.direction, v.evidence_type, "
        "v.anchors_json, c.claim_id, c.population, c.severity, c.intervention, c.attribute "
        "FROM claim_versions v JOIN claims c ON c.claim_id = v.claim_id "
        f"WHERE v.status = 'approved' AND c.intervention IN ({drug_slots}) "
        f"AND c.attribute IN ({attr_slots})",
        tuple(drugs) + tuple(attributes),
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    citations: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    source_no = start_source_no
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        version_id = row["version_id"]
        if version_id in existing_versions:
            continue
        attribute = str(row["attribute"])
        group = TREATMENT_ELEMENT_GROUPS.get(attribute)
        if group is None:
            continue
        anchors = json.loads(row["anchors_json"] or "[]")
        if not anchors or not isinstance(anchors[0], dict):
            continue
        anchor = anchors[0]
        if not anchor.get("doc_id") or not anchor.get("page_start"):
            continue
        drug_id = str(row["intervention"])
        value = json.loads(row["value_json"] or "{}")
        text = (
            f"{service._label(drug_id)}{service._label(attribute)}："
            f"{_value_text(value, row['direction'])}"
        )
        context_parts = [
            service._label(row["population"]),
            service._label(row["severity"]),
        ]
        context = "·".join(part for part in context_parts if part)
        dedupe_key = (drug_id, group, text, context)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        entry = {
            "text": text,
            "value": _value_text(value, row["direction"]),
            "context": context,
            "group": group,
            "source_no": source_no,
            "evidence_type": row["evidence_type"],
            "attribute": attribute,
        }
        bucket = grouped.setdefault(
            drug_id,
            {"drug": service._label(drug_id), "drug_id": drug_id, "groups": {}},
        )
        bucket["groups"].setdefault(group, []).append(entry)
        citations.append(
            {
                "source_no": source_no,
                "doc_id": anchor.get("doc_id"),
                "page": anchor.get("page_start"),
                "claim_id": row["claim_id"],
            }
        )
        details.append(
            {
                "source_no": source_no,
                "text": text,
                "claim_id": row["claim_id"],
                "doc_id": anchor.get("doc_id"),
                "page": anchor.get("page_start"),
                "quote": anchor.get("quote") or "",
                "evidence_type": row["evidence_type"],
            }
        )
        source_no += 1
    elements: list[dict[str, Any]] = []
    for drug_id, bucket in grouped.items():
        scenario_map: dict[str, dict[str, Any]] = {}
        for group, items in bucket["groups"].items():
            for item in items:
                context = str(item.get("context") or "").strip() or "人群/严重度未标注"
                scenario = scenario_map.setdefault(
                    context, {"context": context, "groups": {}}
                )
                scenario["groups"].setdefault(group, []).append(item)
        scenarios = []
        used_groups: set[str] = set()
        for context, scenario in scenario_map.items():
            groups = {}
            for group in TREATMENT_ELEMENT_ORDER:
                items = scenario["groups"].get(group, [])
                items.sort(
                    key=lambda item: (
                        -EVIDENCE_RANK.get(str(item.get("evidence_type") or ""), 0),
                        item.get("source_no") or 0,
                    )
                )
                if items:
                    groups[group] = items[:2]
                    used_groups.add(group)
            if groups:
                scenarios.append({"context": context, "groups": groups})

        def scenario_priority(item: dict[str, Any]) -> int:
            context = str(item.get("context") or "")
            if "重症" in context:
                return 0
            if "难治" in context:
                return 1
            if "轻症" in context:
                return 2
            return 3

        scenarios.sort(key=lambda item: (scenario_priority(item), item["context"]))
        missing = [group for group in TREATMENT_ELEMENT_ORDER if group not in used_groups]
        if scenarios:
            elements.append(
                {
                    "drug": bucket["drug"],
                    "drug_id": drug_id,
                    "scenarios": scenarios,
                    "missing": missing,
                }
            )
    return elements, citations, details

RETRIEVAL_MODEL = "fts5"
RETRIEVAL_VERSION = "3"


def _retrieval_signature(cfg: Config, kb) -> str:
    mode = os.environ.get("MPPKB_RETRIEVAL_MODE", "lexical").strip().lower() or "lexical"
    index_hash = get_meta(kb, "index_corpus_hash") or "none"
    return f"v{RETRIEVAL_VERSION}:{mode}:{cfg.embedding_model}:{index_hash}"


def _cache_key(
    query: str, case_hash: str, version: int, signature: str, top_k: int = 5
) -> str:
    raw = f"{query}|{case_hash}|{version}|{RETRIEVAL_MODEL}|{signature}|{top_k}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _cache_get(kb, cache_key: str, version: int) -> dict[str, Any] | None:
    row = kb.execute(
        "SELECT value_json FROM retrieval_cache WHERE cache_key = ? AND library_version = ?",
        (cache_key, version),
    ).fetchone()
    if row is None:
        return None
    with kb:
        kb.execute(
            "UPDATE retrieval_cache SET hit_count = hit_count + 1 WHERE cache_key = ?",
            (cache_key,),
        )
    return json.loads(row["value_json"])


def _cache_set(
    kb, cache_key: str, version: int, value: dict[str, Any], signature: str
) -> None:
    with kb:
        kb.execute("DELETE FROM retrieval_cache WHERE library_version <> ?", (version,))
        kb.execute(
            "INSERT OR REPLACE INTO retrieval_cache(cache_key, cache_type, value_json, "
            "library_version, embedding_model, created_at, expires_at, hit_count) "
            "VALUES (?, 'retrieval', ?, ?, ?, ?, NULL, 0)",
            (
                cache_key,
                json.dumps(value, ensure_ascii=False),
                version,
                signature,
                now_iso(),
            ),
        )


def _active_snapshot(kb) -> str | None:
    row = kb.execute(
        "SELECT snapshot_id FROM kb_snapshots WHERE library = 'cold' AND status = 'active' "
        "ORDER BY created_at DESC, snapshot_id DESC LIMIT 1"
    ).fetchone()
    return row["snapshot_id"] if row is not None else None


def _log_answer(
    kb,
    case_id: str | None,
    question_hash: str,
    snapshot_id: str | None,
    result: dict[str, Any],
    question: str | None = None,
) -> None:
    if os.environ.get("MPPKB_STORE_ANSWER_QUESTION", "1") == "0":
        question = None
    with kb:
        kb.execute(
            "INSERT INTO answer_log(case_id, question_hash, question, snapshot_id, "
            "record_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                case_id,
                question_hash,
                question,
                snapshot_id,
                json.dumps(result, ensure_ascii=False),
                now_iso(),
            ),
        )


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _grams(text: str) -> set[str]:
    return set(to_grams(text).split())


def _value_text(value: dict[str, Any], direction: str | None) -> str:
    if direction:
        return DIRECTION_LABELS.get(direction, direction)
    kind = value.get("kind")
    if kind == "numeric":
        unit = value.get("unit") or ""
        return f"{value.get('value')}{unit}"
    if kind == "range":
        unit = value.get("unit") or ""
        return f"{value.get('low')}–{value.get('high')}{unit}"
    if kind == "direction":
        return DIRECTION_LABELS.get(str(value.get("value")), str(value.get("value")))
    if kind == "text":
        return str(value.get("value") or "")
    return _canonical(value)


class EvidenceService:
    def __init__(self, kb, cfg: Config):
        self.kb = kb
        self.cfg = cfg
        try:
            self.ontology = load_ontology(cfg.ontology)
        except Exception:  # noqa: BLE001 - degrade to ids when the vocabulary is missing
            self.ontology = None
        self._labels = (
            {term.term_id: term.label for term in self.ontology.terms}
            if self.ontology is not None
            else {}
        )
        self._drug_aliases: dict[str, list[str]] = {}
        if self.ontology is not None:
            for term in self.ontology.terms:
                if getattr(term, "kind", "") != "drug":
                    continue
                names = [getattr(term, "label", "") or ""]
                names.extend(getattr(term, "aliases", []) or [])
                self._drug_aliases[term.term_id] = [
                    str(name).strip().lower() for name in names if str(name).strip()
                ]

    def _mentioned_drugs(self, query: str) -> set[str]:
        text = str(query or "").lower()
        return {
            term_id
            for term_id, aliases in self._drug_aliases.items()
            if any(alias and alias in text for alias in aliases)
        }

    def _label(self, term_id: str | None) -> str:
        if not term_id:
            return ""
        return self._labels.get(term_id, term_id)

    def _claim_text(self, claim: dict[str, Any], anchors: list[dict]) -> str:
        parts = [
            self._label(claim["population"]),
            self._label(claim["severity"]),
            self._label(claim["intervention"]),
            self._label(claim["attribute"]),
            _value_text(json.loads(claim["value_json"]), claim["direction"]),
        ]
        parts.extend(str(anchor.get("quote") or "") for anchor in anchors)
        return " ".join(parts)

    def _scored(self, status: str, query_grams: set[str]) -> list[dict[str, Any]]:
        rows = self.kb.execute(
            "SELECT v.*, c.population, c.severity, c.intervention, c.attribute "
            "FROM claim_versions v JOIN claims c ON c.claim_id = v.claim_id "
            "WHERE v.status = ?",
            (status,),
        ).fetchall()
        scored: list[dict[str, Any]] = []
        for row in rows:
            claim = dict(row)
            anchors = json.loads(row["anchors_json"])
            grams = _grams(self._claim_text(claim, anchors))
            score = len(query_grams & grams)
            if score <= 0:
                continue
            scored.append({"row": claim, "anchors": anchors, "score": score})
        scored.sort(key=lambda item: (-item["score"], item["row"]["claim_id"]))
        return scored

    def items_by_ids(self, version_ids: list[str]) -> list[dict[str, Any]]:
        if not version_ids:
            return []
        placeholders = ",".join("?" for _ in version_ids)
        rows = self.kb.execute(
            "SELECT v.*, c.population, c.severity, c.intervention, c.attribute "
            "FROM claim_versions v JOIN claims c ON c.claim_id = v.claim_id "
            f"WHERE v.version_id IN ({placeholders})",
            tuple(version_ids),
        ).fetchall()
        by_id = {row["version_id"]: row for row in rows}
        items: list[dict[str, Any]] = []
        for version_id in version_ids:
            row = by_id.get(version_id)
            if row is None:
                continue
            items.append(
                {
                    "row": dict(row),
                    "anchors": json.loads(row["anchors_json"]),
                    "score": 1,
                }
            )
        return items

    def _rrf(self, lexical: list[dict[str, Any]], vector: list[dict[str, Any]], k: int = 60) -> list[dict[str, Any]]:
        scores: dict[str, float] = {}
        items: dict[str, dict[str, Any]] = {}
        for rank, item in enumerate(lexical, start=1):
            version_id = item["row"]["version_id"]
            scores[version_id] = scores.get(version_id, 0.0) + 1.0 / (k + rank)
            items[version_id] = item
        for rank, item in enumerate(vector, start=1):
            version_id = item["row"]["version_id"]
            scores[version_id] = scores.get(version_id, 0.0) + 1.0 / (k + rank)
            items.setdefault(version_id, item)
        ordered = sorted(scores, key=lambda version_id: (-scores[version_id], version_id))
        return [items[version_id] for version_id in ordered]

    def _hybrid_current(self, query: str, query_grams: set[str], top_k: int = 5) -> dict[str, Any]:
        lexical = self._scored("approved", query_grams)
        vector_items: list[dict[str, Any]] = []
        degraded = False
        reasons: list[str] = []
        try:
            from . import index as index_module

            result = index_module.search(self.kb, self.cfg, query, "cold", top_k=max(10, top_k * 2))
            if result.get("degraded"):
                degraded = True
                reasons.append(str(result.get("reason") or "index_degraded"))
            version_ids = [
                str(item.get("version_id"))
                for item in result.get("items", [])
                if item.get("version_id")
            ]
            vector_items = self.items_by_ids(version_ids)
        except Exception as exc:  # noqa: BLE001 - retrieval must degrade, not fail
            degraded = True
            reasons.append(f"index_error:{exc}")

        if not lexical and not vector_items:
            return {
                "evidence_status": "current",
                "items": [],
                "refused": True,
                "retrieval_mode": "hybrid",
                "retrieval_degraded": degraded,
                "reasons": reasons,
            }
        fused = self._rrf(lexical, vector_items)[:top_k] or lexical[:top_k] or vector_items[:top_k]
        return {
            "evidence_status": "current",
            "items": fused,
            "refused": False,
            "retrieval_mode": "hybrid",
            "retrieval_degraded": degraded,
            "reasons": reasons,
        }

    def retrieve(self, query: str, top_k: int = 5) -> dict[str, Any]:
        mode = os.environ.get("MPPKB_RETRIEVAL_MODE", "lexical").strip().lower() or "lexical"
        query_grams = _grams(query)
        if mode == "hybrid":
            hybrid = self._hybrid_current(query, query_grams, top_k=top_k)
            if hybrid["items"] or hybrid["refused"]:
                return hybrid

        current = self._scored("approved", query_grams)[:top_k]
        if current:
            return {
                "evidence_status": "current",
                "items": current,
                "refused": False,
                "retrieval_mode": "lexical",
                "retrieval_degraded": False,
                "reasons": [],
            }
        superseded = self._scored("superseded", query_grams)[:top_k]
        if superseded:
            return {
                "evidence_status": "superseded",
                "items": superseded,
                "refused": False,
                "retrieval_mode": "lexical",
                "retrieval_degraded": False,
                "reasons": [],
            }
        return {
            "evidence_status": "current",
            "items": [],
            "refused": True,
            "retrieval_mode": "lexical",
            "retrieval_degraded": False,
            "reasons": [],
        }

    def hot_notes(self, query: str) -> list[dict[str, Any]]:
        query_grams = _grams(query)
        notes: list[dict[str, Any]] = []
        for status in ("proposed", "conflicted"):
            for item in self._scored(status, query_grams)[:3]:
                row = item["row"]
                notes.append(
                    {
                        "claim_id": row["claim_id"],
                        "version_id": row["version_id"],
                        "status": status,
                        "text": f"未审核新证据：{self._label(row['intervention'])}"
                        f"{self._label(row['attribute'])} "
                        f"{_value_text(json.loads(row['value_json']), row['direction'])}",
                    }
                )
        return notes

    def p0_rules(self) -> list[dict[str, Any]]:
        rows = self.kb.execute(
            "SELECT rule_id, drug, age_max_years, condition, template FROM p0_rules "
            "WHERE enabled = 1 ORDER BY rule_id"
        ).fetchall()
        return [dict(row) for row in rows]


def _hot_updates(kb, service: "EvidenceService", cfg: Config, query: str) -> list[dict[str, Any]]:
    """P2 evidence: high-tier hot versions, their diff against the cold current,
    and the cold claims whose answer may change (ticket 27)."""
    grams = _grams(query)
    updates: list[dict[str, Any]] = []
    for status in ("proposed", "conflicted"):
        for item in service._scored(status, grams)[:3]:
            row = item["row"]
            if row.get("evidence_type") not in (cfg.evidence_min or ()):
                continue
            current = kb.execute(
                "SELECT * FROM claim_versions WHERE claim_id = ? AND status = 'approved' "
                "ORDER BY created_at DESC, version_id DESC LIMIT 1",
                (row["claim_id"],),
            ).fetchone()
            new_value = json.loads(row["value_json"])
            new_text = _value_text(new_value, row["direction"])
            old_text = None
            changed: list[str] = []
            old_value = None
            if current is not None:
                old_value = json.loads(current["value_json"])
                old_text = _value_text(old_value, current["direction"])
                if old_value != new_value:
                    changed.append("value")
                if current["direction"] != row["direction"]:
                    changed.append("direction")
            else:
                changed.append("new_claim")
            impact_rows = kb.execute(
                "SELECT v.*, c.attribute, c.population FROM claim_versions v "
                "JOIN claims c ON c.claim_id = v.claim_id "
                "WHERE v.status = 'approved' AND c.intervention = ? AND v.claim_id <> ? "
                "ORDER BY v.claim_id",
                (row["intervention"], row["claim_id"]),
            ).fetchall()
            impact = [
                {
                    "claim_id": impact_row["claim_id"],
                    "attribute": impact_row["attribute"],
                    "population": impact_row["population"],
                    "value_text": _value_text(
                        json.loads(impact_row["value_json"]), impact_row["direction"]
                    ),
                }
                for impact_row in impact_rows
            ]
            updates.append(
                {
                    "claim_id": row["claim_id"],
                    "version_id": row["version_id"],
                    "status": status,
                    "evidence_type": row["evidence_type"],
                    "provenance": row["provenance"],
                    "diff": {
                        "changed": changed or ["none"],
                        "old_text": old_text,
                        "new_text": new_text,
                        "old_value": old_value,
                        "new_value": new_value,
                    },
                    "impact": impact,
                    "review_triggered": True,
                }
            )
    return updates


def _trigger_hot_review(kb, updates: list[dict[str, Any]], question_hash: str) -> None:
    if not updates:
        return
    with kb:
        for update in updates:
            kb.execute(
                "INSERT INTO kb_audit(event_seq, action, actor, detail_json, created_at) "
                "VALUES (NULL, 'p2_hot_evidence', 'system', ?, ?)",
                (
                    json.dumps(
                        {
                            "claim_id": update["claim_id"],
                            "version_id": update["version_id"],
                            "question_hash": question_hash,
                        },
                        ensure_ascii=False,
                    ),
                    now_iso(),
                ),
            )


def _section_for(attribute: str | None) -> str:
    return SECTION_BY_ATTRIBUTE.get(attribute or "", "治疗")


def answer(
    kb,
    cfg: Config,
    query: str,
    case_id: str | None = None,
    case_context: dict | None = None,
    corpus=None,
) -> dict[str, Any]:
    service = EvidenceService(kb, cfg)
    started = time.perf_counter()
    version = library_version(kb)
    routing = route(query, case_context)
    use_orchestration_v2 = os.environ.get("MPPKB_ORCHESTRATION_V2", "0").strip() == "1"
    route_path = str(routing.get("path") or "simple")
    top_k = 6 if use_orchestration_v2 and route_path == "complex" else 5
    planned_query = None
    requested_groups: list[str] = []
    retrieval_query = query
    if use_orchestration_v2 and route_path == "complex" and service._mentioned_drugs(query):
        planned_query, requested_groups = _planned_query(query)
        retrieval_query = planned_query
    case_hash = hashlib.sha256(
        _canonical({"case_id": case_id or "", "context": case_context or {}}).encode("utf-8")
    ).hexdigest()[:16]
    question_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:32]
    signature = _retrieval_signature(cfg, kb)
    cache_key = _cache_key(retrieval_query, case_hash, version, signature, top_k=top_k)
    snapshot_id = _active_snapshot(kb)
    cached = _cache_get(kb, cache_key, version)
    if cached is None:
        retrieval = service.retrieve(retrieval_query, top_k=top_k)
        _cache_set(
            kb,
            cache_key,
            version,
            {
                "evidence_status": retrieval["evidence_status"],
                "refused": retrieval["refused"],
                "version_ids": [item["row"]["version_id"] for item in retrieval["items"]],
                "retrieval_mode": retrieval.get("retrieval_mode", "lexical"),
                "retrieval_degraded": retrieval.get("retrieval_degraded", False),
                "reasons": retrieval.get("reasons", []),
            },
            signature,
        )
        cache_hit = False
    else:
        retrieval = {
            "evidence_status": cached["evidence_status"],
            "refused": cached["refused"],
            "items": service.items_by_ids(cached["version_ids"]),
            "retrieval_mode": cached.get("retrieval_mode", "lexical"),
            "retrieval_degraded": cached.get("retrieval_degraded", False),
            "reasons": cached.get("reasons", []),
        }
        cache_hit = True
    retrieval_calls = 1
    gap_retries = 0
    notes = service.hot_notes(query)
    hot_updates = _hot_updates(kb, service, cfg, query)
    _trigger_hot_review(kb, hot_updates, question_hash)
    result: dict[str, Any] = {
        "ok": True,
        "path": routing["path"],
        "routing": routing,
        "orchestration_v2": use_orchestration_v2,
        "planned_expansion": bool(planned_query),
        "requested_groups": requested_groups,
        "expanded_query": planned_query,
        "library_version": version,
        "snapshot_id": snapshot_id,
        "claims": [],
        "citations": [],
        "source_details": [],
        "treatment_elements": [],
        "sections": [],
        "conclusion": "",
        "evidence_status": retrieval["evidence_status"],
        "p0_warning": None,
        "budgets": {
            "tool_calls": 0,
            "retrieval_calls": retrieval_calls,
            "gap_retries": gap_retries,
            "citation_rewrites": 0,
        },
        "model": {"name": "rule-based", "version": "0", "prompt_hash": None},
        "disclaimer": DISCLAIMER,
        "note": notes,
        "refused": retrieval["refused"],
        "cache_hit": cache_hit,
        "retrieval": {
            "mode": retrieval.get("retrieval_mode", "lexical"),
            "degraded": retrieval.get("retrieval_degraded", False),
            "candidate_claims": len(retrieval["items"]),
            "reasons": retrieval.get("reasons", []),
        },
        "recommendation_suppressed": False,
        "attribution": [],
        "synthesis": {
            "backend": "rule",
            "model": "rule-based",
            "temperature": 0.0,
            "prompt_hash": "answer-rule-v1",
            "compile_version": "none",
            "sentences": 0,
            "defects": 0,
        },
        "hot_updates": hot_updates,
        "review_triggered": bool(hot_updates),
        "conflicts": [],
        "evidence_gaps": [],
        "evidence_status_block": _evidence_status_block(
            version, snapshot_id, retrieval, hot_updates, [], []
        ),
    }

    if retrieval["refused"]:
        result["message"] = "没有足够的当前证据回答该问题。"
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        _log_answer(kb, case_id, question_hash, snapshot_id, result, query)
        return result

    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in SECTION_ORDER}
    attribution: list[dict[str, Any]] = []
    summary_items: list[dict[str, Any]] = []
    retrieved_drugs: list[str] = []
    source_no = 0
    for item in retrieval["items"]:
        row = item["row"]
        if row["intervention"] not in retrieved_drugs:
            retrieved_drugs.append(row["intervention"])
        source_no += 1
        anchor = item["anchors"][0] if item["anchors"] else None
        if anchor is not None:
            result["citations"].append(
                {"source_no": source_no, "doc_id": anchor.get("doc_id"), "page": anchor.get("page_start")}
            )
        else:
            source_no -= 1
            continue
        claim_entry = {
            "claim_id": row["claim_id"],
            "version_id": row["version_id"],
            "status": row["status"],
            "value": json.loads(row["value_json"]),
            "direction": row["direction"],
            "anchors": item["anchors"],
            "source_no": source_no,
            "population": row.get("population"),
            "severity": row.get("severity"),
            "intervention": row.get("intervention"),
            "attribute": row.get("attribute"),
            "evidence_type": row.get("evidence_type"),
        }
        result["claims"].append(claim_entry)
        label = f"{service._label(row['intervention'])}{service._label(row['attribute'])}"
        text = f"{label}：{_value_text(json.loads(row['value_json']), row['direction'])}"
        if retrieval["evidence_status"] == "superseded":
            text += "（已过期，仅供参考）"
        attribution.append(
            {"sentence": text, "claim_id": row["claim_id"], "source_no": source_no}
        )
        summary_items.append(
            {
                "text": text,
                "claim_id": row["claim_id"],
                "source_no": source_no,
                "attribute": row["attribute"],
            }
        )
        result["source_details"].append(
            {
                "source_no": source_no,
                "text": text,
                "claim_id": row["claim_id"],
                "doc_id": anchor.get("doc_id") if anchor else None,
                "page": anchor.get("page_start") if anchor else None,
                "quote": (anchor.get("quote") if anchor else "") or "",
                "evidence_type": row.get("evidence_type"),
            }
        )
        buckets[_section_for(row["attribute"])].append(
            {"text": text, "claim_ids": [row["claim_id"]], "source_no": source_no}
        )

    for name in SECTION_ORDER:
        entries = buckets[name]
        if not entries:
            continue
        result["sections"].append(
            {
                "name": name,
                "text": "；".join(f"{entry['text']}[Source[{entry['source_no']}]]" for entry in entries),
                "claim_ids": [cid for entry in entries for cid in entry["claim_ids"]],
            }
        )
    result["attribution"] = attribution
    result["synthesis"]["sentences"] = len(attribution)
    result["synthesis"]["defects"] = 0

    # P0 safety exception: a claim promoted under a rule renders a fixed warning
    # section ahead of the recommendation.
    p0_section = None
    mentioned_drugs = service._mentioned_drugs(query)
    for item in retrieval["items"]:
        row = item["row"]
        if row.get("provenance") != "p0_exception":
            continue
        if mentioned_drugs and row.get("intervention") not in mentioned_drugs:
            continue
        rule = kb.execute(
            "SELECT rule_id, template FROM p0_rules WHERE drug = ? AND enabled = 1",
            (row["intervention"],),
        ).fetchone()
        if rule is None:
            continue
        result["p0_warning"] = {"rule_id": rule["rule_id"], "template": rule["template"]}
        p0_section = {
            "name": "安全警示",
            "text": f"【安全警示】{rule['template']}（P0 安全例外，已覆盖当前推荐）",
            "claim_ids": [row["claim_id"]],
        }
        break

    if p0_section is not None:
        result["recommendation_suppressed"] = True
        for section in result["sections"]:
            section["suppressed"] = True
            section["text"] = f"（P0 安全例外已覆盖，以下不构成推荐）{section['text']}"
        result["sections"].insert(0, p0_section)

    result["conclusion"] = _build_conclusion(summary_items, result.get("p0_warning"))

    existing_versions = {str(claim.get("version_id")) for claim in result["claims"]}
    start_source = max(
        [int(item.get("source_no") or 0) for item in result["source_details"]],
        default=0,
    ) + 1
    elements, extra_citations, extra_details = _collect_treatment_elements(
        kb, service, query, existing_versions, start_source, retrieved_drugs
    )
    result["treatment_elements"] = elements
    result["citations"].extend(extra_citations)
    result["source_details"].extend(extra_details)

    if not result["sections"] and not result["treatment_elements"]:
        result["refused"] = True
        result["message"] = "没有足够的当前证据回答该问题。"
        result["conclusion"] = "没有足够的已审核证据形成结论。"
    elif result["treatment_elements"] and not result["sections"]:
        result["conclusion"] = "已找到该药的治疗要素（见下），但缺少直接回答问题的综合证据；请核对来源。"

    evidence_gap_details = _evidence_gap_details(result["treatment_elements"])
    evidence_gaps = [
        f"{detail.get('drug')}：{'、'.join(detail.get('missing') or [])}"
        for detail in evidence_gap_details
    ]
    conflicts = _detect_conflicts(result["claims"], service)
    citation_check = _citation_check(
        result["conclusion"],
        {
            int(item["source_no"])
            for item in result["citations"]
            if item.get("source_no") is not None
        },
    )
    result["conflicts"] = conflicts
    result["evidence_gaps"] = evidence_gaps
    result["evidence_gap_details"] = evidence_gap_details
    gap_fills: list[dict[str, Any]] = []
    if evidence_gap_details:
        corpus_conn = corpus if corpus is not None else connect(cfg.corpus_db)
        try:
            gap_fills = fill_gaps(kb, corpus_conn, service, evidence_gap_details)
        finally:
            if corpus is None:
                corpus_conn.close()
    result["gap_fills"] = gap_fills
    result["citation_check"] = citation_check
    result["synthesis"]["defects"] = int(citation_check.get("defects") or 0)
    result["evidence_status_block"] = _evidence_status_block(
        version,
        snapshot_id,
        retrieval,
        hot_updates,
        conflicts,
        evidence_gaps,
        evidence_gap_details,
        citation_check,
    )
    result["evidence_status_block"]["gap_fills"] = gap_fills

    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    _log_answer(kb, case_id, question_hash, snapshot_id, result, query)
    return result


def run(args: Any, cfg: Config) -> int:
    kb, _corpus = ensure_databases(cfg)
    query = getattr(args, "query", "")
    case_id = getattr(args, "case_id", None)
    case_context = getattr(args, "context", None)
    result = answer(kb, cfg, query, case_id, case_context, _corpus)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(result.get("conclusion") or result.get("message", ""))
        for section in result["sections"]:
            print(f"## {section['name']}")
            print(section["text"])
        for note in result["note"]:
            print(f"(附注) {note['text']}")
        print(result["disclaimer"])
    return 0
