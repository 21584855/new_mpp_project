"""PDF parsing and section segmentation (ticket 02, extended by ticket 23).

``parse_pdf`` is the public module API reused by later tickets; ``run`` backs
``python -m mppkb inspect <pdf> [--json]``.

The parser is deliberately deterministic: given the same file bytes and
embedded metadata it returns the same sections, hashes and JSON bytes. It uses
PyMuPDF for text extraction, removes repeated running headers and page-number
footers, detects headings from font size and common Chinese numbering patterns,
and packs paragraphs into chunks of at most 2000 estimated tokens without ever
reordering text.

Ticket 23 adds real-corpus handling on top of the ticket-02 extraction:

- running headers/footers are removed before anything else, and their text is
  kept aside because it is the most reliable journal/year source;
- the target article is cut out of a page run that may contain the tail of the
  previous paper and the title/abstract of the next one (report section 9.2);
- title, publication year, journal, DOI and authors are extracted from page
  layout and running headers, with per-field provenance; ``creationDate`` is
  never used as the publication year (report section 9.1);
- the evidence type is a rule score with a confidence, or ``None`` when the
  text is inconclusive (report section 9.4);
- a text-quality gate reports broken text layers, garbled text, full-width
  letter spacing, thesis-length documents and sparse text layers (section 9.5).

Documents of 30 pages or more take the thesis route (cover/catalogue/abstract)
and are never cut with the journal article-boundary rules.

Token estimate (fixed by the ticket): one token per CJK character plus one
token per ASCII word. Because every token covers at least one character, the
estimate of any string never exceeds its character count.

Document-level context (title, year, publisher, evidence type) is returned as
top-level metadata rather than being duplicated into every chunk; the
extraction ticket composes it into prompts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

MAX_SECTION_TOKENS = 2000
PAPER_ID_LEN = 16
HASH_LEN = 16
MIN_TEXT_CHARS = 10

HEADER_BAND_PT = 72.0
FOOTER_BAND_PT = 72.0
MAX_CHROME_CHARS = 100

TITLE_SIZE_RATIO = 1.5
HEADING_SIZE_RATIO = 1.15
MAX_HEADING_CHARS = 50

# ticket 23: real-corpus layout constants
ARTICLE_TITLE_RATIO = 1.3
MAX_TITLE_CHARS = 120
BOUNDARY_ABSTRACT_PT = 200.0
BOUNDARY_KEYWORDS_PT = 300.0
BOUNDARY_END_PT = 300.0
AUTHOR_GAP_PT = 120.0
THESIS_MIN_PAGES = 30
THESIS_TITLE_SEARCH_PAGES = 5

MIN_PAGE_TEXT_CHARS = 50
LOW_TEXT_CHARS = 200
SPARSE_CHARS_PER_PAGE = 1000

PREAMBLE_SECTION_PATH = "正文"

QUALITY_FLAG_ORDER = (
    "no_text_layer",
    "low_text_content",
    "garbled_text",
    "letter_spacing",
    "thesis_route",
    "sparse_text_layer",
)

CJK_RANGES = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"
_CJK_RE = re.compile(f"[{CJK_RANGES}]")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[._%+\-/][A-Za-z0-9]+)*")
_CJK_CHAR_RE = re.compile(f"[{CJK_RANGES}]")
_PAGE_NUMBER_RE = re.compile(
    r"^(?:"
    r"(?:第\s*)?\d{1,4}(?:\s*页)?"
    r"|[-—–]\s*\d{1,4}\s*[-—–]"
    r"|page\s+\d{1,4}(?:\s*(?:/|of)\s*\d{1,4})?"
    r")$",
    re.IGNORECASE,
)
_YEAR_CN_RE = re.compile(r"((?:19|20)\d{2})\s*年")
_YEAR_VOL_RE = re.compile(r"\bvol\.?\s*\d+[^\n]{0,40}?((?:19|20)\d{2})", re.IGNORECASE)
_YEAR_PAREN_RE = re.compile(r"[（(]\s*((?:19|20)\d{2})\s*[）)]")
_YEAR_PUNCT_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)\s*[,，;；:：.]")
_ARTICLE_NUMBER_RE = re.compile(
    r"文章编号\s*[:：]?\s*\d{4}\s*[-—]\s*\d{4}\s*[（(]\s*((?:19|20)\d{2})\s*[）)]"
)
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]*\d[-._;()/:A-Za-z0-9]*")
_DOI_VALID_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]*\d[-._;()/:A-Za-z0-9]*")
_DOI_TRAILING = ".,;:)]}。，；、）】"
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])\s*")

_FULLWIDTH_LATIN_RE = re.compile(r"[Ａ-Ｚａ-ｚ]")
# Damage pattern "C i r CAGFG1": single lowercase letters spaced apart.
# Uppercase initials ("Wang Y H") are normal and must not trigger the flag.
_SPACED_LETTERS_RE = re.compile(r"(?:[a-z]\s+){3,}[a-z]")
_ABSTRACT_LINE_RE = re.compile(r"^\s*[【［\[]?\s*(?:摘\s*要|abstract)", re.IGNORECASE)
_ABSTRACT_HEADING_RE = re.compile(r"摘\s*要|abstract", re.IGNORECASE)
_KEYWORDS_LINE_RE = re.compile(
    r"关键词|关键字|key\s*words|中图分类号", re.IGNORECASE
)
_INSTITUTION_RE = re.compile(
    r"医院|大学|学院|研究所|研究院|实验室|中心|科室|邮编|自治区|"
    r"school|university|hospital|department|institute|college",
    re.IGNORECASE,
)
_POSTCODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_AUTHOR_NOISE_RE = re.compile(r"[0-9*†‡#☆★①②③④⑤⑥⑦⑧⑨⑩]")
_NON_AUTHOR_LINE_RE = re.compile(
    r"扫码|视频简介|编者按|收稿日期|录用日期|基金项目|通信作者|通讯作者|"
    r"作者简介|中图分类号|文献标志码|文章编号|引用本文|声明",
)
_SENTENCE_END = "。；;！!？?"

# Names attached to running headers, watermarks and cover pages. Their lines
# must never be mistaken for the article title (report sections 6.1 and 9.1).
_EXCLUDED_TITLE_TOKENS = (
    "分类号",
    "中图分类",
    "密级",
    "udc",
    "issn",
    "cnki",
    "万方",
    "维普",
    "cqvip",
    "学位论文",
    "大学",
    "学院",
    "编辑部",
    "栏目",
    "收稿日期",
    "网络出版",
    "http",
    "www.",
    "doi",
    "文章编号",
    "版权",
    "copyright",
    "目录",
)

# Section words are never article-title candidates, even when the journal
# prints them larger than the body text (report section 9.3).
_SECTION_HEADING_WORDS = (
    "摘要",
    "关键词",
    "关键字",
    "前言",
    "引言",
    "绪论",
    "资料与方法",
    "对象与方法",
    "材料与方法",
    "研究方法",
    "方法",
    "结果",
    "讨论",
    "结论",
    "参考文献",
    "致谢",
    "附录",
    "abstract",
    "introduction",
    "background",
    "methods",
    "materials and methods",
    "results",
    "discussion",
    "conclusion",
    "references",
)

# Column/page labels that are printed large but are never the article title
# (report section 6.1/9.1). Matched exactly after stripping punctuation and
# whitespace, so a real title such as "某病的临床研究" is not affected.
_COLUMN_LABELS = (
    "临床研究",
    "基础研究",
    "论著",
    "专家论坛",
    "指南与共识",
    "诊疗规范",
    "诊疗建议",
    "综述",
    "述评",
    "病例报告",
    "短篇报道",
    "经验交流",
    "original article",
    "research article",
    "review article",
    "editorial",
    "case report",
)
_COLUMN_LABELS_COMPACT = frozenset(
    re.sub(r"[\s·・\[\]【】()（）:：]", "", label).lower() for label in _COLUMN_LABELS
)
_SECTION_WORDS_COMPACT = frozenset(
    word.replace(" ", "") for word in _SECTION_HEADING_WORDS
)

# ordered from most specific to least specific
_HEADING_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"^第[一二三四五六七八九十百零〇\d]+[章篇部]"), 1),
    (re.compile(r"^第[一二三四五六七八九十百零〇\d]+节"), 2),
    (re.compile(r"^[一二三四五六七八九十百零〇]+[、.．]"), 1),
    (re.compile(r"^（[一二三四五六七八九十百零〇]+）"), 2),
    (re.compile(r"^\d+\.\d+\.\d+"), 3),
    (re.compile(r"^\d+\.\d+"), 2),
    (re.compile(r"^\d+[、.．]\s*"), 1),
    (re.compile(r"^【[^】]+】"), 2),
)

# Contract evidence types (docs/contracts.md section 11). The scoring rules and
# their weights follow the probe report section 9.4; anything below the score
# threshold is reported as None (unclear) rather than guessed.
_EVIDENCE_ORDER = (
    "guideline",
    "systematic_review",
    "randomized_controlled_trial",
    "observational_study",
    "case_report",
    "narrative_review",
)
_EVIDENCE_MIN_SCORE = 3


class ParseError(Exception):
    """Raised when a PDF cannot be turned into sections."""


@dataclass(frozen=True)
class _Line:
    page: int
    page_height: float
    block: int
    text: str
    size: float
    x0: float
    y0: float
    y1: float


@dataclass(frozen=True)
class _Paragraph:
    page_start: int
    page_end: int
    text: str


@dataclass(frozen=True)
class _Candidate:
    """A run of adjacent large-font lines that may be an article title."""

    start: int
    stop: int
    lines: tuple[_Line, ...]
    size: float

    @property
    def page(self) -> int:
        return self.lines[0].page

    @property
    def page_height(self) -> float:
        return self.lines[0].page_height

    @property
    def y0(self) -> float:
        return self.lines[0].y0

    @property
    def y1(self) -> float:
        return self.lines[-1].y1

    @property
    def text(self) -> str:
        return _join_lines(list(self.lines))


@dataclass(frozen=True)
class _TitlePick:
    text: str
    candidate: _Candidate | None


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Collapse whitespace so repeated lines compare equal across pages."""
    return re.sub(r"\s+", " ", text).strip()


