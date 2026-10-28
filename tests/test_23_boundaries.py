"""Ticket 23 acceptance: real-corpus article boundaries, metadata and quality.

Every assertion goes through the public CLI seam (``python -m mppkb inspect
--json``), the same seam ticket 02 uses. The fixtures are synthetic documents
built by ``tests/fixtures/build_fixtures.py`` so page geometry, header lines
and evidence signals are known exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

from support import run_cli

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINI = FIXTURES / "mini_two_pages.pdf"
LONG = FIXTURES / "long_section.pdf"
TWO_ARTICLES = FIXTURES / "one_page_two_articles.pdf"
CREATIONDATE = FIXTURES / "creationdate_not_year.pdf"
GUIDELINE = FIXTURES / "evidence_guideline.pdf"
SYSTEMATIC_REVIEW = FIXTURES / "evidence_systematic_review.pdf"
RCT = FIXTURES / "evidence_rct.pdf"
OBSERVATIONAL = FIXTURES / "evidence_observational.pdf"
GARBLED = FIXTURES / "garbled_text.pdf"
FULLWIDTH = FIXTURES / "fullwidth_spacing.pdf"
PARTIAL_SCAN = FIXTURES / "partial_scan.pdf"
THESIS = FIXTURES / "thesis_like.pdf"

TWO_ARTICLES_TITLE = "难治性肺炎支原体肺炎患儿支气管肺泡灌洗治疗的临床研究"
NEXT_ARTICLE_TITLE = "针灸流派介绍（二）"
CREATIONDATE_DOI = "10.11855/j.issn.0577-7402.0981.2019.0124"


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
    return result, json.loads(result.stdout)


def body_text(payload) -> str:
    return "\n".join(section["text"] for section in payload["sections"])


def test_one_page_two_articles_keeps_only_the_target_article(tmp_path):
    result, payload = inspect_json(tmp_path, TWO_ARTICLES)
    assert result.returncode == 0, result.stderr

    # The target article sits between the previous tail and the next title on
    # page 1, so both boundaries are inside the first page.
    assert payload["article_start"] == 1
    assert payload["article_end"] == 1
    assert payload["pages"] == 2
    assert all(section["page_start"] == 1 for section in payload["sections"])
    assert all(section["page_end"] == 1 for section in payload["sections"])

    text = body_text(payload)
    assert "支气管肺泡灌洗" in text
    assert "关键词：肺炎支原体" in text
    assert "阿奇霉素抗感染治疗" in text

    # The previous article's tail (references, receipt date) is cut away.
    assert "刘洋" not in text
    assert "收稿日期" not in text
    # The next article's title, abstract and page-2 body are cut away too.
    assert NEXT_ARTICLE_TITLE not in text
    assert "岭南针灸流派" not in text
    assert "下一页针灸内容" not in text

    # The multi-line title is merged, and the title stays document metadata.
    assert payload["title"] == TWO_ARTICLES_TITLE
    assert TWO_ARTICLES_TITLE not in text


def test_page_header_year_journal_and_doi_are_extracted(tmp_path):
    result, payload = inspect_json(tmp_path, CREATIONDATE)
    assert result.returncode == 0, result.stderr

    assert payload["year"] == 2019
    assert payload["metadata_sources"]["year"] == "header"
    assert payload["publisher"] == "测试医学杂志"
    assert payload["metadata_sources"]["publisher"] == "header"
    assert payload["doi"] == CREATIONDATE_DOI
    assert payload["metadata_sources"]["doi"] == "page_text"
    assert payload["authors"] == ["赵一", "钱二"]
    assert payload["metadata_sources"]["authors"] == "page_layout"
    assert payload["title"] == "儿童肺炎支原体肺炎治疗研究"
    assert payload["metadata_sources"]["title"] == "page_layout"

    # Labelled DOI on the adjacent-article fixture is found as well.
    result, payload = inspect_json(tmp_path, TWO_ARTICLES)
    assert result.returncode == 0, result.stderr
    assert payload["doi"] == "10.3969/j.issn.0253-9802.2024.07.001"


def test_unreliable_sources_yield_none_instead_of_guessing(tmp_path):
    result, payload = inspect_json(tmp_path, LONG)
    assert result.returncode == 0, result.stderr

    # The fixture has creationDate 2024 but no publication year anywhere in
    # the text layer, and no journal header or DOI.
    assert payload["year"] is None
    assert payload["publisher"] is None
    assert payload["doi"] is None
    assert payload["authors"] == []
    assert "year" not in payload["metadata_sources"]
    assert "publisher" not in payload["metadata_sources"]
    assert "doi" not in payload["metadata_sources"]
    # Existing ticket-02 behaviour: PDF metadata title is still the fallback.
    assert payload["title"] == "长章节测试夹具"
    assert payload["metadata_sources"]["title"] == "pdf_metadata"


def test_creationdate_is_never_treated_as_publication_year(tmp_path):
    # The fixture's PDF creationDate is D:20260703203626 (a CNKI export time)
    # while the article was published in 2019.
    result, payload = inspect_json(tmp_path, CREATIONDATE)
    assert result.returncode == 0, result.stderr

    assert payload["year"] == 2019
    assert payload["year"] != 2026
    assert payload["metadata_sources"].get("year") != "pdf_metadata"
    assert payload["metadata_sources"].get("year") != "creationDate"


def test_evidence_type_scores_the_four_designs_with_confidence(tmp_path):
    cases = [
        (GUIDELINE, "guideline"),
        (SYSTEMATIC_REVIEW, "systematic_review"),
        (RCT, "randomized_controlled_trial"),
        (OBSERVATIONAL, "observational_study"),
    ]
    for pdf, expected in cases:
        result, payload = inspect_json(tmp_path, pdf)
        assert result.returncode == 0, result.stderr
        assert payload["evidence_type"] == expected, (pdf.name, payload["evidence_type"])
        assert isinstance(payload["evidence_confidence"], (int, float))
        assert 0.5 <= payload["evidence_confidence"] <= 1.0
        assert payload["metadata_sources"]["evidence_type"] == "rules"

    # Nothing conclusive in a plain methods section: report None, do not guess.
    result, payload = inspect_json(tmp_path, LONG)
    assert result.returncode == 0, result.stderr
    assert payload["evidence_type"] is None
    assert payload["evidence_confidence"] is None


def test_quality_gate_flags_broken_text_layers_and_thesis_length(tmp_path):
    result, payload = inspect_json(tmp_path, GARBLED)
    assert result.returncode == 0, result.stderr
    assert "garbled_text" in payload["quality_flags"]

    result, payload = inspect_json(tmp_path, FULLWIDTH)
    assert result.returncode == 0, result.stderr
    assert "letter_spacing" in payload["quality_flags"]

    result, payload = inspect_json(tmp_path, PARTIAL_SCAN)
    assert result.returncode == 0, result.stderr
    assert "no_text_layer" in payload["quality_flags"]

    result, payload = inspect_json(tmp_path, THESIS)
    assert result.returncode == 0, result.stderr
    assert payload["pages"] == 30
    assert "thesis_route" in payload["quality_flags"]
    # The thesis route never applies journal article-boundary cutting.
    assert payload["article_start"] == 1
    assert payload["article_end"] == 30
    # A cover page's largest text ("硕士学位论文", 36 pt) is not the title.
    assert payload["title"] == "儿童难治性肺炎支原体肺炎临床研究"

    # A clean two-page paper has no flags.
    result, payload = inspect_json(tmp_path, MINI)
    assert result.returncode == 0, result.stderr
    assert payload["quality_flags"] == []


def test_ticket_02_payload_stays_backward_compatible(tmp_path):
    result, payload = inspect_json(tmp_path, MINI)
    assert result.returncode == 0, result.stderr

    # The eight original top-level keys are unchanged and keep their meaning.
    assert set(payload) >= {
        "pdf",
        "sha256",
        "title",
        "year",
        "publisher",
        "evidence_type",
        "pages",
        "sections",
    }
    # Ticket 23 adds its fields instead of replacing anything.
    assert set(payload) >= {
        "article_start",
        "article_end",
        "quality_flags",
        "metadata_sources",
        "evidence_confidence",
    }
    assert payload["title"] == "肺炎支原体肺炎诊疗指南（测试夹具）"
    assert payload["year"] == 2024
    assert payload["publisher"] == "测试出版社"
    assert payload["evidence_type"] == "guideline"
    assert payload["pages"] == 2

    sections = payload["sections"]
    assert [s["section_path"] for s in sections] == [
        "一、病原学",
        "二、临床表现",
        "三、治疗原则",
        "四、随访",
    ]
    for section in sections:
        assert set(section) == {
            "section_path",
            "page_start",
            "page_end",
            "text",
            "token_count",
            "chunk_hash",
        }
