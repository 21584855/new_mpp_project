"""Text normalisation helpers.

FTS5's ``trigram`` tokenizer silently misses 2-character Chinese terms, so the
corpus index stores 2-gram preprocessed text instead (``to_grams``) and queries
are built the same way. ASCII runs are kept as lowercased words.
"""

from __future__ import annotations

import re

_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"
_CJK_RE = re.compile(f"[{_CJK}]")
_ASCII_WORD_RE = re.compile(r"[a-z0-9]+")


def to_grams(text: str) -> str:
    """Return space separated 2-grams for CJK runs plus lowercase ASCII words."""
    grams: list[str] = []
    run: list[str] = []
    for char in text:
        if _CJK_RE.match(char):
            run.append(char)
            continue
        if run:
            grams.extend(_run_grams(run))
            run = []
    if run:
        grams.extend(_run_grams(run))
    grams.extend(_ASCII_WORD_RE.findall(text.lower()))
    return " ".join(grams)


def _run_grams(run: list[str]) -> list[str]:
    if len(run) == 1:
        return [run[0]]
    return [run[i] + run[i + 1] for i in range(len(run) - 1)]
