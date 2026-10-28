"""Build the synthetic PDF fixtures used by the ticket 02 and ticket 23 tests.

The fixtures are deliberately tiny and fully controlled: their page numbers,
section headings and body text are known, so acceptance tests can assert exact
page ranges and split points.

Run from anywhere with::

    python tests/fixtures/build_fixtures.py

The generated files are deterministic (fixed metadata, ``no_new_id`` and
``reproducible`` save options) and are committed to git. Re-running the script
must produce byte-identical PDFs with the pinned PyMuPDF version.

Ticket 23 adds layout fixtures for the real-corpus problems: adjacent articles
on one page run, a ``creationDate`` that is not the publication year, the four
evidence-type shapes, and text-layer quality problems.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

FIXTURES_DIR = Path(__file__).resolve().parent

PAGE_WIDTH = 595
PAGE_HEIGHT = 842
BODY_FONT_SIZE = 10.5
HEADING_FONT_SIZE = 14
TITLE_FONT_SIZE = 18
ARTICLE_TITLE_FONT_SIZE = 17
SMALL_FONT_SIZE = 9
CHROME_FONT_SIZE = 8

CJK_FONT = "china-s"

MINI_TITLE = "肺炎支原体肺炎诊疗指南（测试夹具）"
MINI_HEADER = "MPP 知识库测试夹具"

LONG_TITLE = "长章节测试夹具"
LONG_HEADING = "一、长章节"
LONG_SHORT_HEADING = "二、短章节"

NO_TEXT_TITLE = "无文本层测试夹具"

FIXED_METADATA = {
    "author": "MPPKB test fixtures",
    "creator": "tests/fixtures/build_fixtures.py",
    "producer": "PyMuPDF",
    "creationDate": "D:20240101000000",
    "modDate": "D:20240101000000",
}


def _metadata(title: str, keywords: str = "") -> dict[str, str]:
    data = dict(FIXED_METADATA)
    data.update({"title": title, "subject": "", "keywords": keywords})
    return data


def _page(doc: pymupdf.Document) -> pymupdf.Page:
    return doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)


def _text(page: pymupdf.Page, x: float, y: float, text: str, size: float) -> None:
    page.insert_text(
        (x, y),
        text,
        fontname=CJK_FONT,
        fontsize=size,
        color=(0, 0, 0),
    )


def _paragraph(
    page: pymupdf.Page,
    rect: tuple[float, float, float, float],
    text: str,
    size: float = BODY_FONT_SIZE,
) -> None:
    """Insert one wrapped paragraph as a single text block."""
    leftover = page.insert_textbox(
        pymupdf.Rect(*rect),
        text,
        fontname=CJK_FONT,
        fontsize=size,
        color=(0, 0, 0),
        align=pymupdf.TEXT_ALIGN_LEFT,
    )
    if leftover < 0:
        raise RuntimeError(f"paragraph does not fit fixture layout: {text[:30]!r}")


def _running_header(page: pymupdf.Page, journal: str, issue: str) -> None:
    """Two chrome lines repeated on every page of the shorter fixtures."""
    _text(page, 72, 30, journal, CHROME_FONT_SIZE)
    _text(page, 72, 46, issue, CHROME_FONT_SIZE)


def _save(
    doc: pymupdf.Document,
    name: str,
    title: str,
    keywords: str = "",
    creation_date: str | None = None,
) -> Path:
    metadata = _metadata(title, keywords)
    if creation_date is not None:
        metadata["creationDate"] = creation_date
    doc.set_metadata(metadata)
    path = FIXTURES_DIR / name
    doc.save(path, no_new_id=1, reproducible=True)
    doc.close()
    return path


def build_mini_two_pages() -> Path:
    """Two pages, four numbered sections, one section spanning both pages.

    Page 1: running header, title, sections 一 and 二 (first paragraph).
    Page 2: running header, continuation of section 二, sections 三 and 四,
    page-number footer.
    """
    doc = pymupdf.open()
    page = _page(doc)
    _text(page, 72, 30, MINI_HEADER, CHROME_FONT_SIZE)
    _text(page, 72, 46, "2024 年 7 月第 55 卷第 7 期", CHROME_FONT_SIZE)
    _text(page, 72, 92, MINI_TITLE, TITLE_FONT_SIZE)
    _text(page, 72, 128, "一、病原学", HEADING_FONT_SIZE)
    _paragraph(
        page,
        (72, 142, 523, 222),
        "肺炎支原体是儿童社区获得性肺炎的常见病原体之一，全年均可发病，"
        "以学龄期儿童较为多见。病原体主要经飞沫传播，潜伏期较长。",
    )
    _text(page, 72, 252, "二、临床表现", HEADING_FONT_SIZE)
    _paragraph(
        page,
        (72, 266, 523, 400),
        "起病缓慢，多数患儿以发热和咳嗽为主要表现，咳嗽多为阵发性干咳，"
        "夜间加重。部分患儿可伴有咽痛、头痛和乏力等全身症状。",
    )
    _text(page, 72, 820, "第 1 页", CHROME_FONT_SIZE)

    page = _page(doc)
    _text(page, 72, 30, MINI_HEADER, CHROME_FONT_SIZE)
    _text(page, 72, 46, "2024 年 7 月第 55 卷第 7 期", CHROME_FONT_SIZE)
    _paragraph(
        page,
        (72, 80, 523, 160),
        "重症患儿可出现气促、胸闷和呼吸困难，肺部体征可不明显，"
        "需结合影像学检查综合判断。",
    )
    _text(page, 72, 196, "三、治疗原则", HEADING_FONT_SIZE)
    _paragraph(
        page,
        (72, 210, 523, 330),
        "大环内酯类抗菌药物是治疗肺炎支原体肺炎的首选药物。轻症患儿可口服"
        "阿奇霉素，疗程一般为三至五天；重症患儿应在医生指导下静脉用药。",
    )
    _text(page, 72, 372, "四、随访", HEADING_FONT_SIZE)
    _paragraph(
        page,
        (72, 386, 523, 470),
        "治疗结束后应随访观察症状消退情况。若咳嗽持续超过四周或出现喘息，"
        "应及时复诊评估。",
    )
    _text(page, 72, 820, "第 2 页", CHROME_FONT_SIZE)

    return _save(doc, "mini_two_pages.pdf", MINI_TITLE, "publisher=测试出版社;evidence_type=guideline")


def _filler(base: str, total: int, prefix: str) -> str:
    """Return ``prefix`` plus repeated ``base`` truncated to ``total`` chars."""
    body = (base * (total // len(base) + 2))[: total - len(prefix)]
    return prefix + body


def build_long_section() -> Path:
    """One section whose paragraphs exceed the 2000-token chunk limit.

    Paragraphs 01-04 fit on page 1 (about 1.6k tokens); paragraphs 05-06 push
    the running total past 2000, so the section must split at a paragraph
    boundary and keep the same ``section_path`` across both chunks. Each
    paragraph is about 420 characters but only about 390 tokens because
    punctuation is not a token.
    """
    base = "肺炎支原体肺炎多见于学龄期儿童，临床以发热和咳嗽为主要表现，治疗首选大环内酯类抗菌药物。"
    paragraphs = [_filler(base, 420, f"段落{index:02d}：") for index in range(1, 7)]

    doc = pymupdf.open()
    page = _page(doc)
    _text(page, 72, 80, LONG_HEADING, HEADING_FONT_SIZE)
    top = 100
    for text in paragraphs[:4]:
        _paragraph(page, (72, top, 523, top + 145), text)
        top += 155

    page = _page(doc)
    _paragraph(page, (72, 80, 523, 225), paragraphs[4])
    _paragraph(page, (72, 230, 523, 375), paragraphs[5])
    _text(page, 72, 415, LONG_SHORT_HEADING, HEADING_FONT_SIZE)
    _paragraph(
        page,
        (72, 429, 523, 510),
        "短章节用于确认超长章节切分之后，新的标题仍然会开启新的 section_path。",
    )

    return _save(doc, "long_section.pdf", LONG_TITLE)


def build_no_text() -> Path:
    """A valid PDF with a single page that contains no text layer."""
    doc = pymupdf.open()
    _page(doc)
    return _save(doc, "no_text.pdf", NO_TEXT_TITLE)


def build_not_a_pdf() -> Path:
    """Bytes that are not a PDF at all (failure-path fixture)."""
    path = FIXTURES_DIR / "not_a_pdf.pdf"
    path.write_bytes("this file is not a PDF document\n".encode("utf-8"))
    return path


# ---------------------------------------------------------------------------
# ticket 23 fixtures: adjacency, metadata sources, evidence types, quality
# ---------------------------------------------------------------------------

TWO_ARTICLES_TITLE_LINES = (
    "难治性肺炎支原体肺炎患儿",
    "支气管肺泡灌洗治疗的临床研究",
)
TWO_ARTICLES_TITLE = "".join(TWO_ARTICLES_TITLE_LINES)
NEXT_ARTICLE_TITLE = "针灸流派介绍（二）"


def build_one_page_two_articles() -> Path:
    """A CNKI-style page run: previous article tail, target article, next title.

    Page 1 carries the tail of the *previous* article (references and receipt
    date), then the complete short target article, then the large-font title
    and abstract of the *next* article. Page 2 is entirely the next article, so
    a correct parser keeps page 1 only (``article_start == article_end == 1``).
    """
    doc = pymupdf.open()
    page = _page(doc)
    _running_header(page, "测试医学杂志", "2024 年 7 月第 55 卷第 7 期")
    _paragraph(
        page,
        (72, 78, 523, 150),
        "［18］刘洋，王芳. 儿童肺炎支原体肺炎诊治进展[J]. 中华儿科杂志, "
        "2018, 56(3): 161-165.",
        SMALL_FONT_SIZE,
    )
    _paragraph(
        page,
        (72, 152, 523, 200),
        "［19］陈静. 支气管镜在儿童呼吸系统疾病中的应用[J]. 中国实用儿科杂志, "
        "2017, 32(12): 901-904.",
        SMALL_FONT_SIZE,
    )
    _text(page, 72, 218, "（收稿日期：2022-04-16）", SMALL_FONT_SIZE)

    # The target article starts below 24% of the page, like the real 11473/12684.
    _text(page, 72, 250, TWO_ARTICLES_TITLE_LINES[0], ARTICLE_TITLE_FONT_SIZE)
    _text(page, 72, 272, TWO_ARTICLES_TITLE_LINES[1], ARTICLE_TITLE_FONT_SIZE)
    _text(page, 72, 300, "王小明，李华", BODY_FONT_SIZE)
    _text(page, 72, 316, "测试大学附属儿童医院呼吸科，北京 100020", SMALL_FONT_SIZE)
    _text(page, 72, 332, "DOI：10.3969/j.issn.0253-9802.2024.07.001", SMALL_FONT_SIZE)
    _paragraph(
        page,
        (72, 344, 523, 450),
        "【摘要】目的 观察支气管肺泡灌洗治疗儿童难治性肺炎支原体肺炎的临床"
        "效果。方法 回顾性分析 2019 年 1 月至 2022 年 12 月收治的 86 例患儿。"
        "结果 观察组总有效率高于对照组。结论 支气管肺泡灌洗可改善患儿预后。",
    )
    _text(page, 72, 470, "关键词：肺炎支原体；支气管肺泡灌洗；儿童", BODY_FONT_SIZE)
    _text(page, 72, 496, "1 资料与方法", HEADING_FONT_SIZE)
    _paragraph(
        page,
        (72, 510, 523, 600),
        "1.1 一般资料 选取 2019 年 1 月至 2022 年 12 月在我院住院治疗的难治性"
        "肺炎支原体肺炎患儿 86 例。1.2 治疗方法 两组患儿均给予阿奇霉素抗感染"
        "治疗，观察组在此基础上行支气管肺泡灌洗。",
    )

    # Adjacent article 2 starts on the same page.
    _text(page, 72, 640, NEXT_ARTICLE_TITLE, TITLE_FONT_SIZE)
    _paragraph(
        page,
        (72, 656, 523, 720),
        "【摘要】目的 介绍岭南针灸流派的学术思想与临床经验。方法 梳理相关文献。",
    )
    _text(page, 72, 736, "关键词：针灸；流派", BODY_FONT_SIZE)
    _paragraph(
        page,
        (72, 750, 523, 810),
        "1 流派概述 岭南针灸流派形成于近代，重视经络辨证与针药并用。",
    )

    page = _page(doc)
    _running_header(page, "测试医学杂志", "2024 年 7 月第 55 卷第 7 期")
    _paragraph(
        page,
        (72, 80, 523, 200),
        "下一页针灸内容：2 针法特点 岭南针灸强调候气与催气，常用捻转、"
        "提插等手法。3 临床经验 该流派在痛证、中风后遗症等疾病的治疗中"
        "积累了丰富经验。",
    )
    return _save(doc, "one_page_two_articles.pdf", "", creation_date="D:20260703203626")


def build_creationdate_not_year() -> Path:
    """``creationDate`` is 2026 (CNKI export time) but the header says 2019.

    A parser that trusts ``creationDate`` would report 2026 here.
    """
    doc = pymupdf.open()
    page = _page(doc)
    # One combined header line, like the real "浙江临床医学2018年11月第20卷第11期".
    _text(page, 72, 30, "测试医学杂志2019年3月第12卷第3期", CHROME_FONT_SIZE)
    _text(page, 72, 92, "儿童肺炎支原体肺炎治疗研究", ARTICLE_TITLE_FONT_SIZE)
    _text(page, 72, 122, "赵一，钱二", BODY_FONT_SIZE)
    _text(page, 72, 138, "测试大学附属医院儿科，上海 200032", SMALL_FONT_SIZE)
    _text(page, 72, 154, "10.11855/j.issn.0577-7402.0981.2019.0124", SMALL_FONT_SIZE)
    _paragraph(
        page,
        (72, 168, 523, 210),
        "【摘要】目的 分析儿童肺炎支原体肺炎的临床特点。方法 回顾性分析收治"
        "患儿的临床资料。结果 患儿以发热、咳嗽为主要表现。",
    )
    _text(page, 72, 224, "关键词：肺炎支原体；儿童", BODY_FONT_SIZE)
    _text(page, 72, 248, "1 资料与方法", HEADING_FONT_SIZE)
    _paragraph(
        page,
        (72, 262, 523, 340),
        "选取 2019 年 1 月至 2020 年 12 月收治的患儿 60 例，收集其临床资料并"
        "进行统计分析。",
    )

    page = _page(doc)
    _text(page, 72, 30, "测试医学杂志2019年3月第12卷第3期", CHROME_FONT_SIZE)
    _text(page, 72, 92, "2 结果", HEADING_FONT_SIZE)
    _paragraph(
        page,
        (72, 106, 523, 220),
        "患儿的临床表现以发热、咳嗽为主，部分患儿伴有喘息。所有患儿均接受"
        "阿奇霉素治疗，疗程为 3 至 5 天。",
    )
    return _save(
        doc,
        "creationdate_not_year.pdf",
        "",
        creation_date="D:20260703203626",
    )


def _build_evidence(
    name: str,
    title_lines: tuple[str, ...],
    abstract: str,
    keywords: str,
    body: str,
) -> Path:
    """One-page article used to pin the evidence-type heuristic."""
    doc = pymupdf.open()
    page = _page(doc)
    y = 80.0
    for title_line in title_lines:
        _text(page, 72, y, title_line, ARTICLE_TITLE_FONT_SIZE)
        y += 24
    y += 4
    _text(page, 72, y, "测试作者，测试作者二", BODY_FONT_SIZE)
    y += 16
    _text(page, 72, y, "测试大学附属医院儿科，北京 100020", SMALL_FONT_SIZE)
    y += 20
    _paragraph(page, (72, y, 523, y + 120), abstract)
    y += 126
    _text(page, 72, y, keywords, BODY_FONT_SIZE)
    y += 24
    _text(page, 72, y, "1 资料与方法", HEADING_FONT_SIZE)
    y += 18
    _paragraph(page, (72, y, 523, y + 200), body)
    return _save(doc, name, "")


def build_evidence_guideline() -> Path:
    return _build_evidence(
        "evidence_guideline.pdf",
        ("儿童社区获得性肺炎诊疗指南", "（2024 年版）"),
        "【摘要】本指南基于循证医学证据制定。推荐意见：优先使用大环内酯类"
        "抗菌药物。采用 GRADE 分级，证据等级分为高、中、低、极低，并给出"
        "推荐强度。",
        "关键词：社区获得性肺炎；指南；儿童",
        "1 定义与临床分型 社区获得性肺炎指在医院外罹患的感染性肺实质炎症。"
        "2 诊断 结合临床表现与影像学检查。3 治疗 推荐意见见上文。",
    )


def build_evidence_systematic_review() -> Path:
    return _build_evidence(
        "evidence_systematic_review.pdf",
        ("甲泼尼龙联合阿奇霉素治疗儿童难治性肺炎支原体肺炎的", "Meta分析"),
        "【摘要】目的 系统评价甲泼尼龙联合阿奇霉素的疗效。方法 计算机检索"
        " PubMed、CNKI 和万方数据库，依据纳入标准与排除标准筛选文献，采用"
        " RevMan 5.3 软件进行 Meta 分析，并评估异质性。结果 共纳入 13 项"
        "随机对照研究。",
        "关键词：Meta分析；肺炎支原体；甲泼尼龙",
        "1 资料与方法 检索策略见上文，纳入与排除标准如下。2 结果 森林图"
        "显示合并效应量具有统计学意义。",
    )


def build_evidence_rct() -> Path:
    return _build_evidence(
        "evidence_rct.pdf",
        ("阿奇霉素与甲泼尼龙治疗儿童难治性肺炎支原体肺炎的", "随机对照研究"),
        "【摘要】目的 观察两药联合治疗的临床效果。方法 采用前瞻性研究，"
        "按照随机数字表法分为治疗组和对照组，每组 40 例。结果 治疗组总有"
        "效率高于对照组。",
        "关键词：随机对照；肺炎支原体；阿奇霉素",
        "1 资料与方法 纳入患儿按随机数字表法分组，比较两组的临床疗效。",
    )


def build_evidence_observational() -> Path:
    return _build_evidence(
        "evidence_observational.pdf",
        ("儿童难治性肺炎支原体肺炎的临床特征", "及危险因素分析"),
        "【摘要】目的 分析难治性肺炎支原体肺炎的临床特征及危险因素。方法"
        " 回顾性分析 2019 年 1 月至 2021 年 12 月收治的 120 例患儿的临床"
        "资料。结果 发热持续时间与影像学改变是危险因素。",
        "关键词：肺炎支原体；危险因素；回顾性研究",
        "1 资料与方法 收集 120 例患儿的临床资料，采用多因素 logistic 回归"
        "分析危险因素。",
    )


def build_garbled_text() -> Path:
    """A text layer that extracted to symbols rather than CJK text."""
    garbage = (
        "!\"#$%&'()*+,-./0123456789:;<=>?@[\\]^_`{|}~ "
        "!\"#$%&'()*+,-./0123456789:;<=>?@[\\]^_`{|}~ "
        "!\"#$%&'()*+,-./0123456789:;<=>?@[\\]^_`{|}~ "
        "!\"#$%&'()*+,-./0123456789:;<=>?@[\\]^_`{|}~"
    )
    doc = pymupdf.open()
    page = _page(doc)
    _paragraph(page, (72, 80, 523, 400), garbage)
    return _save(doc, "garbled_text.pdf", "")


def build_fullwidth_spacing() -> Path:
    """Full-width Latin letters and letter-spaced text (real 15104 pattern)."""
    doc = pymupdf.open()
    page = _page(doc)
    _paragraph(
        page,
        (72, 80, 523, 240),
        "本研究纳入 Ｃ ｉ ｒ ＣＡＧＦＧ１ 基因型患儿共 ４２ 例，其中 ２０２４年"
        "发病者 １８ 例。所有患儿均符合肺炎支原体肺炎诊断标准，Ｐ＜０．０５"
        " 为差异有统计学意义。基因型分布与临床表现的关系见表 １。",
    )
    return _save(doc, "fullwidth_spacing.pdf", "")


def build_partial_scan() -> Path:
    """Three pages where only the first has a text layer (scanned rest)."""
    doc = pymupdf.open()
    page = _page(doc)
    _paragraph(
        page,
        (72, 80, 523, 220),
        "第一页存在文本层，包含足够的汉字用于解析，其余页面是扫描图像，"
        "没有可抽取的文字。",
    )
    _page(doc)
    _page(doc)
    return _save(doc, "partial_scan.pdf", "")


def build_thesis_like() -> Path:
    """A 30-page document that must take the thesis route, not journal rules."""
    doc = pymupdf.open()
    page = _page(doc)
    _text(page, 72, 160, "硕士学位论文", 36)
    _text(page, 72, 260, "儿童难治性肺炎支原体肺炎临床研究", 24)
    _text(page, 72, 310, "测试大学", 20)

    page = _page(doc)
    _text(page, 72, 80, "目录", 16)
    _paragraph(page, (72, 100, 523, 260), "第一章 绪论 ………… 1 摘要 ………… 3")

    page = _page(doc)
    _text(page, 72, 80, "摘要", HEADING_FONT_SIZE)
    _paragraph(
        page,
        (72, 100, 523, 260),
        "本研究回顾性分析儿童难治性肺炎支原体肺炎的临床特征，纳入 120 例"
        "患儿，探讨其危险因素与治疗转归。",
    )

    for index in range(4, 31):
        page = _page(doc)
        _text(page, 72, 100, f"正文第 {index} 页。", BODY_FONT_SIZE)
    return _save(doc, "thesis_like.pdf", "学位论文测试夹具")


def main() -> None:
    built = [
        build_mini_two_pages(),
        build_long_section(),
        build_no_text(),
        build_not_a_pdf(),
        build_one_page_two_articles(),
        build_creationdate_not_year(),
        build_evidence_guideline(),
        build_evidence_systematic_review(),
        build_evidence_rct(),
        build_evidence_observational(),
        build_garbled_text(),
        build_fullwidth_spacing(),
        build_partial_scan(),
        build_thesis_like(),
    ]
    for path in built:
        print(f"wrote {path.relative_to(FIXTURES_DIR.parent.parent)} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
