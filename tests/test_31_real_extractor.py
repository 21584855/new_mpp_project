"""Real extraction backend acceptance: parsing, anchors and error handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mppkb.extract import ExtractionError, RealBackend, validate_candidate

DOCUMENT = {
    "paper_id": "a" * 16,
    "sha256": "a" * 64,
    "title": "儿童肺炎支原体肺炎诊疗指南",
    "year": 2023,
}
SECTION = {
    "page_start": 3,
    "page_end": 3,
    "path": ["治疗"],
    "text": "轻症患者予阿奇霉素 10 mg/kg，每日 1 次，疗程 3 天。",
}


def _transport(content: str):
    calls = {"n": 0}

    def transport(model, messages, timeout):
        calls["n"] += 1
        assert messages[0]["role"] == "system"
        assert "阿奇霉素" in messages[1]["content"]
        return content

    transport.calls = calls
    return transport


def test_parses_candidates_and_fills_anchors():
    content = json.dumps(
        [
            {
                "population": "儿童",
                "severity": "轻症",
                "intervention": "阿奇霉素",
                "attribute": "剂量",
                "value": {"kind": "numeric", "value": 10, "unit": "mg/kg"},
                "direction": None,
                "evidence_type": "guideline",
                "anchors": [{"quote": "阿奇霉素 10 mg/kg"}],
                "confidence": 0.9,
            }
        ],
        ensure_ascii=False,
    )
    backend = RealBackend(transport=_transport(content), api_key="test")
    payloads = backend.extract(DOCUMENT, SECTION)

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["intervention"] == "阿奇霉素"
    assert payload["anchors"][0]["doc_id"] == "a" * 16
    assert payload["anchors"][0]["page_start"] == 3
    assert payload["anchors"][0]["quote"] == "阿奇霉素 10 mg/kg"
    assert validate_candidate(payload, DOCUMENT, SECTION) == []


def test_parses_fenced_json_and_candidates_object():
    content = "```json\n" + json.dumps(
        {"candidates": [{"population": "儿童", "intervention": "阿奇霉素", "attribute": "疗程",
                         "value": {"kind": "numeric", "value": 3, "unit": "days"},
                         "anchors": [{"quote": "疗程 3 天"}]}]},
        ensure_ascii=False,
    ) + "\n```"
    backend = RealBackend(transport=_transport(content), api_key="test")
    payloads = backend.extract(DOCUMENT, SECTION)
    assert len(payloads) == 1
    assert payloads[0]["attribute"] == "疗程"


def test_malformed_response_raises():
    backend = RealBackend(transport=_transport("这不是 JSON"), api_key="test")
    with pytest.raises(ExtractionError, match="malformed_response"):
        backend.extract(DOCUMENT, SECTION)


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("MPPKB_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    backend = RealBackend(api_key="")
    with pytest.raises(ExtractionError, match="API_KEY"):
        backend.extract(DOCUMENT, SECTION)


def test_coerces_chinese_enums_and_numeric_strings():
    content = json.dumps(
        {
            "candidates": [
                {
                    "population": "儿童",
                    "severity": None,
                    "intervention": "阿奇霉素",
                    "attribute": "一线治疗",
                    "value": {"kind": "direction", "value": "推荐"},
                    "direction": "推荐",
                    "evidence_type": "指南",
                    "anchors": [{"quote": "轻症患者予阿奇霉素"}],
                },
                {
                    "population": "儿童",
                    "severity": None,
                    "intervention": "阿奇霉素",
                    "attribute": "剂量",
                    "value": {"kind": "numeric", "value": "10", "unit": "mg/kg"},
                    "direction": None,
                    "evidence_type": "指南",
                    "anchors": [{"quote": "阿奇霉素 10 mg/kg"}],
                },
                {
                    "population": "儿童",
                    "severity": None,
                    "intervention": "阿奇霉素",
                    "attribute": "疗程",
                    "value": {"kind": "range", "low": 7, "high": 3, "unit": "days"},
                    "direction": None,
                    "evidence_type": "指南",
                    "anchors": [{"quote": "疗程 3 天"}],
                },
            ]
        },
        ensure_ascii=False,
    )
    backend = RealBackend(transport=_transport(content), api_key="test")
    payloads = backend.extract(DOCUMENT, SECTION)
    assert payloads[0]["direction"] == "first_line"
    assert payloads[0]["value"]["value"] == "first_line"
    assert payloads[0]["evidence_type"] == "guideline"
    assert payloads[1]["value"]["value"] == 10.0
    assert payloads[2]["value"]["low"] == 3 and payloads[2]["value"]["high"] == 7


def test_prompt_scope_and_structured_output(monkeypatch):
    backend = RealBackend(transport=_transport('{"candidates":[]}'), api_key="test")
    default_prompt = backend._prompt(DOCUMENT, SECTION)
    assert "只收治疗类" in default_prompt
    assert "不要输出化验指标" in default_prompt
    assert "化验项必须放在 attribute" not in default_prompt

    labs = RealBackend(
        transport=_transport('{"candidates":[]}'), api_key="test", include_labs=True
    )
    labs_prompt = labs._prompt(DOCUMENT, SECTION)
    assert "血糖" in labs_prompt and "化验项必须放在 attribute" in labs_prompt

    monkeypatch.delenv("MPPKB_LLM_JSON_MODE", raising=False)
    captured: dict = {}

    def fake_post(payload):
        captured.update(payload)
        return {
            "choices": [{"message": {"content": '{"candidates":[]}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    direct = RealBackend(api_key="test")
    direct._post = fake_post  # type: ignore[method-assign]
    direct._chat("prompt")
    assert captured["response_format"] == {"type": "json_object"}
