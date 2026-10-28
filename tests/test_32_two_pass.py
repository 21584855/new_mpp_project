"""Two-pass extraction acceptance (ticket 32 B)."""

from __future__ import annotations

import json

from mppkb.extract import TwoPassBackend, validate_candidate

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


def _transport(responses):
    calls = {"n": 0}

    def transport(model, messages, timeout):
        index = calls["n"]
        calls["n"] += 1
        return responses[index]

    transport.calls = calls
    return transport


def test_selects_sentence_then_anchors_claim_to_exact_substring():
    pass1 = json.dumps(
        {"sentences": [{"id": "s1", "quote": "轻症患者予阿奇霉素10mg/kg 每日1次", "kind": "dose"}]},
        ensure_ascii=False,
    )
    pass2 = json.dumps(
        {
            "candidates": [
                {
                    "sentence_id": "s1",
                    "population": "儿童",
                    "severity": "轻症",
                    "intervention": "阿奇霉素",
                    "attribute": "剂量",
                    "value": {"kind": "numeric", "value": 10, "unit": "mg/kg"},
                    "direction": None,
                    "evidence_type": "guideline",
                }
            ]
        },
        ensure_ascii=False,
    )
    backend = TwoPassBackend(transport=_transport([pass1, pass2]), api_key="test")
    payloads = backend.extract(DOCUMENT, SECTION)

    assert len(payloads) == 1
    quote = payloads[0]["anchors"][0]["quote"]
    assert quote in SECTION["text"]
    assert payloads[0]["anchors"][0]["page_start"] == 3
    assert validate_candidate(payloads[0], DOCUMENT, SECTION) == []


def test_no_sentences_skips_second_pass():
    transport = _transport([json.dumps({"sentences": []})])
    backend = TwoPassBackend(transport=transport, api_key="test")
    assert backend.extract(DOCUMENT, SECTION) == []
    assert transport.calls["n"] == 1


def test_unknown_sentence_id_is_dropped():
    pass1 = json.dumps({"sentences": [{"id": "s1", "quote": "阿奇霉素 10 mg/kg"}]})
    pass2 = json.dumps(
        {
            "candidates": [
                {
                    "sentence_id": "s9",
                    "population": "儿童",
                    "intervention": "阿奇霉素",
                    "attribute": "剂量",
                    "value": {"kind": "numeric", "value": 10, "unit": "mg/kg"},
                }
            ]
        }
    )
    backend = TwoPassBackend(transport=_transport([pass1, pass2]), api_key="test")
    assert backend.extract(DOCUMENT, SECTION) == []


def test_malformed_first_pass_falls_back_to_single_pass():
    fallback = json.dumps(
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
            }
        ],
        ensure_ascii=False,
    )
    backend = TwoPassBackend(transport=_transport(["not json", fallback]), api_key="test")
    payloads = backend.extract(DOCUMENT, SECTION)
    assert len(payloads) == 1
    assert payloads[0]["intervention"] == "阿奇霉素"