def _is_cjk_char(char: str) -> bool:
    return bool(char) and bool(_CJK_CHAR_RE.match(char))


def _starts_cjk(text: str) -> bool:
    return bool(text) and _is_cjk_char(text[0])


def _ends_cjk(text: str) -> bool:
    return bool(text) and _is_cjk_char(text[-1])


def _needs_space(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left.endswith("-"):
        return False
    if _ends_cjk(left) or _starts_cjk(right):
        return False
    return not left[-1].isspace() and not right[0].isspace()


def _join_lines(lines: list[_Line]) -> str:
    """Join wrapped lines without inserting spaces inside Chinese text."""
    text = ""
    for line in lines:
        part = line.text.strip()
        if not part:
            continue
        if not text:
            text = part
        elif _needs_space(text, part):
            text += " " + part
        else:
            text += part
    return text


def _join_fragments(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    return left + (" " if _needs_space(left, right) else "") + right


def estimate_tokens(text: str) -> int:
    """Return the ticket's token estimate: CJK chars + ASCII words."""
    return len(_CJK_RE.findall(text)) + len(_ASCII_WORD_RE.findall(text))


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------


def _extract_lines(doc: pymupdf.Document) -> list[_Line]:
    lines: list[_Line] = []
    for page_index, page in enumerate(doc):
        height = float(page.rect.height)
        blocks: list[list[_Line]] = []
        page_dict = page.get_text("dict")
        for block in page_dict.get("blocks", []):
            if block.get("type", 0) != 0:
                continue
            block_lines: list[_Line] = []
            for line in block.get("lines", []):
                spans = [span for span in line.get("spans", []) if span.get("text")]
                if not spans:
                    continue
                text = _normalise("".join(span["text"] for span in spans))
                if not text:
                    continue
                bbox = line.get("bbox") or (0.0, 0.0, 0.0, 0.0)
                block_lines.append(
                    _Line(
                        page=page_index + 1,
                        page_height=height,
                        block=len(blocks),
                        text=text,
                        size=round(max(float(span.get("size", 0.0)) for span in spans), 1),
                        x0=float(bbox[0]),
                        y0=float(bbox[1]),
                        y1=float(bbox[3]),
                    )
                )
            if block_lines:
                blocks.append(block_lines)
        # Reading order: top-to-bottom, then left-to-right between blocks.
        blocks.sort(key=lambda block_lines: (min(line.y0 for line in block_lines), min(line.x0 for line in block_lines)))
        for block_lines in blocks:
            lines.extend(block_lines)
    return lines


def _in_chrome_band(line: _Line) -> bool:
    return line.y1 <= HEADER_BAND_PT or line.y0 >= line.page_height - FOOTER_BAND_PT


def _drop_running_chrome(lines: list[_Line], pages: int) -> tuple[list[_Line], list[_Line]]:
    """Drop page numbers and running headers/footers.

    Ticket 02 only removed short lines repeated inside the 72 pt header/footer
    bands. Ticket 23 also removes normalised lines that repeat on
    ``max(3, 50%)`` of the pages anywhere on the page (report section 9.2,
    item 5), and returns the dropped lines so metadata extraction can still
    read the journal name and year from the running header.
    """
    page_sets: dict[str, set[int]] = {}
    for line in lines:
        if len(line.text) <= MAX_CHROME_CHARS:
            page_sets.setdefault(line.text, set()).add(line.page)
    repeat_threshold = max(3, math.ceil(pages * 0.5))
    repeated_everywhere = {
        text for text, seen in page_sets.items() if len(seen) >= repeat_threshold
    }
    band_counts: Counter[str] = Counter(
        line.text
        for line in lines
        if _in_chrome_band(line) and len(line.text) <= MAX_CHROME_CHARS
    )
    kept: list[_Line] = []
    chrome: list[_Line] = []
    for line in lines:
        dropped = False
        if len(line.text) <= MAX_CHROME_CHARS and line.text in repeated_everywhere:
            dropped = True
        elif _in_chrome_band(line):
            if band_counts[line.text] >= 2 or _PAGE_NUMBER_RE.match(line.text):
                dropped = True
        if dropped:
            chrome.append(line)
        else:
            kept.append(line)
    return kept, chrome


def _body_font_size(lines: list[_Line]) -> float:
    weights: Counter[float] = Counter()
    for line in lines:
        weights[line.size] += len(line.text)
    if not weights:
        raise ParseError("no extractable text layer")
    # Most characters win; larger font breaks ties.
    return max(weights.items(), key=lambda item: (item[1], item[0]))[0]


def _pattern_heading_level(text: str) -> int | None:
    for pattern, level in _HEADING_PATTERNS:
        if pattern.match(text):
            return level
    return None


def _heading_level(line: _Line, body_size: float) -> int | None:
    text = line.text
    if not text or len(text) > MAX_HEADING_CHARS:
        return None
    if text[-1] in "。；;，,！!？?":
        return None
    by_size = line.size >= body_size * HEADING_SIZE_RATIO
    pattern_level = _pattern_heading_level(text)
    if pattern_level is not None and (by_size or line.size >= body_size * 0.99):
        return pattern_level
    if by_size:
        # Unnumbered headings are detected by font size alone; treat as top level.
        return 1
    return None


def _safe_metadata(doc: pymupdf.Document) -> dict[str, str]:
    try:
        raw = doc.metadata or {}
    except Exception:  # noqa: BLE001 - malformed PDF metadata must not abort parsing
        return {}
    return {str(key): str(value) for key, value in raw.items() if value}


def _filename_title(path: Path) -> str | None:
    stem = path.stem.replace("_", " ").replace("-", " ").strip()
    return stem or None


def _parse_metadata_pairs(raw: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for part in re.split(r"[;,\n]", raw or ""):
        if "=" not in part and ":" not in part and "：" not in part:
            continue
        key, value = re.split(r"[:=：]", part, maxsplit=1)
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            pairs[key] = value
    return pairs


# ---------------------------------------------------------------------------
# quality gate (report section 9.5)
# ---------------------------------------------------------------------------


def _page_char_counts(lines: list[_Line], pages: int) -> list[int]:
    counts = [0] * pages
    for line in lines:
        index = line.page - 1
        if 0 <= index < pages:
            counts[index] += len(line.text)
    return counts


def _looks_garbled(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 20:
        return False
    total = len(compact)
    cjk = len(_CJK_RE.findall(compact))
    latin = len(re.findall(r"[A-Za-z]", compact))
    alnum = sum(1 for char in compact if char.isalnum())
    symbol_ratio = (total - alnum) / total
    cjk_ratio = cjk / total
    if symbol_ratio > 0.30 and cjk_ratio < 0.30:
        return True
    # Latin-only text is normal for an English article; a broken CID text
    # layer usually has almost no letters either.
    if cjk_ratio < 0.05 and latin / total < 0.30:
        return True
    # Digit/punctuation mapping damage (real 21614): a broken custom encoding
    # replaces numbers and punctuation with a dense stream of ! $ % & # ".
    unusual = sum(compact.count(char) for char in "!$%&#\"")
    if cjk_ratio > 0.05 and unusual / total > 0.02:
        return True
    return False


def _has_letter_spacing(text: str) -> bool:
    if len(_FULLWIDTH_LATIN_RE.findall(text)) >= 4:
        return True
    return bool(_SPACED_LETTERS_RE.search(text))


def _quality_flags(lines: list[_Line], pages: int) -> list[str]:
    flags: set[str] = set()
    counts = _page_char_counts(lines, pages)
    total = sum(counts)
    if pages >= 2:
        empty_pages = sum(1 for count in counts if count == 0)
        if empty_pages / pages > 0.30:
            flags.add("no_text_layer")
    if total < LOW_TEXT_CHARS:
        flags.add("low_text_content")
    text = "\n".join(line.text for line in lines)
    if _looks_garbled(text):
        flags.add("garbled_text")
    if _has_letter_spacing(text):
        flags.add("letter_spacing")
    if pages >= THESIS_MIN_PAGES:
        flags.add("thesis_route")
        if total / pages < SPARSE_CHARS_PER_PAGE:
            flags.add("sparse_text_layer")
    return [flag for flag in QUALITY_FLAG_ORDER if flag in flags]


# ---------------------------------------------------------------------------
# article boundaries (report section 9.2)
# ---------------------------------------------------------------------------


def _is_excluded_title_text(text: str) -> bool:
    if not text or len(text) < 4:
        return True
    if _PAGE_NUMBER_RE.match(text):
        return True
    if re.search(r"[\ue000-\uf8ff\ufffd]", text):
        # Private-use / replacement characters: broken OCR text layer.
        return True
    lowered = text.lower()
    if any(token in lowered for token in _EXCLUDED_TITLE_TOKENS):
        return True
    if not re.search(f"[A-Za-z{CJK_RANGES}]", text):
        return True
    return False


def _compact_label(text: str) -> str:
    return re.sub(r"[\s·・\[\]【】()（）:：]", "", text).lower()


def _is_column_label(text: str) -> bool:
    return _compact_label(text) in _COLUMN_LABELS_COMPACT


def _looks_like_section_heading(text: str) -> bool:
    stripped = re.sub(
        r"^\s*(?:[一二三四五六七八九十百零〇]+[、.．]"
        r"|[（(][一二三四五六七八九十百零〇]+[）)]"
        r"|\d+(?:\.\d+)*[、.．]?)\s*",
        "",
        text,
    ).strip()
    if not stripped:
        return False
    compact = re.sub(r"\s+", "", stripped).lower().rstrip("：:")
    if len(compact) > 20:
        return False
    if compact in _SECTION_WORDS_COMPACT:
        return True
    if stripped.lower().rstrip("：: ") in _SECTION_HEADING_WORDS:
        return True
    return any(
        compact.startswith(word)
        for word in ("摘要", "关键词", "abstract", "references", "参考文献")
    )


def _is_title_candidate_line(line: _Line, body_size: float) -> bool:
    if line.size < body_size * ARTICLE_TITLE_RATIO:
        return False
    if len(line.text) > MAX_TITLE_CHARS:
        return False
    if line.text[-1] in "。；，,;":
        return False
    if _is_column_label(line.text):
        return False
    if _is_excluded_title_text(line.text):
        return False
    if _looks_like_section_heading(line.text):
        return False
    if _pattern_heading_level(line.text) is not None:
        return False
    # A single marginally-larger line that looks like an author or affiliation
    # is not a heading. Real titles at this ratio span several lines; a merged
    # group is handled by the merge step (the first line is evaluated here).
    ratio = line.size / body_size if body_size else 0.0
    if ratio < TITLE_SIZE_RATIO and (
        _is_author_line(line)
        or _INSTITUTION_RE.search(line.text)
        or _POSTCODE_RE.search(line.text)
        or _DOI_RE.search(line.text)
    ):
        return False
    return True


def _title_candidates(lines: list[_Line], body_size: float) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not _is_title_candidate_line(line, body_size):
            index += 1
            continue
        group = [line]
        cursor = index + 1
        while cursor < len(lines):
            nxt = lines[cursor]
            if not _is_title_candidate_line(nxt, body_size):
                break
            if nxt.page != group[-1].page:
                break
            if abs(nxt.size - group[-1].size) > 0.6:
                break
            gap = nxt.y0 - group[-1].y1
            if gap < -nxt.size * 0.6 or gap > nxt.size * 1.6:
                break
            group.append(nxt)
            cursor += 1
        candidates.append(
            _Candidate(
                start=index,
                stop=cursor,
                lines=tuple(group),
                size=group[0].size,
            )
        )
        index = cursor
    return candidates


def _marker_within(
    candidate: _Candidate,
    lines: list[_Line],
    pattern: re.Pattern[str],
    limit_pt: float,
) -> bool:
    last = candidate.lines[-1]
    for line in lines[candidate.stop :]:
        if line.page != last.page:
            break
        gap = line.y0 - last.y1
        if gap < 0:
            continue
        if gap > limit_pt:
            break
        if pattern.search(line.text):
            return True
    return False


def _is_author_line(line: _Line) -> bool:
    text = line.text
    if not text or len(text) > 80:
        return False
    if _INSTITUTION_RE.search(text) or _POSTCODE_RE.search(text):
        return False
    if _ABSTRACT_LINE_RE.match(text) or _KEYWORDS_LINE_RE.search(text):
        return False
    if _looks_like_section_heading(text) or _pattern_heading_level(text) is not None:
        return False
    if text.endswith(("。", "；", ";", "，", ",")):
        return False
    if re.search(r"(?:19|20)\d{2}", text) or _DOI_RE.search(text):
        return False
    return bool(re.search(f"[{CJK_RANGES}]{{2,}}", text) or re.search(r"[A-Za-z]{2,}", text))


def _abstract_below(candidate: _Candidate, lines: list[_Line], limit_pt: float) -> bool:
    """True when an abstract heading starts within ``limit_pt`` below."""
    last = candidate.lines[-1]
    for line in lines[candidate.stop :]:
        if line.page != last.page:
            break
        gap = line.y0 - last.y1
        if gap < 0:
            continue
        if gap > limit_pt:
            break
        text = line.text
        if _ABSTRACT_LINE_RE.match(text):
            return True
        if len(text) <= 30 and _ABSTRACT_HEADING_RE.search(text):
            return True
        if text.strip() in {"摘", "要"}:
            # Split label "摘" / "要：目的…" (real 34774).
            return True
    return False


def _is_noise_fragment(line: _Line) -> bool:
    """Single superscript digits / stray glyphs between author fragments."""
    text = line.text.strip()
    if len(text) > 3:
        return False
    if re.search(f"[{CJK_RANGES}]", text):
        return False  # a single CJK glyph is part of a split author name
    if re.search(r"[A-Za-z]", text):
        return False
    return not any(char in "，,、;；/" for char in text)


def _is_short_cjk_fragment(line: _Line) -> bool:
    text = _AUTHOR_NOISE_RE.sub("", line.text).strip()
    return len(text) == 1 and _is_cjk_char(text)


def _authors_below(candidate: _Candidate, lines: list[_Line]) -> bool:
    last = candidate.lines[-1]
    seen_author = False
    for line in lines[candidate.stop :]:
        if line.page != last.page:
            break
        gap = line.y0 - last.y1
        if gap < 0:
            continue
        if gap > AUTHOR_GAP_PT:
            break
        if _INSTITUTION_RE.search(line.text) or _POSTCODE_RE.search(line.text):
            return seen_author
        if _is_author_line(line):
            seen_author = True
        elif seen_author:
            if _is_noise_fragment(line):
                continue
            return False
    return False


def _candidate_starts_article(candidate: _Candidate, lines: list[_Line]) -> bool:
    if _abstract_below(candidate, lines, BOUNDARY_ABSTRACT_PT):
        return True
    if _marker_within(candidate, lines, _KEYWORDS_LINE_RE, BOUNDARY_KEYWORDS_PT):
        return True
    return _authors_below(candidate, lines)


def _detect_article_bounds(
    lines: list[_Line], body_size: float, pages: int
) -> tuple[int, int]:
    """Return the ``[start, stop)`` line slice that belongs to the target article.

    Theses (>=30 pages) take the cover/catalogue/abstract route instead of the
    journal header route (report section 9.2, item 6). An article that starts on
    page 1 is never started from a later page: a later large-font block is the
    English title/abstract of the same paper or the next article, not the start.
    """
    if pages >= THESIS_MIN_PAGES:
        return 0, len(lines)
    candidates = _title_candidates(lines, body_size)
    page1_candidates = [candidate for candidate in candidates if candidate.page == 1]
    start: _Candidate | None = None
    if page1_candidates:
        for candidate in page1_candidates:
            if _candidate_starts_article(candidate, lines):
                start = candidate
                break
        if start is None:
            # Report 9.5: a title candidate below 24% of page 1, or a page
            # that opens with references, means the page run contains a
            # previous article and must be cut even without an anchor.
            first = page1_candidates[0]
            opens_with_references = any(
                re.match(r"^\s*[［\[]\s*\d+\s*[］\]]", line.text)
                or "参考文献" in line.text
                or "收稿日期" in line.text
                for line in lines[: first.start]
            )
            if first.y0 > first.page_height * 0.24 or opens_with_references:
                start = first
    else:
        # No title-sized line on page 1 at all (scanned cover page, etc.).
        for candidate in candidates:
            if _candidate_starts_article(candidate, lines):
                start = candidate
                break
    if start is None:
        return 0, len(lines)
    start_is_cjk = len(_CJK_RE.findall(_join_lines(list(start.lines)))) >= 2
    stop = len(lines)
    for candidate in candidates:
        if candidate.start <= start.start:
            continue
        # The English title + Abstract block of a Chinese article is the same
        # article, not the next one.
        if start_is_cjk and len(_CJK_RE.findall(candidate.text)) == 0:
            continue
        if _abstract_below(candidate, lines, BOUNDARY_END_PT):
            stop = candidate.start
            break
    if stop <= start.start:
        stop = len(lines)
    return start.start, stop


# ---------------------------------------------------------------------------
# first-page / running-header metadata (report section 9.1)
# ---------------------------------------------------------------------------


def _score_title_candidate(
    candidate: _Candidate, lines: list[_Line], body_size: float
) -> float:
    if body_size <= 0:
        return 0.0
    score = 2.0 * (candidate.size / body_size)
    score += 2.0 * max(0.0, 1.0 - candidate.y0 / candidate.page_height)
    if _abstract_below(candidate, lines, BOUNDARY_ABSTRACT_PT):
        score += 3.0
    elif _marker_within(candidate, lines, _KEYWORDS_LINE_RE, BOUNDARY_KEYWORDS_PT):
        score += 3.0
    if _authors_below(candidate, lines):
        score += 1.5
    if not candidate.text.endswith(_SENTENCE_END):
        score += 0.5
    return score


def _layout_title(
    lines: list[_Line], body_size: float, pages: int
) -> _TitlePick | None:
    candidates = _title_candidates(lines, body_size)
    if not candidates:
        return None
    if pages >= THESIS_MIN_PAGES:
        # Do not take the largest cover text: "硕士学位论文" or the university
        # name often is. Search around the abstract page instead.
        abstract_page: int | None = None
        for line in lines:
            if _ABSTRACT_LINE_RE.match(line.text):
                abstract_page = line.page
                break
        boundary = abstract_page or THESIS_TITLE_SEARCH_PAGES
        pool = [candidate for candidate in candidates if candidate.page <= boundary]
    else:
        first_page = lines[0].page if lines else 1
        pool = [candidate for candidate in candidates if candidate.page == first_page]
    if not pool:
        return None
    # In a Chinese article the English title block is a translation, not the
    # title. Only apply the script filter when the title page's body text is
    # CJK-dominant (bilingual first pages still count as Chinese).
    first_page = lines[0].page if lines else 1
    page1_body = "".join(
        line.text
        for line in lines
        if line.page == first_page and line.size < body_size * ARTICLE_TITLE_RATIO
    )
    compact_body = re.sub(r"\s+", "", page1_body)
    if compact_body and len(_CJK_RE.findall(compact_body)) / len(compact_body) > 0.12:
        cjk_pool = [candidate for candidate in pool if _CJK_RE.search(candidate.text)]
        if cjk_pool:
            pool = cjk_pool
    best = max(
        pool,
        key=lambda candidate: (
            _score_title_candidate(candidate, lines, body_size),
            -candidate.start,
        ),
    )
    text = _normalise(best.text)
    if len(text) < 2:
        return None
    return _TitlePick(text=text, candidate=best)


def _parse_author_names(text: str) -> list[str]:
    cleaned = _AUTHOR_NOISE_RE.sub("", text)
    tokens: list[str] = []
    for part in re.split(r"[，,、;；/]+", cleaned):
        for token in part.split():
            token = token.strip(" .·*")
            if not token or len(token) > 20:
                continue
            if re.search(f"[{CJK_RANGES}]|[A-Za-z]", token):
                tokens.append(token)
    # Pair up single-character CJK fragments split by spaces inside a name
    # ("王  立" -> 王立). Three-character names stay as one token.
    names: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            len(token) == 1
            and _is_cjk_char(token)
            and index + 1 < len(tokens)
            and len(tokens[index + 1]) == 1
            and _is_cjk_char(tokens[index + 1])
        ):
            names.append(token + tokens[index + 1])
            index += 2
        else:
            names.append(token)
            index += 1
    return names[:20]


def _extract_authors(lines: list[_Line], pick: _TitlePick | None) -> list[str]:
    if pick is None or pick.candidate is None:
        return []
    candidate = pick.candidate
    collected: list[_Line] = []
    institution_text = ""
    last = candidate.lines[-1]
    for line in lines[candidate.stop :]:
        if line.page != last.page:
            break
        gap = line.y0 - last.y1
        if gap < 0:
            continue
        if gap > AUTHOR_GAP_PT:
            break
        if _ABSTRACT_LINE_RE.search(line.text) or _KEYWORDS_LINE_RE.search(line.text):
            break
        if _NON_AUTHOR_LINE_RE.search(line.text):
            # Promo/editorial lines before the author block are skipped; after
            # the authors they end it.
            if collected:
                break
            continue
        if _INSTITUTION_RE.search(line.text) or _POSTCODE_RE.search(line.text):
            # Authors and affiliations are printed on the same visual line in
            # CNKI layouts; remember the affiliation text to filter city names
            # ("海南", "海口") out of the author list later.
            institution_text += line.text
            continue
        if _is_noise_fragment(line):
            continue
        if _is_short_cjk_fragment(line):
            collected.append(line)
            continue
        if not _is_author_line(line):
            break
        collected.append(line)
    if not collected:
        return []
    # Keep each source line as a separator: superscript-split author fragments
    # must stay separate names, not be glued into one.
    names = _parse_author_names("\n".join(line.text for line in collected))
    if institution_text:
        names = [name for name in names if name not in institution_text]
    return names


def _doi_ready(text: str) -> str:
    """NFKC + whitespace removal so full-width / line-wrapped DOIs still match."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _extract_doi(
    article_lines: list[_Line],
    header_lines: list[_Line],
    pre_title_lines: list[_Line],
) -> tuple[str | None, str | None]:
    # A DOI printed just above the title belongs to this article (CNKI prints
    # DOI/文章编号 before the title); the running header is the next best
    # source, the article text after the title the last.
    nearest_first = list(reversed(pre_title_lines)) + article_lines
    for source_name, source_lines in (
        ("header", header_lines),
        ("page_text", nearest_first),
    ):
        # A DOI printed on one line is the common case.
        for line in source_lines:
            match = _DOI_RE.search(_doi_ready(line.text))
            if match:
                doi = match.group(0).rstrip(_DOI_TRAILING)
                if _DOI_VALID_RE.fullmatch(doi):
                    return doi, source_name
        # Line-wrapped DOIs (the report found them broken across lines): join
        # without a separator and retry once.
        joined = _doi_ready("".join(line.text for line in source_lines))
        match = _DOI_RE.search(joined)
        if match:
            doi = match.group(0).rstrip(_DOI_TRAILING)
            if _DOI_VALID_RE.fullmatch(doi):
                return doi, source_name
    return None, None


def _first_year_match(text: str) -> int | None:
    for pattern in (_YEAR_CN_RE, _YEAR_VOL_RE, _YEAR_PAREN_RE, _YEAR_PUNCT_RE):
        match = pattern.search(text)
        if match:
            year = int(match.group(1))
            if 1900 <= year <= 2100:
                return year
    return None


def _extract_year(
    article_lines: list[_Line],
    header_lines: list[_Line],
    doi: str | None,
) -> tuple[int | None, str | None]:
    """Publication year from layout/DOI/article number; never creationDate."""
    for source_name, source_lines in (
        ("header", header_lines),
        ("page_text", article_lines),
    ):
        for line in source_lines:
            year = _first_year_match(line.text)
            if year is not None:
                return year, source_name
    page_text = "".join(line.text for line in article_lines)
    match = _ARTICLE_NUMBER_RE.search(page_text)
    if match:
        year = int(match.group(1))
        if 1900 <= year <= 2100:
            return year, "article_number"
    if doi:
        match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", doi)
        if match:
            year = int(match.group(1))
            if 1900 <= year <= 2100:
                return year, "doi"
    return None, None


def _looks_like_journal_line(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 2:
        return False
    if len(stripped) > 60:
        return False
    lowered = stripped.lower()
    if _PAGE_NUMBER_RE.match(stripped):
        return False
    if re.match(r"^(?:vol|no|number)[.．]?\s*\d", lowered):
        return False
    if lowered.strip(" .·:：") in {
        "and",
        "the",
        "for",
        "with",
        "from",
        "this",
        "that",
        "article",
        "journal",
    }:
        return False
    if any(token in lowered for token in ("万方", "cnki", "维普", "cqvip", "copyright")):
        return False
    if "http" in lowered or "www." in lowered:
        return False
    if "doi" in lowered or _DOI_RE.search(stripped):
        return False
    if re.search(r"(?:\s等|et\s*al\.?)$", stripped, re.IGNORECASE):
        # Running author head, e.g. "王艳红 等" / "T.-Y. Liu et al".
        return False
    if re.match(
        r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}",
        lowered,
    ):
        return False
    if re.match(r"^\d", stripped):
        return False
    if not re.match(f"[A-Za-z{CJK_RANGES}]", stripped):
        return False
    if re.search(r"(?:19|20)\d{2}\s*年", stripped):
        return False
    if "卷" in stripped and ("期" in stripped or "第" in stripped):
        return False
    if re.search(r"第\s*\d+\s*[卷期]", stripped):
        return False
    if not re.search(f"[{CJK_RANGES}]|[A-Za-z]", stripped):
        return False
    return True


def _trim_journal_name(name: str) -> str:
    name = re.sub(r"\s*[（(](?:19|20)\d{2}[）)].*$", "", name)
    name = re.sub(r"[\s,;]+(?:19|20)\d{2}\b.*$", "", name)
    return name.strip(" ,.;:&-")


def _journal_name_from_line(text: str) -> str | None:
    """Recover the journal from a running header.

    Handles plain names (``新医学``), Chinese combined headers
    (``浙江临床医学2018年11月第20卷第11期``) and Latin headers
    (``Scientific Reports | 6:39929 | DOI: ...``,
    ``Pediatric Pulmonology 49:377-380 (2014)``).
    """
    lowered = text.lower()
    if "|" in text:
        head = text.split("|", 1)[0].strip()
        if head and not any(
            token in head.lower()
            for token in ("万方", "cnki", "维普", "cqvip", "doi", "http", "www.")
        ):
            name = _trim_journal_name(head)
            if len(name) >= 2 and re.search(f"[A-Za-z{CJK_RANGES}]", name):
                return name
    if any(
        token in lowered
        for token in ("万方", "cnki", "维普", "cqvip", "copyright", "doi", "http", "www.")
    ):
        return None
    if re.match(
        r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}\s*$",
        lowered,
    ):
        return None
    # A volume/issue suffix is not part of the journal name.
    match = re.match(r"^(.+?)[,;]?\s+(?:vol|no)[.．]?\s*\d", text, re.IGNORECASE)
    if match:
        name = _trim_journal_name(match.group(1))
        if len(name) >= 2 and not re.search(r"\d", name):
            return name
    match = re.match(r"^(.{2,40}?)(?=(?:19|20)\d{2}\s*年)", text)
    if match:
        name = _trim_journal_name(match.group(1))
        if name and not re.search(r"\d", name) and re.search(f"[{CJK_RANGES}]|[A-Za-z]", name):
            return name
    if _looks_like_journal_line(text):
        name = _trim_journal_name(text)
        if len(name) >= 2:
            return name
    # Latin running head that carries a year or volume/page information.
    match = re.match(r"^([A-Za-z][A-Za-z&.,'\- ]{3,80}?)\s+(?:19|20)\d{2}\b", text)
    if match:
        name = _trim_journal_name(match.group(1))
        if len(name) >= 2:
            return name
    match = re.match(r"^([A-Za-z][A-Za-z&.,'\- ]{3,80}?)\s+\d+\s*[:：]", text)
    if match:
        name = _trim_journal_name(match.group(1))
        if len(name) >= 2:
            return name
    return None


def _extract_journal(
    header_lines: list[_Line],
    article_lines: list[_Line],
    pairs: dict[str, str],
    pages: int,
    body_size: float,
) -> tuple[str | None, str | None]:
    if pairs.get("publisher"):
        return pairs["publisher"], "metadata_keywords"
    if pages >= THESIS_MIN_PAGES:
        # Report 9.5: theses do not use the journal running-header rules.
        return None, None
    counts: Counter[str] = Counter()
    order: dict[str, int] = {}
    for index, line in enumerate(header_lines):
        if len(line.text) > MAX_CHROME_CHARS:
            continue
        counts[line.text] += 1
        order.setdefault(line.text, index)
    threshold = max(2, math.ceil(pages * 0.5))
    repeated = [text for text, count in counts.items() if count >= threshold]
    repeated.sort(key=lambda text: (-counts[text], order[text]))
    for text in repeated:
        name = _journal_name_from_line(text)
        if name:
            return name, "header"
    # No cross-page repetition (often a one-page paper): fall back to the very
    # first line of the page, but only when it physically sits in the top
    # chrome band. A body paragraph must never become the journal name.
    for line in header_lines:
        if line.page != 1 or line.y0 > line.page_height * 0.08:
            continue
        if line.size >= body_size * ARTICLE_TITLE_RATIO:
            continue
        name = _journal_name_from_line(line.text)
        if name:
            return name, "header"
    if article_lines:
        first = article_lines[0]
        if first.y0 <= first.page_height * 0.08 and first.size < body_size * ARTICLE_TITLE_RATIO:
            name = _journal_name_from_line(first.text)
            if name:
                return name, "first_line"
    return None, None


def _evidence_confidence(score: int) -> float:
    return round(min(0.95, 0.35 + 0.1 * score), 2)


_EVIDENCE_ALIASES = {
    "cohort": "observational_study",
    "observational": "observational_study",
    "review": "narrative_review",
    "rct": "randomized_controlled_trial",
    "meta": "systematic_review",
    "meta_analysis": "systematic_review",
}


def _clean_evidence_type(value: str | None) -> str | None:
    """Keep ``evidence_type`` inside the contract enum (docs/contracts.md 11)."""
    if not value:
        return None
    key = value.strip().lower().replace(" ", "_").replace("-", "_")
    key = _EVIDENCE_ALIASES.get(key, key)
    return key if key in _EVIDENCE_ORDER else None


def _evidence_text(lines: list[_Line]) -> str:
    """Title/abstract/methods excerpt: stop at references and cap the length.

    The probe report (section 9.4) warns that "指南"/"Meta" appear in reference
    lists; evidence scoring must not read the bibliography.
    """
    parts: list[str] = []
    total = 0
    for line in lines:
        compact = re.sub(r"\s+", "", line.text).lower()
        if compact in {"参考文献", "references"} or (
            len(compact) <= 8 and compact.startswith(("参考文献", "references"))
        ):
            break
        parts.append(line.text)
        total += len(line.text)
        if total >= 8000:
            break
    return "\n".join(parts)[:8000]


def _infer_evidence_type(
    title: str | None, lines: list[_Line], pages: int
) -> tuple[str | None, float | None]:
    """Rule scoring per report section 9.4; low evidence yields ``None``."""
    title_text = (title or "").lower()
    text = _evidence_text(lines)
    lowered = text.lower()
    scores: dict[str, int] = {name: 0 for name in _EVIDENCE_ORDER}

    def add(name: str, points: int) -> None:
        scores[name] += points

    # guideline / consensus / local protocol
    if re.search(r"指南|共识|诊疗建议|诊疗规范|推荐意见", title_text):
        add("guideline", 3)
    for signal in ("推荐意见", "推荐强度", "证据等级"):
        if signal in lowered:
            add("guideline", 2)
    if re.search(r"\bgrade\b", lowered):
        add("guideline", 2)
    if "定义与临床分型" in text:
        add("guideline", 1)

    # systematic review / meta-analysis
    if re.search(r"meta\s*分析|meta-analysis|荟萃分析|系统评价|systematic review", title_text):
        add("systematic_review", 4)
    if re.search(r"检索策略|计算机检索|检索数据库|literature search", lowered):
        add("systematic_review", 2)
    if re.search(r"纳入标准|排除标准|纳入与排除", lowered):
        add("systematic_review", 2)
    if re.search(r"森林图|异质性|prisma|revman|stata|funnel plot", lowered):
        add("systematic_review", 2)

    # randomized controlled trial (and its weak/quasi variants)
    randomized = re.search(
        r"随机数字表|随机分组|randomly divided|random number table|"
        r"randomly assigned|randomly allocated|randomi[sz]ed",
        lowered,
    )
    prospective = re.search(r"前瞻性|prospective", lowered)
    claimed = re.search(r"随机分为|随机分成|randomly divided", lowered)
    quasi = re.search(r"按住院|按就诊|按入院|住院先后|就诊先后", lowered)
    if re.search(r"随机对照|randomized controlled|randomised controlled", title_text):
        add("randomized_controlled_trial", 1)
    if randomized:
        add("randomized_controlled_trial", 3)
    elif claimed and not quasi:
        add("randomized_controlled_trial", 2)
    if prospective:
        add("randomized_controlled_trial", 1)
    if quasi:
        # "按住院先后随机分为" is not true randomization: downgrade it.
        scores["randomized_controlled_trial"] = min(
            scores["randomized_controlled_trial"], 2
        )

    # observational studies
    if re.search(r"回顾性|retrospective|队列|cohort|病例对照|case-control|横断面|cross-sectional", lowered):
        add("observational_study", 3)
    if re.search(r"收集.{0,20}临床资料", lowered):
        add("observational_study", 1)
    if re.search(r"根据.{0,20}分为", lowered) and not randomized:
        add("observational_study", 1)

    # case report / narrative review
    if re.search(r"例报告|一例|个案报告|case report", title_text):
        add("case_report", 3)
    if pages <= 2:
        add("case_report", 1)
    if re.search(r"综述|研究进展|文献综述", title_text) and not re.search(
        r"meta|系统评价|systematic review", title_text
    ):
        add("narrative_review", 2)
    if "病例摘要" in text:
        add("case_report", 1)

    ranked = sorted(
        scores.items(),
        key=lambda item: (-item[1], _EVIDENCE_ORDER.index(item[0])),
    )
    best_type, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0
    if best_score < _EVIDENCE_MIN_SCORE:
        return None, None
    if second_score >= best_score:
        return None, None
    return best_type, _evidence_confidence(best_score)


def _document_metadata(
    doc: pymupdf.Document,
    path: Path,
    article_lines: list[_Line],
    header_lines: list[_Line],
    pre_title_lines: list[_Line],
    body_size: float,
    pages: int,
) -> dict[str, Any]:
    raw = _safe_metadata(doc)
    pairs = _parse_metadata_pairs(raw.get("keywords", ""))
    pairs.update(_parse_metadata_pairs(raw.get("subject", "")))
    sources: dict[str, str] = {}

    pick = _layout_title(article_lines, body_size, pages)
    if pick is not None:
        title: str | None = pick.text
        sources["title"] = "page_layout"
    else:
        metadata_title = _normalise(raw.get("title", ""))
        if metadata_title:
            title = metadata_title
            sources["title"] = "pdf_metadata"
        else:
            title = _filename_title(path)
            if title:
                sources["title"] = "filename"
            pick = None

    authors = [] if pages >= THESIS_MIN_PAGES else _extract_authors(article_lines, pick)
    if authors:
        sources["authors"] = "page_layout"

    doi, doi_source = _extract_doi(article_lines, header_lines, pre_title_lines)
    if doi:
        sources["doi"] = doi_source or "page_text"

    year, year_source = _extract_year(article_lines, header_lines, doi)
    if year is not None:
        sources["year"] = year_source or "page_text"

    publisher, publisher_source = _extract_journal(
        header_lines, article_lines, pairs, pages, body_size
    )
    if publisher:
        sources["publisher"] = publisher_source or "header"

    evidence_type = _clean_evidence_type(pairs.get("evidence_type"))
    evidence_confidence: float | None = None
    if evidence_type:
        evidence_confidence = 0.9
        sources["evidence_type"] = "metadata_keywords"
    else:
        evidence_type, evidence_confidence = _infer_evidence_type(
            title, article_lines, pages
        )
        if evidence_type:
            sources["evidence_type"] = "rules"

    return {
        "title": title or None,
        "year": year,
        "publisher": publisher,
        "doi": doi,
        "authors": authors,
        "evidence_type": evidence_type or None,
        "evidence_confidence": evidence_confidence,
        "metadata_sources": sources,
        "_title_lines": [line.text for line in pick.candidate.lines] if pick and pick.candidate else [],
    }


# ---------------------------------------------------------------------------
# section segmentation and chunking
# ---------------------------------------------------------------------------


def _is_title_line(
    line: _Line,
    title: str | None,
    title_line_texts: frozenset[str],
    title_page: int,
    body_size: float,
    metadata_title: bool,
) -> bool:
    if not title:
        return False
    if line.text in title_line_texts:
        return True
    if line.page != title_page:
        return False
    if metadata_title:
        return line.text == _normalise(title)
    # Legacy heuristic: without a metadata title, skip a large top-of-page line
    # that looks like the title. Ticket 23 usually supplies explicit title
    # lines, so this is only a fallback.
    return (
        line.size >= body_size * TITLE_SIZE_RATIO
        and line.y0 <= line.page_height * 0.35
        and _pattern_heading_level(line.text) is None
    )


def _assemble_sections(
    lines: list[_Line],
    title: str | None,
    title_line_texts: frozenset[str],
    title_page: int,
    body_size: float,
    metadata_title: bool,
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []
    current_path = PREAMBLE_SECTION_PATH
    paragraphs: list[_Paragraph] = []
    pending: list[_Line] = []

    def flush_paragraph() -> None:
        nonlocal pending
        if not pending:
            return
        text = _join_lines(pending)
        if text:
            paragraphs.append(
                _Paragraph(
                    page_start=pending[0].page,
                    page_end=pending[-1].page,
                    text=text,
                )
            )
        pending = []

    def flush_section() -> None:
        nonlocal paragraphs
        flush_paragraph()
        if paragraphs:
            sections.append(
                {"section_path": current_path, "paragraphs": list(paragraphs)}
            )
            paragraphs = []

    for line in lines:
        if _is_title_line(
            line, title, title_line_texts, title_page, body_size, metadata_title
        ):
            continue
        level = _heading_level(line, body_size)
        if level is not None:
            flush_section()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, line.text))
            current_path = " > ".join(text for _, text in stack)
            continue
        if pending and (pending[-1].page != line.page or pending[-1].block != line.block):
            flush_paragraph()
        pending.append(line)

    flush_section()

    # A section that only contains the page chrome or the title is not usable.
    return [section for section in sections if section["paragraphs"]]


def _split_text(text: str, limit: int) -> list[str]:
    """Split text into pieces of at most ``limit`` estimated tokens."""
    sentences = [fragment for fragment in _SENTENCE_SPLIT_RE.split(text) if fragment.strip()]
    if not sentences:
        sentences = [text]
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        if estimate_tokens(sentence) > limit:
            if current:
                pieces.append(current)
                current = ""
            # A single "sentence" (e.g. an unpunctuated paragraph) still has to
            # be cut. One token never covers less than one character, so a
            # slice of ``limit`` characters cannot exceed ``limit`` tokens.
            pieces.extend(
                sentence[start : start + limit]
                for start in range(0, len(sentence), limit)
            )
            continue
        candidate = _join_fragments(current, sentence)
        if current and estimate_tokens(candidate) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(current)
    return [piece for piece in pieces if piece]


def _split_paragraph(paragraph: _Paragraph, limit: int) -> list[_Paragraph]:
    if estimate_tokens(paragraph.text) <= limit:
        return [paragraph]
    return [
        _Paragraph(paragraph.page_start, paragraph.page_end, piece)
        for piece in _split_text(paragraph.text, limit)
    ]


def _chunk_section(section_path: str, paragraphs: list[_Paragraph]) -> list[dict[str, Any]]:
    chunks: list[list[_Paragraph]] = []
    current: list[_Paragraph] = []
    for paragraph in paragraphs:
        for piece in _split_paragraph(paragraph, MAX_SECTION_TOKENS):
            if current:
                candidate = "\n".join([item.text for item in current] + [piece.text])
                if estimate_tokens(candidate) > MAX_SECTION_TOKENS:
                    chunks.append(current)
                    current = []
            current.append(piece)
    if current:
        chunks.append(current)

    result: list[dict[str, Any]] = []
    for chunk in chunks:
        text = "\n".join(item.text for item in chunk)
        result.append(
            {
                "section_path": section_path,
                "page_start": chunk[0].page_start,
                "page_end": chunk[-1].page_end,
                "text": text,
                "token_count": estimate_tokens(text),
            }
        )
    return result


def _chunk_hash(paper_id: str, section_path: str, page_start: int, text: str) -> str:
    payload = f"{paper_id}{section_path}{page_start}{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:HASH_LEN]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def parse_pdf(path: str | Path) -> dict[str, Any]:
    """Parse ``path`` into document metadata and page-anchored sections.

    Returns exactly::

        {
          "pdf": str, "sha256": str, "title": str | None, "year": int | None,
          "publisher": str | None, "evidence_type": str | None, "pages": int,
          "sections": [
            {"section_path": str, "page_start": int, "page_end": int,
             "text": str, "token_count": int, "chunk_hash": str}
          ],
          "article_start": int, "article_end": int,
          "quality_flags": [str], "metadata_sources": {str: str},
          "evidence_confidence": float | None,
          "doi": str | None, "authors": [str],
        }

    The eight original keys keep their ticket-02 meaning; ticket 23 appends
    article boundaries, provenance of each metadata value, a text-quality gate,
    and the rule-scored evidence type with its confidence.

    Raises :class:`ParseError` for missing, unreadable, encrypted, corrupted or
    text-less input. No partial structure is returned on failure.
    """
    pdf_path = Path(path)
    if not pdf_path.is_file():
        raise ParseError(f"PDF not found: {pdf_path}")
    try:
        data = pdf_path.read_bytes()
    except OSError as exc:
        raise ParseError(f"cannot read PDF {pdf_path}: {exc}") from exc
    if not data:
        raise ParseError(f"PDF is empty: {pdf_path}")

    sha256 = hashlib.sha256(data).hexdigest()
    paper_id = sha256[:PAPER_ID_LEN]

    try:
        # Open the exact bytes that were hashed above, so the identity of the
        # document cannot drift between hashing and parsing.
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - any PyMuPDF open failure is a parse failure
        raise ParseError(f"cannot open PDF {pdf_path.name}: {exc}") from exc

    try:
        if getattr(doc, "needs_pass", False):
            raise ParseError(f"PDF is encrypted: {pdf_path.name}")
        try:
            return _parse_document(doc, pdf_path, sha256, paper_id)
        except ParseError:
            raise
        except Exception as exc:  # noqa: BLE001 - malformed input must not escape
            raise ParseError(f"cannot parse PDF {pdf_path.name}: {exc}") from exc
    finally:
        doc.close()


def _parse_document(
    doc: pymupdf.Document, pdf_path: Path, sha256: str, paper_id: str
) -> dict[str, Any]:
    pages = int(doc.page_count)
    if pages <= 0:
        raise ParseError(f"PDF has no pages: {pdf_path.name}")

    raw_lines = _extract_lines(doc)
    lines, chrome_lines = _drop_running_chrome(raw_lines, pages)
    visible_chars = sum(len(line.text) for line in lines)
    if visible_chars < MIN_TEXT_CHARS:
        raise ParseError(
            f"PDF has no extractable text layer (scanned PDFs are not supported): {pdf_path.name}"
        )

    # Page-1 top-band lines are chrome for metadata purposes even when they
    # occur only once (a one-page paper still prints its journal there), but
    # they stay in ``lines`` for the reader. Title-sized lines are excluded:
    # they are the article title, not a running header.
    body_size = _body_font_size(lines)
    page1_top: list[_Line] = []
    for line in raw_lines:
        if line.page != 1 or line.y0 > line.page_height * 0.08:
            continue
        if line.size >= body_size * ARTICLE_TITLE_RATIO:
            continue
        if any(line == chrome for chrome in chrome_lines):
            continue
        page1_top.append(line)
    header_lines = page1_top + chrome_lines

    quality_flags = _quality_flags(lines, pages)

    start, stop = _detect_article_bounds(lines, body_size, pages)
    article_lines = lines[start:stop]
    if not article_lines:
        article_lines = lines
    article_start = article_lines[0].page if article_lines else 1
    article_end = article_lines[-1].page if article_lines else pages

    # Lines just above the title on the first article page are the DOI /
    # article-number block; they are cut from the body but still metadata.
    pre_title_lines: list[_Line] = []
    if 0 < start < len(lines):
        title_line = lines[start]
        pre_title_lines = [
            line
            for line in lines[:start]
            if line.page == title_line.page
            and line.y0 >= title_line.y0 - AUTHOR_GAP_PT
        ]

    metadata = _document_metadata(
        doc, pdf_path, article_lines, header_lines, pre_title_lines, body_size, pages
    )
    if "garbled_text" in quality_flags:
        # A broken text layer cannot yield trustworthy author names; the
        # document is routed to OCR/manual review instead (report 9.5).
        metadata["authors"] = []
        metadata["metadata_sources"].pop("authors", None)
    title_line_texts = frozenset(metadata.pop("_title_lines", []))
    metadata_title = bool(_normalise(_safe_metadata(doc).get("title", "")))
    title_page = article_lines[0].page if article_lines else 1

    sections: list[dict[str, Any]] = []
    for logical in _assemble_sections(
        article_lines,
        metadata["title"],
        title_line_texts,
        title_page,
        body_size,
        metadata_title,
    ):
        for chunk in _chunk_section(logical["section_path"], logical["paragraphs"]):
            chunk["chunk_hash"] = _chunk_hash(
                paper_id,
                chunk["section_path"],
                chunk["page_start"],
                chunk["text"],
            )
            sections.append(chunk)

    if not sections:
        raise ParseError(f"PDF has no extractable body text: {pdf_path.name}")

    return {
        "pdf": str(pdf_path),
        "sha256": sha256,
        "title": metadata["title"],
        "year": metadata["year"],
        "publisher": metadata["publisher"],
        "evidence_type": metadata["evidence_type"],
        "pages": pages,
        "sections": sections,
        # Ticket 23 additive fields (see docs/contracts.md section 7).
        "article_start": article_start,
        "article_end": article_end,
        "quality_flags": quality_flags,
        "metadata_sources": metadata["metadata_sources"],
        "evidence_confidence": metadata["evidence_confidence"],
        "doi": metadata["doi"],
        "authors": metadata["authors"],
    }


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def run(args: Any, cfg: Any) -> int:
    """CLI entry point for ``mppkb inspect <pdf> [--json]``."""
    json_mode = bool(getattr(args, "json", False))
    try:
        parsed = parse_pdf(Path(args.pdf))
    except ParseError as exc:
        if json_mode:
            _emit({"ok": False, "error": str(exc)})
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    if json_mode:
        payload = {
            "ok": True,
            **{
                key: parsed[key]
                for key in (
                    "pdf",
                    "sha256",
                    "title",
                    "year",
                    "publisher",
                    "evidence_type",
                    "pages",
                    "sections",
                    "article_start",
                    "article_end",
                    "quality_flags",
                    "metadata_sources",
                    "evidence_confidence",
                    "doi",
                    "authors",
                )
            },
        }
        _emit(payload)
        return 0

    print(f"pdf: {parsed['pdf']}")
    print(f"sha256: {parsed['sha256']}")
    print(f"pages: {parsed['pages']}")
    print(f"title: {parsed['title'] or '-'}")
    print(f"year: {parsed['year'] if parsed['year'] is not None else '-'}")
    print(f"publisher: {parsed['publisher'] or '-'}")
    print(f"doi: {parsed['doi'] or '-'}")
    print(f"authors: {'，'.join(parsed['authors']) or '-'}")
    print(f"evidence_type: {parsed['evidence_type'] or '-'}")
    confidence = parsed["evidence_confidence"]
    print(f"evidence_confidence: {confidence if confidence is not None else '-'}")
    print(f"article: p{parsed['article_start']}-p{parsed['article_end']}")
    print(f"quality_flags: {', '.join(parsed['quality_flags']) or '-'}")
    print("metadata_sources:")
    for key in sorted(parsed["metadata_sources"]):
        print(f"  {key}: {parsed['metadata_sources'][key]}")
    print("sections:")
    for section in parsed["sections"]:
        print(
            f"  {section['section_path']}  "
            f"p{section['page_start']}-{section['page_end']}  "
            f"{section['token_count']} tokens  "
            f"{section['chunk_hash']}"
        )
    return 0
