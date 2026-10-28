"""Run the fixed 20-case orchestration evaluation.

The cases are technical evaluation prompts, not clinical gold standards. The
script records the current behaviour and writes machine-readable artifacts.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import time
from pathlib import Path

from mppkb.answer import answer
from mppkb.config import load_config
from mppkb.db import ensure_databases

CASES = [
    ("c01", "儿童 阿奇霉素 用法", "simple", ["阿奇霉素", "用法"]),
    ("c02", "儿童 阿奇霉素 剂量", "simple", ["阿奇霉素", "剂量"]),
    ("c03", "儿童 重症 支原体肺炎 治疗", "complex", ["儿童", "重症", "治疗"]),
    ("c04", "儿童 难治性 支原体肺炎 阿奇霉素 剂量 疗程", "complex", ["阿奇霉素", "剂量", "疗程"]),
    ("c05", "儿童 难治性 支原体肺炎 甲泼尼龙 联合用药", "complex", ["甲泼尼龙", "联合"]),
    ("c06", "阿奇霉素 禁忌 婴幼儿", "complex", ["阿奇霉素", "禁忌"]),
    ("c07", "儿童 重症 支原体肺炎 静脉注射免疫球蛋白 剂量", "complex", ["静脉注射免疫球蛋白", "剂量"]),
    ("c08", "儿童 难治性 支原体肺炎 糖皮质激素 用法", "complex", ["糖皮质激素", "用法"]),
    ("c09", "儿童重症支原体肺炎如何选择抗感染治疗？", "complex", ["治疗", "首选"]),
    ("c10", "阿奇霉素 疗程 3天 7天 区别", "complex", ["阿奇霉素", "疗程"]),
    ("c11", "儿童 难治性 阿奇霉素 甲泼尼龙 联合", "complex", ["阿奇霉素", "甲泼尼龙", "联合"]),
    ("c12", "阿奇霉素 指南更新 耐药", "complex", ["阿奇霉素", "更新"]),
    ("c13", "儿童 哮喘 生物制剂 剂量", "boundary", []),
    ("c14", "8岁以下 阿奇霉素 剂量", "boundary", ["阿奇霉素", "剂量", "年龄"]),
    ("c15", "阿奇霉素 与 布洛芬 相互作用", "boundary", []),
    ("c16", "孕妇 阿奇霉素 禁忌", "boundary", ["阿奇霉素", "禁忌", "孕妇"]),
    ("c17", "这个药怎么用？", "vague", []),
    ("c18", "儿童 怎么办", "vague", []),
    ("c19", "剂量多少", "vague", []),
    ("c20", "有什么禁忌", "vague", []),
]


def _segments(conclusion: str) -> list[str]:
    text = str(conclusion or "").strip()
    if not text:
        return []
    parts = re.split(r"[；;\n]+", text)
    boilerplate = ("综合当前", "以上为", "具体诊疗", "本回答", "提示：")
    return [
        part.strip()
        for part in parts
        if part.strip() and not part.strip().startswith(boilerplate)
    ]


def _citation_coverage(conclusion: str) -> tuple[int, int, float]:
    segments = _segments(conclusion)
    if not segments:
        return 0, 0, 0.0
    cited = sum(1 for segment in segments if "Source[" in segment)
    return cited, len(segments), round(cited / len(segments), 4)


def _element_hits(case_elements: list[str], result: dict) -> dict:
    blob = " ".join(
        [
            str(result.get("conclusion") or ""),
            " ".join(str(section.get("text") or "") for section in result.get("sections") or []),
            " ".join(
                str(item.get("text") or "")
                for element in result.get("treatment_elements") or []
                for scenario in element.get("scenarios") or []
                for items in (scenario.get("groups") or {}).values()
                for item in items
            ),
        ]
    )
    hits = [term for term in case_elements if term in blob]
    return {
        "expected": case_elements,
        "hits": hits,
        "recall": round(len(hits) / len(case_elements), 4) if case_elements else None,
    }


def run(out_dir: Path, label: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    kb, _ = ensure_databases(cfg)
    rows = []
    for case_id, query, category, expected in CASES:
        started = time.perf_counter()
        result = answer(kb, cfg, query, case_id=case_id)
        latency = (time.perf_counter() - started) * 1000.0
        cited, segments, coverage = _citation_coverage(result.get("conclusion") or "")
        block = result.get("evidence_status_block") or {}
        gaps = [
            f"{element.get('drug')}:{'、'.join(element.get('missing') or [])}"
            for element in result.get("treatment_elements") or []
            if element.get("missing")
        ]
        budgets = result.get("budgets") or {}
        path = str(result.get("path") or "")
        route_effective = bool(
            (path == "simple" and budgets.get("retrieval_calls") == 1 and budgets.get("gap_retries") == 0)
            or (path == "complex" and bool(result.get("orchestration_v2")))
        ) and (result.get("routing") or {}).get("path") == path
        rows.append(
            {
                "id": case_id,
                "query": query,
                "category": category,
                "snapshot_id": result.get("snapshot_id"),
                "library_version": result.get("library_version"),
                "refused": bool(result.get("refused")),
                "latency_ms": round(latency, 2),
                "path": path,
                "budgets": budgets,
                "route_effective": route_effective,
                "planned_expansion": bool(result.get("planned_expansion")),
                "requested_groups": result.get("requested_groups") or [],
                "claims": len(result.get("claims") or []),
                "citations": len(result.get("citations") or []),
                "cited_segments": cited,
                "segments": segments,
                "citation_coverage": coverage,
                "status_block_present": bool(block.get("summary")),
                "status_block": block,
                "hot_updates": len(result.get("hot_updates") or []),
                "evidence_gaps": gaps,
                "gap_fills": len(result.get("gap_fills") or []),
                "gap_fill_actions": [
                    fill.get("action") for fill in result.get("gap_fills") or []
                ],
                "element_hits": _element_hits(expected, result),
            }
        )

    latencies = sorted(row["latency_ms"] for row in rows)
    p95 = latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)]
    aggregate = {
        "label": label,
        "case_count": len(rows),
        "snapshot_id": rows[0].get("snapshot_id") if rows else None,
        "library_version": rows[0].get("library_version") if rows else None,
        "refusal_rate": round(sum(1 for row in rows if row["refused"]) / len(rows), 4),
        "citation_coverage_mean": round(
            sum(row["citation_coverage"] for row in rows) / len(rows), 4
        ),
        "status_visible_rate": round(
            sum(1 for row in rows if row["status_block_present"]) / len(rows), 4
        ),
        "route_effective_rate": round(sum(1 for row in rows if row["route_effective"]) / len(rows), 4),
        "planned_expansion_rate": round(
            sum(1 for row in rows if row["planned_expansion"]) / len(rows), 4
        ),
        "gap_case_rate": round(sum(1 for row in rows if row["evidence_gaps"]) / len(rows), 4),
        "gap_fill_case_rate": round(
            sum(1 for row in rows if row["gap_fill_actions"]) / len(rows), 4
        ),
        "gap_fill_source_cases": sum(
            1 for row in rows if "source_hint" in row["gap_fill_actions"]
        ),
        "latency_mean_ms": round(statistics.mean(latencies), 2),
        "latency_p95_ms": round(p95, 2),
        "element_recall_mean": round(
            statistics.mean(
                row["element_hits"]["recall"]
                for row in rows
                if row["element_hits"]["recall"] is not None
            ),
            4,
        ),
    }
    payload = {"aggregate": aggregate, "cases": rows}
    (out_dir / f"{label}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "cases.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "id": case_id,
                    "query": query,
                    "category": category,
                    "expected_elements": expected,
                    "note": "technical evaluation prompt; not a clinical gold standard",
                },
                ensure_ascii=False,
            )
            for case_id, query, category, expected in CASES
        )
        + "\n",
        encoding="utf-8",
    )
    report = [
        f"# 编排评测 {label}",
        "",
        f"- 用例：{aggregate['case_count']}",
        f"- snapshot：{aggregate['snapshot_id']}",
        f"- 版本：{aggregate['library_version']}",
        f"- 引用覆盖均值：{aggregate['citation_coverage_mean']:.1%}",
        f"- 证据状态可见率：{aggregate['status_visible_rate']:.1%}",
        f"- route 生效率：{aggregate['route_effective_rate']:.1%}",
        f"- 计划扩展触发率：{aggregate['planned_expansion_rate']:.1%}",
        f"- 缺口案例率：{aggregate['gap_case_rate']:.1%}",
        f"- 自动补检索覆盖率：{aggregate['gap_fill_case_rate']:.1%}（含原文线索 {aggregate['gap_fill_source_cases']} 例）",
        f"- 延迟均值 / p95：{aggregate['latency_mean_ms']} / {aggregate['latency_p95_ms']} ms",
        f"- 期望要素召回均值：{aggregate['element_recall_mean']:.1%}",
        "",
        "## 失败/缺口案例",
    ]
    for row in rows:
        if row["refused"] or row["evidence_gaps"] or not row["status_block_present"]:
            report.append(
                f"- {row['id']} {row['query']}：refused={row['refused']}，"
                f"status={row['status_block_present']}，缺口={row['evidence_gaps']}"
            )
    (out_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=str(Path("docs/eval/orchestration_20261027")))
    parser.add_argument("--label", default="baseline")
    args = parser.parse_args()
    run(Path(args.out_dir), args.label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
