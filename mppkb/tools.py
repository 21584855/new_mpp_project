"""Deterministic tools for the complex path (ticket 34).

Every tool is a pure function over SQLite data: no network, no model calls.
The CLI exposes ``tools list|run`` as the test seam; the LangGraph tools node
uses the same registry.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from typing import Any

from .config import Config
from .db import connect, ensure_databases
from .ontology import load_ontology
from .scope import LAB_ATTRIBUTES

TOOL_SPECS: dict[str, dict[str, Any]] = {
    "dose_calculator": {
        "description": "体重 × 冷库 mg/kg 剂量 claim，返回计算剂量与来源锚点",
        "input": {"drug": "药物名或 DRUG: 术语", "weight_kg": "体重", "age_years": "年龄（可选）"},
    },
    "p0_check": {
        "description": "药物 × 年龄查 P0 规则，返回是否命中与警示模板",
        "input": {"drug": "药物名", "age_years": "年龄"},
    },
    "lab_threshold": {
        "description": "查化验阈值 claim（LDH/CRP/PCT/血糖等）",
        "input": {"marker": "化验指标名（如 C反应蛋白/血糖）"},
    },
    "guideline_search": {
        "description": "语料 chunk 检索，返回页码与段落",
        "input": {"query": "检索词", "top_k": "可选，默认 5"},
    },
}


def _ontology_index(cfg: Config):
    try:
        return load_ontology(cfg.ontology)
    except Exception:  # noqa: BLE001 - tools fail soft
        return None


def _map(cfg: Config, text: str | None, kind: str) -> str | None:
    if not text:
        return None
    ontology = _ontology_index(cfg)
    if ontology is None:
        return None
    return ontology.map_text(str(text), kind)


def _value_text(value: dict[str, Any], direction: str | None) -> str:
    if direction:
        return str(direction)
    if value.get("kind") == "numeric":
        return f"{value.get('value')}{value.get('unit') or ''}"
    if value.get("kind") == "range":
        return f"{value.get('low')}-{value.get('high')}{value.get('unit') or ''}"
    return json.dumps(value, ensure_ascii=False)


def _population_for_age(cfg: Config, age: float | None) -> list[str]:
    if age is None:
        return []
    candidates = []
    if age < 8:
        candidates.append(_map(cfg, "8岁以下", "population"))
    if age < 18:
        candidates.append(_map(cfg, "儿童", "population"))
        candidates.append(_map(cfg, "青少年", "population"))
    candidates.append(_map(cfg, "成人", "population"))
    return [item for item in candidates if item]


def dose_calculator(
    kb, corpus, cfg: Config, payload: dict[str, Any]
) -> dict[str, Any]:
    drug = _map(cfg, payload.get("drug"), "drug") or payload.get("drug")
    weight = payload.get("weight_kg")
    if not drug or weight is None:
        return {"status": "error", "error": "drug and weight_kg are required"}
    try:
        weight = float(weight)
    except (TypeError, ValueError):
        return {"status": "error", "error": "weight_kg must be numeric"}
    rows = kb.execute(
        "SELECT v.version_id, v.claim_id, v.value_json, v.direction, v.anchors_json, "
        "c.population, c.severity, c.intervention, c.attribute FROM claim_versions v "
        "JOIN claims c ON c.claim_id = v.claim_id "
        "WHERE v.status = 'approved' AND c.intervention = ? AND c.attribute = 'ATTR:DOSE' "
        "ORDER BY v.created_at DESC, v.version_id",
        (drug,),
    ).fetchall()
    if not rows:
        return {"status": "not_found", "reason": "no approved dose claim"}
    preferred = _population_for_age(cfg, payload.get("age_years"))
    if preferred:
        row = next((item for item in rows if item["population"] in preferred), None)
        if row is None:
            return {
                "status": "not_found",
                "reason": "no dose claim for this age population",
            }
    else:
        row = rows[0]
    value = json.loads(row["value_json"])
    unit = str(value.get("unit") or "")
    compact = unit.lower().replace(" ", "")
    if "mg/kg" not in compact:
        return {"status": "not_found", "reason": f"dose unit not mg/kg: {unit}"}
    suffix = "/day" if ("day" in compact or compact.endswith("/d")) else "/dose"
    if value.get("kind") == "numeric":
        total = round(float(value.get("value") or 0) * weight, 2)
        result = {"value": total, "unit": "mg" + suffix, "weight_kg": weight}
    elif value.get("kind") == "range":
        low = round(float(value.get("low") or 0) * weight, 2)
        high = round(float(value.get("high") or 0) * weight, 2)
        result = {"low": low, "high": high, "unit": "mg" + suffix, "weight_kg": weight}
    else:
        return {"status": "not_found", "reason": "dose value kind unsupported"}
    anchors = json.loads(row["anchors_json"])
    return {
        "status": "ok",
        "data": {
            "drug": drug,
            "population": row["population"],
            "calculation": result,
            "claim_id": row["claim_id"],
            "version_id": row["version_id"],
            "anchor": anchors[0] if anchors else None,
        },
    }


def p0_check(kb, corpus, cfg: Config, payload: dict[str, Any]) -> dict[str, Any]:
    drug = _map(cfg, payload.get("drug"), "drug") or payload.get("drug")
    age = payload.get("age_years")
    if not drug or age is None:
        return {"status": "error", "error": "drug and age_years are required"}
    try:
        age = float(age)
    except (TypeError, ValueError):
        return {"status": "error", "error": "age_years must be numeric"}
    rules = kb.execute(
        "SELECT rule_id, drug, age_max_years, condition, template FROM p0_rules "
        "WHERE enabled = 1 AND drug = ?",
        (drug,),
    ).fetchall()
    for rule in rules:
        if rule["age_max_years"] is not None and age < float(rule["age_max_years"]):
            return {
                "status": "ok",
                "data": {
                    "hit": True,
                    "rule_id": rule["rule_id"],
                    "age_years": age,
                    "age_max_years": rule["age_max_years"],
                    "template": rule["template"],
                },
            }
    return {"status": "ok", "data": {"hit": False, "drug": drug, "age_years": age}}


def lab_threshold(kb, corpus, cfg: Config, payload: dict[str, Any]) -> dict[str, Any]:
    marker = _map(cfg, payload.get("marker"), "attribute") or payload.get("marker")
    if not marker:
        return {"status": "error", "error": "marker is required"}
    if marker not in LAB_ATTRIBUTES:
        return {"status": "not_found", "reason": f"not a lab attribute: {marker}"}
    rows = kb.execute(
        "SELECT v.version_id, v.claim_id, v.value_json, v.direction, v.anchors_json, "
        "c.population, c.severity FROM claim_versions v JOIN claims c ON c.claim_id = v.claim_id "
        "WHERE v.status = 'approved' AND c.attribute = ? "
        "ORDER BY v.created_at DESC, v.version_id LIMIT 5",
        (marker,),
    ).fetchall()
    if not rows:
        return {"status": "not_found", "reason": "no approved lab claim"}
    data = []
    for row in rows:
        value = json.loads(row["value_json"])
        data.append(
            {
                "population": row["population"],
                "severity": row["severity"],
                "value": value,
                "value_text": _value_text(value, row["direction"]),
                "claim_id": row["claim_id"],
                "version_id": row["version_id"],
                "anchor": (json.loads(row["anchors_json"]) or [None])[0],
            }
        )
    return {"status": "ok", "data": {"marker": marker, "thresholds": data}}


def _search_terms(query: str) -> list[str]:
    parts = [part for part in re.split(r"[\s,，。;；、/|]+", query) if len(part) >= 2]
    return parts[:8] or ([query.strip()] if query.strip() else [])


def guideline_search(
    kb, corpus, cfg: Config, payload: dict[str, Any]
) -> dict[str, Any]:
    query = str(payload.get("query") or "").strip()
    if not query:
        return {"status": "error", "error": "query is required"}
    top_k = int(payload.get("top_k") or 5)
    own = False
    if corpus is None:
        corpus = connect(cfg.corpus_db)
        own = True
    try:
        terms = _search_terms(query)
        rows = []
        try:
            rows = corpus.execute(
                "SELECT c.chunk_id, c.paper_id, c.page_start, c.page_end, c.text "
                "FROM chunks_fts f JOIN chunks c ON c.chunk_id = f.chunk_id "
                "WHERE chunks_fts MATCH ? LIMIT 20",
                (" OR ".join(f'"{term}"' for term in terms),),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        if not rows:
            where = " OR ".join("text LIKE ?" for _ in terms)
            rows = corpus.execute(
                f"SELECT chunk_id, paper_id, page_start, page_end, text FROM chunks "
                f"WHERE {where} LIMIT 200",
                tuple(f"%{term}%" for term in terms),
            ).fetchall()
    finally:
        if own:
            corpus.close()
    scored = []
    for row in rows:
        text = row["text"] or ""
        score = sum(1 for term in terms if term in text)
        scored.append((score, -int(row["page_start"]), row))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]["chunk_id"]))
    scored = [item for item in scored if item[0] > 0][:top_k]
    if not scored:
        return {"status": "not_found", "reason": "no matching chunk"}
    return {
        "status": "ok",
        "data": {
            "query": query,
            "passages": [
                {
                    "paper_id": row["paper_id"],
                    "chunk_id": row["chunk_id"],
                    "page_start": row["page_start"],
                    "page_end": row["page_end"],
                    "text": (row["text"] or "")[:300],
                }
                for _score, _neg_page, row in scored
            ],
        },
    }


TOOLS = {
    "dose_calculator": dose_calculator,
    "p0_check": p0_check,
    "lab_threshold": lab_threshold,
    "guideline_search": guideline_search,
}


def run_tool(
    name: str, payload: dict[str, Any] | None, kb, cfg: Config, corpus=None
) -> dict[str, Any]:
    tool = TOOLS.get(name)
    if tool is None:
        return {"status": "error", "error": f"unknown tool: {name}"}
    try:
        return tool(kb, corpus, cfg, payload or {})
    except Exception as exc:  # noqa: BLE001 - tools must not crash the graph
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def run(args: Any, cfg: Config) -> int:
    command = getattr(args, "tools_command", "list")
    if command == "list":
        payload = {"ok": True, "tools": TOOL_SPECS}
        if getattr(args, "json", False):
            _emit(payload)
        else:
            for name, spec in TOOL_SPECS.items():
                print(f"{name}: {spec['description']}")
        return 0
    if command != "run":
        print(f"unknown tools command: {command}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(getattr(args, "input_json", "") or "{}")
    except json.JSONDecodeError as exc:
        result = {"ok": False, "error": f"invalid input json: {exc}"}
        _emit(result) if getattr(args, "json", False) else print(result)
        return 1
    kb, corpus = ensure_databases(cfg)
    result = run_tool(getattr(args, "name", ""), payload, kb, cfg, corpus)
    output = {"ok": result.get("status") in ("ok", "not_found"), **result}
    if getattr(args, "json", False):
        _emit(output)
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0
