"""Extraction port and candidate validation (ticket 03).

The extraction backend is the only replaceable outbound port in the system.
Tests always use :class:`ReplayBackend`; :class:`RealBackend` is a thin,
network-using implementation that is disabled unless an endpoint is configured.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from .scope import LAB_LABELS, TREATMENT_LABELS

VALUE_KINDS = frozenset({"numeric", "range", "direction"})
DIRECTIONS = frozenset({"first_line", "alternative", "avoid"})
EVIDENCE_TYPES = frozenset(
    {
        "guideline",
        "systematic_review",
        "randomized_controlled_trial",
        "observational_study",
        "narrative_review",
        "case_report",
    }
)
AE_TYPES = frozenset({"overall", "rash", "gi", "liver", "local", "other"})
AE_ARMS = frozenset({"observation", "control", "unspecified"})
AE_METRICS = frozenset({"rate", "count"})


class ExtractionError(Exception):
    """Raised when a backend cannot produce candidates for a section."""


class ExtractionBackend(Protocol):
    def extract(self, document: dict[str, Any], section: dict[str, Any]) -> list[dict[str, Any]]:
        ...


class ReplayBackend:
    """Deterministic backend reading recorded responses keyed by chunk hash."""

    DEFAULT_KEY = "__default__"

    def __init__(self, replay_path: str | Path):
        try:
            raw = Path(replay_path).read_text(encoding="utf-8")
            data = json.loads(raw)
        except OSError as exc:
            raise ExtractionError(f"cannot read replay file: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"replay file is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ExtractionError("replay file must be a JSON object keyed by chunk hash")
        self._data = data

    def extract(self, document: dict[str, Any], section: dict[str, Any]) -> list[dict[str, Any]]:
        key = str(section.get("chunk_hash"))
        entry = self._data.get(key, self._data.get(self.DEFAULT_KEY))
        if entry is None:
            raise ExtractionError(f"no_replay_entry: {key}")
        if isinstance(entry, dict):
            error = entry.get("error")
            if error:
                raise ExtractionError(str(error))
            raise ExtractionError("malformed_response: replay entry must be an array")
        if not isinstance(entry, list):
            raise ExtractionError("malformed_response: replay entry must be an array")
        return entry


class RealBackend:
    """Model-backed extractor using a compatible chat-completions endpoint."""

    DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    DEFAULT_MODEL = "qwen-plus"
    SYSTEM_PROMPT = "你是医学证据抽取器。只输出 JSON，不要输出解释或 Markdown。"

    def __init__(
        self,
        transport=None,
        endpoint: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: int = 90,
        include_labs: bool | None = None,
    ) -> None:
        self.endpoint = (
            endpoint
            or os.environ.get("MPPKB_LLM_ENDPOINT", "").strip()
            or self.DEFAULT_ENDPOINT
        )
        self.model = model or os.environ.get("MPPKB_LLM_MODEL", "").strip() or self.DEFAULT_MODEL
        self.api_key = (
            api_key
            or os.environ.get("MPPKB_API_KEY", "")
            or os.environ.get("DASHSCOPE_API_KEY", "")
        )
        self.timeout = timeout
        self.transport = transport
        if include_labs is None:
            include_labs = os.environ.get("MPPKB_INCLUDE_LABS", "").strip().lower() in (
                "1",
                "true",
                "yes",
            )
        self.include_labs = include_labs
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def extract(self, document: dict[str, Any], section: dict[str, Any]) -> list[dict[str, Any]]:
        content = self._chat(self._prompt(document, section))
        return _parse_candidates(content, document, section)

    def _prompt(self, document: dict[str, Any], section: dict[str, Any]) -> str:
        ontology_lines = _ontology_labels()
        if self.include_labs:
            scope_line = (
                "治疗类与化验类都收；化验项必须放在 attribute（如 血糖、C反应蛋白），"
                "不要放进 intervention。"
            )
            attribute_options = TREATMENT_LABELS + "、" + LAB_LABELS
        else:
            scope_line = (
                "默认只收治疗类结论（用药、剂量、疗程、禁忌、联合治疗、随访）；"
                "不要输出化验指标（CRP、LDH、血糖、白细胞、血气等）。"
            )
            attribute_options = TREATMENT_LABELS
        example = (
            '{"candidates":[{"population":"儿童","severity":"轻症",'
            '"intervention":"阿奇霉素","attribute":"剂量",'
            '"value":{"kind":"numeric","value":10,"unit":"mg/kg/day"},'
            '"direction":null,"evidence_type":"guideline",'
            '"anchors":[{"quote":"轻症患者予阿奇霉素 10 mg/kg，每日 1 次"}],'
            '"confidence":0.9}]}'
        )
        return (
            "从下面的指南/论文段落中抽取可比较的医学结论（claim）。\n"
            f"文档标题：{document.get('title') or ''}\n"
            f"发表年份：{document.get('year') or ''}\n"
            f"章节：{section.get('path') or ''}\n"
            f"正文：\n{section.get('text') or ''}\n\n"
            f"{scope_line}\n"
            "人群写 儿童/青少年/8岁以下/成人；严重度写 轻症/重症/难治性/暴发性（原文有则必填）。\n"
            "输出严格 JSON（不要 Markdown 围栏），格式示例：\n"
            f"{example}\n\n"
            "字段规则：\n"
            "- population：人群（如 儿童、8岁以下、成人）\n"
            "- severity：严重度（轻症/重症等）或 null\n"
            "- intervention：药物/干预，用规范中文名\n"
            f"- attribute：{attribute_options} 之一\n"
            "- value：numeric 需 value+unit；range 需 low/high+unit；direction 需 value 取 "
            "first_line|alternative|avoid\n"
            "- direction：value.kind=direction 时必填，其余 null\n"
            "- evidence_type：guideline|systematic_review|randomized_controlled_trial|"
            "observational_study|narrative_review|case_report\n"
            "- anchors：**必填、非空**，每项 {\"quote\": \"正文中的逐字原句\"}；"
            "quote 必须能在正文中逐字找到，不能改写或拼接\n"
            "- confidence：0-1\n"
            "- 若 attribute 为不良反应，必须额外输出：ae_type（overall|rash|gi|liver|local|other）、"
            "arm（observation|control|unspecified）、metric（rate|count）、denominator（样本量或 null）；"
            "发生率 value.unit 用 %，例数用 例，不要把观察组与对照组合并成一个值\n\n"
            "没有可抽取的结论时输出 {\"candidates\":[]}；"
            "每个候选都必须带 anchors，否则不要输出该候选。\n"
            + (f"可选标签：\n{ontology_lines}\n" if ontology_lines else "")
        )

    def _chat(self, prompt: str) -> str:
        if not self.api_key:
            raise ExtractionError("MPPKB_API_KEY / DASHSCOPE_API_KEY is not set")
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        if self.transport is not None:
            return self.transport(self.model, messages, self.timeout)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        json_mode = os.environ.get("MPPKB_LLM_JSON_MODE", "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            data = self._post(payload)
        except ExtractionError as exc:
            if json_mode and "HTTP 400" in str(exc):
                payload.pop("response_format", None)
                data = self._post(payload)
            else:
                raise

        usage = data.get("usage") or {}
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ExtractionError("malformed_response: no message content") from exc

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise ExtractionError(f"llm HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ExtractionError(f"llm network error: {exc}") from exc


def _ontology_labels() -> str:
    path = os.environ.get("MPPKB_ONTOLOGY") or "ontology.yaml"
    try:
        import yaml

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - vocabulary is optional for the prompt
        return ""
    labels = []
    for term in data.get("terms") or []:
        label = str(term.get("label") or "").strip()
        if label:
            labels.append(f"{label}（{term.get('kind')}）")
    return "、".join(labels[:120])


DIRECTION_ALIASES = {
    "首选": "first_line",
    "一线": "first_line",
    "推荐": "first_line",
    "first-line": "first_line",
    "备选": "alternative",
    "二线": "alternative",
    "替代": "alternative",
    "避免": "avoid",
    "禁用": "avoid",
    "不推荐": "avoid",
    "禁忌": "avoid",
    "first_line": "first_line",
    "second_line": "alternative",
    "alternative": "alternative",
    "avoid": "avoid",
}
EVIDENCE_ALIASES = {
    "指南": "guideline",
    "共识": "guideline",
    "系统评价": "systematic_review",
    "系统综述": "systematic_review",
    "meta分析": "systematic_review",
    "meta-analysis": "systematic_review",
    "随机对照": "randomized_controlled_trial",
    "rct": "randomized_controlled_trial",
    "观察性": "observational_study",
    "队列": "observational_study",
    "病例": "case_report",
    "综述": "narrative_review",
}
AE_TYPE_ALIASES = {
    "overall": "overall",
    "总": "overall",
    "总体": "overall",
    "总体不良反应": "overall",
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
    "unspecified": "unspecified",
}
AE_METRIC_ALIASES = {
    "rate": "rate",
    "发生率": "rate",
    "比例": "rate",
    "count": "count",
    "例数": "count",
    "例": "count",
}


def _infer_ae_fields(item: dict[str, Any]) -> None:
    """Coerce/infer AE type, study arm, metric and denominator."""
    attribute = str(item.get("attribute") or "")
    if "不良反应" not in attribute and "ADVERSE" not in attribute.upper():
        return
    anchors = item.get("anchors")
    text = ""
    if isinstance(anchors, list):
        text = " ".join(
            str(anchor.get("quote") or "") for anchor in anchors if isinstance(anchor, dict)
        )
    raw_type = str(item.get("ae_type") or "").strip().lower()
    ae_type = AE_TYPE_ALIASES.get(raw_type)
    if ae_type is None and raw_type in AE_TYPES:
        ae_type = raw_type
    if ae_type is None:
        if any(key in text for key in ("皮疹", "荨麻疹", "斑丘疹", "红斑")):
            ae_type = "rash"
        elif any(key in text for key in ("恶心", "呕吐", "腹痛", "腹泻", "胃肠道")):
            ae_type = "gi"
        elif any(key in text for key in ("肝功能", "转氨酶", "ALT", "AST", "肝损伤")):
            ae_type = "liver"
        elif any(key in text for key in ("局部", "静脉炎", "注射部位")):
            ae_type = "local"
        else:
            ae_type = "overall" if "总" in text else "other"
    raw_arm = str(item.get("arm") or "").strip().lower()
    arm = AE_ARM_ALIASES.get(raw_arm)
    if arm is None and raw_arm in AE_ARMS:
        arm = raw_arm
    if arm is None:
        has_observation = any(key in text for key in ("观察组", "研究组", "治疗组"))
        has_control = any(key in text for key in ("对照组", "常规组"))
        if has_observation and not has_control:
            arm = "observation"
        elif has_control and not has_observation:
            arm = "control"
        else:
            arm = "unspecified"
    raw_metric = str(item.get("metric") or "").strip().lower()
    metric = AE_METRIC_ALIASES.get(raw_metric)
    if metric is None and raw_metric in AE_METRICS:
        metric = raw_metric
    if metric is None:
        unit = str((item.get("value") or {}).get("unit") or "")
        if "%" in unit or "发生率" in text:
            metric = "rate"
        elif "例" in unit:
            metric = "count"
    denominator = item.get("denominator")
    if not isinstance(denominator, (int, float)) or isinstance(denominator, bool):
        denominator = None
        import re

        match = re.search(r"\(?\s*\d+\s*/\s*(\d+)\s*\)?", text)
        if match:
            denominator = int(match.group(1))
    item["ae_type"] = ae_type
    item["arm"] = arm
    item["metric"] = metric
    item["denominator"] = denominator


def _coerce_direction(value: Any) -> Any:
    if value is None:
        return None
    text = str(value).strip().lower()
    return DIRECTION_ALIASES.get(text, text if text in DIRECTIONS else value)


def _coerce_candidate(item: dict[str, Any]) -> dict[str, Any]:
    """Repair the most common model deviations before validation."""
    anchors = item.get("anchors")
    if isinstance(anchors, str) and anchors.strip():
        item["anchors"] = [{"quote": anchors.strip()}]
    if isinstance(item.get("direction"), str):
        item["direction"] = _coerce_direction(item["direction"])
    evidence = item.get("evidence_type")
    if isinstance(evidence, str):
        item["evidence_type"] = EVIDENCE_ALIASES.get(evidence.strip().lower(), evidence)

    value = item.get("value")
    if isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        item["value"] = {
            "kind": "numeric",
            "value": float(value),
            "unit": item.get("unit"),
        }
        item.pop("unit", None)
    elif isinstance(value, str):
        text = value.strip().lower()
        if text in DIRECTIONS or text in DIRECTION_ALIASES:
            direction = _coerce_direction(text)
            item["value"] = {"kind": "direction", "value": direction}
            if not item.get("direction"):
                item["direction"] = direction
        else:
            try:
                item["value"] = {
                    "kind": "numeric",
                    "value": float(text),
                    "unit": item.get("unit"),
                }
                item.pop("unit", None)
            except ValueError:
                pass

    value = item.get("value")
    if isinstance(value, dict):
        kind = value.get("kind")
        if kind == "direction":
            value["value"] = _coerce_direction(value.get("value"))
            if not item.get("direction"):
                item["direction"] = value.get("value")
        if kind == "numeric" and isinstance(value.get("value"), str):
            try:
                value["value"] = float(value["value"])
            except ValueError:
                pass
        if kind == "range":
            try:
                if float(value.get("low")) > float(value.get("high")):
                    value["low"], value["high"] = value["high"], value["low"]
            except (TypeError, ValueError):
                pass
    _infer_ae_fields(item)
    return item


def _parse_candidates(
    content: str, document: dict[str, Any], section: dict[str, Any]
) -> list[dict[str, Any]]:
    import re

    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        array = re.search(r"\[.*\]", text, re.S)
        obj = re.search(r"\{.*\}", text, re.S)
        match = array or obj
        if match is None:
            raise ExtractionError("malformed_response: no JSON found")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"malformed_response: {exc}") from exc

    if isinstance(data, dict):
        candidates = data.get("candidates")
        if candidates is None:
            candidates = data.get("claims")
    elif isinstance(data, list):
        candidates = data
    else:
        candidates = None
    if not isinstance(candidates, list):
        raise ExtractionError("malformed_response: expected a JSON array")

    doc_id = str(document.get("paper_id") or str(document.get("sha256") or "")[:16])
    parsed: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        item = _coerce_candidate(item)
        quote = None
        anchors = item.get("anchors")
        if isinstance(anchors, list) and anchors and isinstance(anchors[0], dict):
            quote = anchors[0].get("quote")
        if not quote:
            quote = item.get("quote") or item.get("source_quote") or item.get("evidence")
        parsed.append(
            {
                "population": item.get("population"),
                "severity": item.get("severity"),
                "intervention": item.get("intervention"),
                "attribute": item.get("attribute"),
                "value": item.get("value"),
                "direction": item.get("direction"),
                "evidence_type": item.get("evidence_type"),
                "ae_type": item.get("ae_type"),
                "arm": item.get("arm"),
                "metric": item.get("metric"),
                "denominator": item.get("denominator"),
                "confidence": item.get("confidence", 0.7),
                "anchors": [
                    {
                        "doc_id": doc_id,
                        "page_start": section.get("page_start"),
                        "page_end": section.get("page_end"),
                        "quote": str(quote) if quote else "",
                    }
                ]
                if quote
                else [],
            }
        )
    return parsed


def _norm(text: str) -> str:
    return " ".join(text.split())


_COMPACT_DROP = set("，。；：、（）()【】[]“”‘’\"'·,.;:!?！？—–-/\\")


def _compact(text: str) -> str:
    return "".join(ch for ch in str(text) if not ch.isspace() and ch not in _COMPACT_DROP)


def _quote_coverage(quote_compact: str, section_compact: str) -> float:
    """Bigram coverage of a quote in the section; tolerates table cell order."""
    if len(quote_compact) < 10:
        return 0.0
    bigrams = [quote_compact[index : index + 2] for index in range(len(quote_compact) - 1)]
    if not bigrams:
        return 0.0
    hits = sum(1 for bigram in bigrams if bigram in section_compact)
    return hits / len(bigrams)


def _load_json_content(content: str) -> Any:
    import re

    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S) or re.search(r"\[.*\]", text, re.S)
        if match is None:
            raise ExtractionError("malformed_response: no JSON found")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"malformed_response: {exc}") from exc


def _compact_map(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(str(text)):
        if char.isspace() or char in _COMPACT_DROP:
            continue
        chars.append(char)
        positions.append(index)
    return "".join(chars), positions


def _snap_quote(section_text: str, quote: str | None) -> str | None:
    """Return the exact original substring for a model-supplied quote."""
    text = str(section_text or "")
    candidate = str(quote or "").strip()
    if not text or not candidate:
        return None
    direct = text.find(candidate)
    if direct >= 0:
        return text[direct : direct + len(candidate)]
    compact, positions = _compact_map(text)
    compact_quote = _compact(candidate)
    if not compact_quote:
        return None
    start = compact.find(compact_quote)
    if start < 0:
        return None
    end = start + len(compact_quote) - 1
    return text[positions[start] : positions[end] + 1]


def _parse_sentences(content: str) -> list[dict[str, Any]]:
    data = _load_json_content(content)
    if isinstance(data, dict):
        rows = data.get("sentences")
    elif isinstance(data, list):
        rows = data
    else:
        rows = None
    if not isinstance(rows, list):
        raise ExtractionError("malformed_response: expected sentence array")
    return [row for row in rows if isinstance(row, dict)]


def _parse_two_pass_candidates(
    content: str,
    document: dict[str, Any],
    section: dict[str, Any],
    sentences: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    data = _load_json_content(content)
    if isinstance(data, dict):
        candidates = data.get("candidates")
    elif isinstance(data, list):
        candidates = data
    else:
        candidates = None
    if not isinstance(candidates, list):
        raise ExtractionError("malformed_response: expected a JSON array")
    by_id = {str(sentence["id"]): sentence for sentence in sentences}
    doc_id = str(document.get("paper_id") or str(document.get("sha256") or "")[:16])
    kind_attribute = {
        "dose": "剂量",
        "duration": "疗程",
        "contraindication": "禁忌",
        "combination": "联合治疗",
        "followup": "随访",
    }
    generic_interventions = {
        "药物",
        "治疗",
        "口服给药",
        "静脉滴注",
        "静脉注射",
        "给药",
        "抗生素",
        "用药",
        "drug",
        "treatment",
    }
    parsed: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        item = _coerce_candidate(item)
        sentence = by_id.get(str(item.get("sentence_id") or "").strip())
        if sentence is None:
            continue
        population = item.get("population")
        attribute = item.get("attribute") or kind_attribute.get(str(sentence.get("kind") or ""))
        value = item.get("value")
        direction = item.get("direction")
        intervention = str(item.get("intervention") or "").strip()
        if not population or not attribute or not intervention:
            continue
        if intervention in generic_interventions:
            continue
        if not isinstance(value, dict):
            if direction in DIRECTIONS:
                value = {"kind": "direction", "value": direction}
            else:
                continue
        evidence = item.get("evidence_type")
        if evidence not in EVIDENCE_TYPES:
            doc_evidence = document.get("evidence_type")
            evidence = doc_evidence if doc_evidence in EVIDENCE_TYPES else None
        parsed.append(
            {
                "population": population,
                "severity": item.get("severity"),
                "intervention": intervention,
                "attribute": attribute,
                "value": value,
                "direction": direction,
                "evidence_type": evidence,
                "ae_type": item.get("ae_type"),
                "arm": item.get("arm"),
                "metric": item.get("metric"),
                "denominator": item.get("denominator"),
                "confidence": item.get("confidence", 0.7),
                "anchors": [
                    {
                        "doc_id": doc_id,
                        "page_start": section.get("page_start"),
                        "page_end": section.get("page_end"),
                        "quote": sentence["quote"],
                    }
                ],
            }
        )
    return parsed


class TwoPassBackend(RealBackend):
    """Sentence-first extraction: select verbatim sentences, then claims anchored to them."""

    def extract(self, document: dict[str, Any], section: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            content = self._chat(self._pass1_prompt(document, section))
            sentences = self._select_sentences(content, section)
        except ExtractionError:
            return super().extract(document, section)
        if not sentences:
            return []
        content = self._chat(self._pass2_prompt(document, section, sentences))
        return _parse_two_pass_candidates(content, document, section, sentences)

    def _pass1_prompt(self, document: dict[str, Any], section: dict[str, Any]) -> str:
        return (
            "任务：从下面的正文中挑选包含可比较医学结论的原句。\n"
            f"文档标题：{document.get('title') or ''}\n"
            f"章节：{section.get('path') or ''}\n"
            f"正文：\n{section.get('text') or ''}\n\n"
            "原句范围：治疗选择、剂量、疗程、禁忌、联合治疗、随访、严重度、化验阈值。\n"
            "要求：quote 必须逐字复制正文中的连续原句，不改写、不拼接；"
            "没有合适原句就输出 {\"sentences\":[]}。\n"
            "kind 取 treatment|dose|duration|contraindication|combination|followup|severity|lab|other。\n"
            "只输出 JSON：{\"sentences\":[{\"id\":\"s1\",\"quote\":\"...\",\"kind\":\"dose\"}]}"
        )

    def _pass2_prompt(
        self, document: dict[str, Any], section: dict[str, Any], sentences: list[dict[str, Any]]
    ) -> str:
        if self.include_labs:
            attribute_options = TREATMENT_LABELS + "、" + LAB_LABELS
            scope_line = "只允许引用下列句子；化验项放 attribute，不要放 intervention。"
        else:
            attribute_options = TREATMENT_LABELS
            scope_line = "只允许引用下列句子；默认只收治疗类结论，不抽化验指标。"
        allowed = "\n".join(
            f"[{item['id']}] kind={item.get('kind') or 'other'} {item['quote']}"
            for item in sentences
        )
        example = (
            '{"candidates":[{"sentence_id":"s1","population":"儿童","severity":"轻症",'
            '"intervention":"阿奇霉素","attribute":"剂量",'
            '"value":{"kind":"numeric","value":10,"unit":"mg/kg/day"},'
            '"direction":null,"evidence_type":"guideline","confidence":0.9}]}'
        )
        return (
            "从给定句子中抽取一个或多个 claim，每个 claim 必须引用 sentence_id。\n"
            f"文档标题：{document.get('title') or ''}\n"
            f"章节正文（仅用于识别药物/人群/表头，不得作为引用来源）：\n"
            f"{str(section.get('text') or '')[:2500]}\n\n"
            f"可用引用句子：\n{allowed}\n\n"
            f"{scope_line}\n"
            "字段：sentence_id、population、severity、intervention、attribute、value、direction、"
            "evidence_type、confidence；不良反应还需 ae_type/arm/metric/denominator。\n"
            "intervention 必须是具体的药物名（如 阿奇霉素、米诺环素）；"
            "若只能写成 治疗/药物/口服给药/静脉滴注 等泛称，就不要输出该候选。\n"
            f"attribute 取：{attribute_options}。\n"
            "每个候选必须同时包含 sentence_id、population、intervention、attribute、value；"
            "缺任一字段就不要输出该候选。population 用 儿童/青少年/8岁以下/成人。\n"
            "evidence_type 只能取 guideline|systematic_review|randomized_controlled_trial|"
            "observational_study|narrative_review|case_report 或 null，不要写 treatment。\n"
            "value：numeric 需 value+unit；range 需 low/high；direction 取 first_line|alternative|avoid。\n"
            "不良反应必须额外输出 ae_type（overall|rash|gi|liver|local|other）、"
            "arm（observation|control|unspecified）、metric（rate|count）、denominator；"
            "观察组与对照组不可合并。\n"
            f"只输出 JSON，格式示例：{example}\n"
            "没有可抽取结论时输出 {\"candidates\":[]}。"
        )

    def _select_sentences(self, content: str, section: dict[str, Any]) -> list[dict[str, Any]]:
        rows = _parse_sentences(content)
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            snapped = _snap_quote(str(section.get("text") or ""), row.get("quote") or row.get("text"))
            if not snapped or snapped in seen:
                continue
            seen.add(snapped)
            selected.append(
                {
                    "id": f"s{len(selected) + 1}",
                    "quote": snapped,
                    "kind": row.get("kind"),
                }
            )
        return selected


def validate_candidate(
    payload: Any,
    document: dict[str, Any],
    section: dict[str, Any],
) -> list[str]:
    """Return a list of human readable problems; empty means valid."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["candidate must be a JSON object"]

    for key in ("population", "intervention", "attribute"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} must be a non-empty string")

    severity = payload.get("severity")
    if severity is not None and (not isinstance(severity, str) or not severity.strip()):
        errors.append("severity must be a non-empty string or null")

    value = payload.get("value")
    if not isinstance(value, dict):
        errors.append("value must be an object")
    else:
        kind = value.get("kind")
        if kind not in VALUE_KINDS:
            errors.append(f"value.kind must be one of {sorted(VALUE_KINDS)}")
        elif kind == "numeric":
            number = value.get("value")
            if not isinstance(number, (int, float)) or isinstance(number, bool):
                errors.append("numeric value must be a number")
        elif kind == "range":
            low, high = value.get("low"), value.get("high")
            numeric = (int, float)
            if (
                not isinstance(low, numeric)
                or not isinstance(high, numeric)
                or isinstance(low, bool)
                or isinstance(high, bool)
                or low > high
            ):
                errors.append("range requires low <= high numbers")
        elif kind == "direction":
            if value.get("value") not in DIRECTIONS:
                errors.append(f"direction value must be one of {sorted(DIRECTIONS)}")

    direction = payload.get("direction")
    if direction is not None and direction not in DIRECTIONS:
        errors.append(f"direction must be one of {sorted(DIRECTIONS)} or null")

    evidence_type = payload.get("evidence_type")
    if evidence_type is not None and evidence_type not in EVIDENCE_TYPES:
        errors.append(f"evidence_type must be one of {sorted(EVIDENCE_TYPES)} or null")

    ae_type = payload.get("ae_type")
    if ae_type is not None and ae_type not in AE_TYPES:
        errors.append(f"ae_type must be one of {sorted(AE_TYPES)} or null")
    arm = payload.get("arm")
    if arm is not None and arm not in AE_ARMS:
        errors.append(f"arm must be one of {sorted(AE_ARMS)} or null")
    metric = payload.get("metric")
    if metric is not None and metric not in AE_METRICS:
        errors.append(f"metric must be one of {sorted(AE_METRICS)} or null")
    denominator = payload.get("denominator")
    if denominator is not None and (
        not isinstance(denominator, (int, float)) or isinstance(denominator, bool)
    ):
        errors.append("denominator must be a number or null")

    confidence = payload.get("confidence")
    if confidence is not None:
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            errors.append("confidence must be a number between 0 and 1")
        elif not 0.0 <= float(confidence) <= 1.0:
            errors.append("confidence must be between 0 and 1")

    anchors = payload.get("anchors")
    if not isinstance(anchors, list) or not anchors:
        errors.append("anchors must be a non-empty array")
        return errors

    paper_id = str(document.get("paper_id"))
    section_start = section.get("page_start")
    section_end = section.get("page_end")
    section_text = _norm(str(section.get("text", "")))
    compact_section = _compact(section_text)

    for index, anchor in enumerate(anchors):
        if not isinstance(anchor, dict):
            errors.append(f"anchor[{index}] must be an object")
            continue
        doc_id = anchor.get("doc_id")
        if doc_id != paper_id:
            errors.append(f"anchor[{index}] doc_id must be the paper id")
        page_start = anchor.get("page_start")
        page_end = anchor.get("page_end")
        if (
            not isinstance(page_start, int)
            or not isinstance(page_end, int)
            or page_start < 1
            or page_end < page_start
        ):
            errors.append(f"anchor[{index}] has an invalid page range")
        elif section_start is not None and section_end is not None:
            if page_start < section_start or page_end > section_end:
                errors.append(f"anchor[{index}] page range is outside the section")
        quote = anchor.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            errors.append(f"anchor[{index}] quote must be a non-empty string")
        else:
            quote_compact = _compact(quote)
            if (
                _norm(quote) not in section_text
                and quote_compact not in compact_section
                and _quote_coverage(quote_compact, compact_section) < 0.85
            ):
                errors.append(f"anchor[{index}] quote_not_found in section text")

    return errors
