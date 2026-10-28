"""Ticket 02 acceptance: PDF parsing, section segmentation and inspect JSON."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from support import run_cli

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINI = FIXTURES / "mini_two_pages.pdf"
LONG = FIXTURES / "long_section.pdf"
NO_TEXT = FIXTURES / "no_text.pdf"
NOT_A_PDF = FIXTURES / "not_a_pdf.pdf"

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[._%+\-/][A-Za-z0-9]+)*")


def estimate_tokens(text: str) -> int:
    """Token estimate fixed by the ticket: CJK chars + ASCII words."""
    return len(CJK_RE.findall(text)) + len(ASCII_WORD_RE.findall(text))


def env_for(tmp_path: Path) -> dict[str, str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    return {
        "MPPKB_KB_DB": str(tmp_path / "data" / "kb.sqlite"),
        "MPPKB_CORPUS_DB": str(tmp_path / "data" / "corpus.sqlite"),
        "MPPKB_CORPUS_DIR": str(corpus),
    }


def inspect_json(cwd: Path, pdf: Path):
    result = run_cli(cwd, "inspect", str(pdf), "--json", env_extra=env_for(cwd))
    payload = json.loads(result.stdout)
    return result, payload


def test_mini_fixture_sections_and_page_ranges(tmp_path):
    result, payload = inspect_json(tmp_path, MINI)

    assert result.returncode == 0, result.stderr
    assert payload["ok"] is True
    assert payload["pdf"] == str(MINI)
    assert payload["sha256"] == hashlib.sha256(MINI.read_bytes()).hexdigest()
    assert payload["pages"] == 2

    # Best-effort metadata: title/year/publisher/evidence type.
    assert payload["title"] == "肺炎支原体肺炎诊疗指南（测试夹具）"
    assert payload["year"] == 2024
    assert payload["publisher"] == "测试出版社"
    assert payload["evidence_type"] == "guideline"

    sections = payload["sections"]
    assert [s["section_path"] for s in sections] == [
        "一、病原学",
        "二、临床表现",
        "三、治疗原则",
        "四、随访",
    ]
    assert [(s["page_start"], s["page_end"]) for s in sections] == [
        (1, 1),
        (1, 2),  # the section continues onto page 2: pages are merged
        (2, 2),
        (2, 2),
    ]

    assert "肺炎支原体是儿童社区获得性肺炎" in sections[0]["text"]
    assert "起病缓慢" in sections[1]["text"]
    assert "需结合影像学检查综合判断" in sections[1]["text"]
    assert "大环内酯类抗菌药物" in sections[2]["text"]
    assert "随访观察" in sections[3]["text"]

    combined = "\n".join(section["text"] for section in sections)
    assert "MPP 知识库测试夹具" not in combined  # repeated running header
    assert "第 1 页" not in combined and "第 2 页" not in combined  # page footer
    assert "诊疗指南（测试夹具）" not in combined  # title stays document metadata


def test_inspect_json_contract_is_script_consumable(tmp_path):
    result, payload = inspect_json(tmp_path, MINI)

    assert result.returncode == 0
    assert result.stdout.strip().startswith("{")
    assert set(payload) >= {"ok", "pdf", "sha256", "pages", "sections"}
    assert re.fullmatch(r"[0-9a-f]{64}", payload["sha256"])
    assert isinstance(payload["pages"], int) and payload["pages"] == 2

    paper_id = payload["sha256"][:16]
    for section in payload["sections"]:
        assert set(section) == {
            "section_path",
            "page_start",
            "page_end",
            "text",
            "token_count",
            "chunk_hash",
        }
        assert isinstance(section["section_path"], str) and section["section_path"]
        assert isinstance(section["page_start"], int)
        assert isinstance(section["page_end"], int)
        assert 1 <= section["page_start"] <= section["page_end"] <= payload["pages"]
        assert isinstance(section["text"], str) and section["text"].strip()
        assert isinstance(section["token_count"], int) and section["token_count"] >= 1
        assert re.fullmatch(r"[0-9a-f]{16}", section["chunk_hash"])

        # The ticket fixes both deterministic formulas.
        assert section["token_count"] == estimate_tokens(section["text"])
        expected_hash = hashlib.sha256(
            (
                paper_id
                + section["section_path"]
                + str(section["page_start"])
                + section["text"]
            ).encode("utf-8")
        ).hexdigest()[:16]
        assert section["chunk_hash"] == expected_hash

    # Whole payload is ordinary JSON, nothing exotic for downstream scripts.
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_corrupt_pdf_fails_with_readable_error(tmp_path):
    result, payload = inspect_json(tmp_path, NOT_A_PDF)

    assert result.returncode == 1
    assert payload["ok"] is False
    assert isinstance(payload["error"], str) and payload["error"].strip()
    assert "pdf" in payload["error"].lower()
    assert "sections" not in payload  # no half-produced data


def test_missing_pdf_fails_with_readable_error(tmp_path):
    result, payload = inspect_json(tmp_path, tmp_path / "does-not-exist.pdf")

    assert result.returncode == 1
    assert payload["ok"] is False
    assert "not found" in payload["error"].lower()


def test_pdf_without_text_layer_fails(tmp_path):
    result, payload = inspect_json(tmp_path, NO_TEXT)

    assert result.returncode == 1
    assert payload["ok"] is False
    assert "text" in payload["error"].lower()
    assert "sections" not in payload


def test_repeated_inspect_is_byte_identical(tmp_path):
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()

    first = run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env_for(tmp_path))
    second = run_cli(other_cwd, "inspect", str(MINI), "--json", env_extra=env_for(other_cwd))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout
    assert first.stdout.encode("utf-8") == second.stdout.encode("utf-8")


def test_long_section_is_split_at_paragraph_boundary_and_keeps_path(tmp_path):
    result, payload = inspect_json(tmp_path, LONG)

    assert result.returncode == 0, result.stderr
    sections = payload["sections"]
    assert [s["section_path"] for s in sections] == [
        "一、长章节",
        "一、长章节",  # split chunk keeps the same section_path
        "二、短章节",
    ]
    assert [(s["page_start"], s["page_end"]) for s in sections] == [
        (1, 2),  # first chunk packs paragraphs across the page break
        (2, 2),
        (2, 2),
    ]

    assert all(s["token_count"] <= 2000 for s in sections)
    long_chunks = [s for s in sections if s["section_path"] == "一、长章节"]
    assert len(long_chunks) == 2
    assert long_chunks[0]["token_count"] > 1900
    assert long_chunks[1]["text"].startswith("段落06：")
    assert "段落01：" in long_chunks[0]["text"]
    assert "段落05：" in long_chunks[0]["text"]
    assert "段落06：" not in long_chunks[0]["text"]
    assert len({s["chunk_hash"] for s in sections}) == len(sections)


def test_every_chunk_obeys_the_token_limit(tmp_path):
    for pdf in (MINI, LONG):
        result, payload = inspect_json(tmp_path, pdf)
        assert result.returncode == 0, result.stderr
        assert payload["sections"]
        for section in payload["sections"]:
            assert section["token_count"] <= 2000
            assert section["token_count"] == estimate_tokens(section["text"])
