"""Ingestion pipeline (ticket 03).

Parses PDFs, keeps the corpus index current, and runs the extraction port to
produce hot-store candidates. Idempotent per (paper, section, extractor) through
``ingest_log``; a run stops explicitly when it would exceed the batch cap.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from .clock import now_iso
from .config import Config
from .db import ensure_databases
from .extract import (
    ExtractionError,
    RealBackend,
    ReplayBackend,
    TwoPassBackend,
    validate_candidate,
)
from .parse import ParseError, parse_pdf
from .text import to_grams


class BatchCapExceeded(Exception):
    """Raised when one run would create more candidates than the batch cap."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _fail(args: Any, message: str, code: int = 1) -> int:
    if getattr(args, "json", False):
        _emit({"ok": False, "error": message})
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def _make_backend(args: Any):
    extractor = getattr(args, "extractor", "replay")
    if extractor == "real":
        return RealBackend()
    if extractor == "two_pass":
        return TwoPassBackend()
    replay_file = getattr(args, "replay_file", None)
    if not replay_file:
        raise ExtractionError("replay extractor requires --replay-file")
    return ReplayBackend(replay_file)


def _pdfs(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(
            (p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"),
            key=lambda p: p.name,
        )
    return [path]


def _index_corpus(corpus, parsed: dict[str, Any], paper_id: str, now: str) -> None:
    with corpus:
        corpus.execute(
            "INSERT INTO papers(paper_id, sha256, path, title, year, publisher, "
            "evidence_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(paper_id) DO UPDATE SET path = excluded.path, "
            "title = excluded.title, year = excluded.year, "
            "publisher = excluded.publisher, evidence_type = excluded.evidence_type",
            (
                paper_id,
                parsed["sha256"],
                str(parsed["pdf"]),
                parsed.get("title"),
                parsed.get("year"),
                parsed.get("publisher"),
                parsed.get("evidence_type"),
                now,
            ),
        )
        for section in parsed["sections"]:
            chunk_id = str(section["chunk_hash"])
            corpus.execute(
                "INSERT OR IGNORE INTO chunks(chunk_id, paper_id, section_path, "
                "page_start, page_end, text, token_count, chunk_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk_id,
                    paper_id,
                    section.get("section_path"),
                    section["page_start"],
                    section["page_end"],
                    section["text"],
                    section["token_count"],
                    chunk_id,
                    now,
                ),
            )
            corpus.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
            corpus.execute(
                "INSERT INTO chunks_fts(chunk_id, text) VALUES (?, ?)",
                (chunk_id, to_grams(section["text"])),
            )


def _already_ingested(kb, paper_id: str, chunk_hash: str, extractor: str) -> bool:
    row = kb.execute(
        "SELECT 1 FROM ingest_log WHERE paper_id = ? AND chunk_hash = ? AND extractor = ?",
        (paper_id, chunk_hash, extractor),
    ).fetchone()
    return row is not None


def _error_reason(exc: ExtractionError) -> str:
    text = str(exc)
    if "no_replay_entry" in text:
        return "no_replay_entry"
    if "malformed_response" in text:
        return "malformed_response"
    return "extractor_error"


def _log_ingest(kb, paper_id, chunk_hash, extractor, outcome, detail, now) -> None:
    kb.execute(
        "INSERT OR REPLACE INTO ingest_log(paper_id, chunk_hash, extractor, "
        "outcome, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (paper_id, chunk_hash, extractor, outcome, detail, now),
    )


RELEVANT_SECTION_TERMS = (
    "治疗",
    "用药",
    "剂量",
    "疗程",
    "禁忌",
    "推荐",
    "方案",
    "随访",
    "诊断",
    "共识",
    "指南",
    "antibiotic",
    "treatment",
    "dose",
    "therapy",
)


def _select_relevant_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored: list[tuple[int, int, int, dict[str, Any]]] = []
    for index, section in enumerate(sections):
        text = str(section.get("text") or "")
        if len(text) < 80:
            continue
        path = " ".join(str(part) for part in (section.get("path") or []))
        score = sum(2 for term in RELEVANT_SECTION_TERMS if term in path)
        score += sum(1 for term in RELEVANT_SECTION_TERMS if term in text)
        if len(text) > 400:
            score += 1
        if score:
            scored.append((score, len(text), index, section))
    if not scored:
        return sections
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[3] for item in scored]


def _ingest_pdf(
    pdf: Path,
    extractor: str,
    backend,
    kb,
    corpus,
    cfg: Config,
    report: dict[str, Any],
    max_sections: int = 0,
    select_sections: bool = False,
) -> None:
    parsed = parse_pdf(pdf)
    paper_id = parsed["sha256"][:16]
    now = now_iso()
    _index_corpus(corpus, parsed, paper_id, now)

    document = dict(parsed)
    document["paper_id"] = paper_id
    report["papers"] += 1
    sections = parsed["sections"]
    if select_sections:
        sections = _select_relevant_sections(sections)
    if max_sections and max_sections > 0:
        report["sections_capped"] = report.get("sections_capped", 0) + max(
            0, len(sections) - max_sections
        )
        sections = sections[:max_sections]
    report["sections"] += len(sections)

    for section in sections:
        chunk_hash = str(section["chunk_hash"])
        if _already_ingested(kb, paper_id, chunk_hash, extractor):
            report["skipped"] += 1
            continue

        payloads: list[dict[str, Any]] = []
        extraction_error: ExtractionError | None = None
        report["calls"] += 1
        try:
            payloads = backend.extract(document, section)
        except ExtractionError as exc:
            extraction_error = exc

        needs_retry = extraction_error is not None or any(
            validate_candidate(payload, document, section) for payload in payloads
        )
        if needs_retry:
            report["calls"] += 1
            try:
                retry_payloads = backend.extract(document, section)
                payloads = retry_payloads
                extraction_error = None
            except ExtractionError as exc:
                payloads = []
                extraction_error = exc

        if extraction_error is not None:
            reason = _error_reason(extraction_error)
            with kb:
                _log_ingest(kb, paper_id, chunk_hash, extractor, "rejected", str(extraction_error), now)
            report["rejected"] += 1
            report["rejections"].append(
                {"chunk_hash": chunk_hash, "reason": reason, "detail": str(extraction_error)}
            )
            continue

        inserted = 0
        rejected: list[dict[str, Any]] = []
        with kb:
            for payload in payloads:
                problems = validate_candidate(payload, document, section)
                payload_json = _canonical(payload)
                candidate_id = _sha16(paper_id + chunk_hash + payload_json)
                if problems:
                    reason = (
                        "quote_not_found"
                        if any("quote_not_found" in item for item in problems)
                        else "invalid_schema"
                    )
                    kb.execute(
                        "INSERT OR IGNORE INTO candidates(candidate_id, paper_id, "
                        "chunk_hash, section_path, payload_json, status, reason, "
                        "created_at) VALUES (?, ?, ?, ?, ?, 'rejected', ?, ?)",
                        (
                            candidate_id,
                            paper_id,
                            chunk_hash,
                            section.get("section_path"),
                            payload_json,
                            reason,
                            now,
                        ),
                    )
                    rejected.append(
                        {
                            "chunk_hash": chunk_hash,
                            "reason": reason,
                            "detail": "; ".join(problems),
                        }
                    )
                    continue

                if report["candidates"] + inserted >= cfg.batch_cap:
                    raise BatchCapExceeded(
                        f"candidate cap {cfg.batch_cap} reached; split the input"
                    )
                cursor = kb.execute(
                    "INSERT OR IGNORE INTO candidates(candidate_id, paper_id, "
                    "chunk_hash, section_path, payload_json, status, reason, "
                    "created_at) VALUES (?, ?, ?, ?, ?, 'proposed', NULL, ?)",
                    (
                        candidate_id,
                        paper_id,
                        chunk_hash,
                        section.get("section_path"),
                        payload_json,
                        now,
                    ),
                )
                if cursor.rowcount:
                    inserted += 1
            _log_ingest(kb, paper_id, chunk_hash, extractor, "ok", None, now)

        report["candidates"] += inserted
        report["rejected"] += len(rejected)
        report["rejections"].extend(rejected)


def _output(args: Any, report: dict[str, Any]) -> None:
    if getattr(args, "json", False):
        _emit(report)
        return
    for key in ("papers", "sections", "calls", "candidates", "rejected", "skipped"):
        print(f"{key}: {report[key]}")
    for rejection in report["rejections"]:
        print(f"rejected {rejection['chunk_hash']}: {rejection['reason']}")
    for error in report["errors"]:
        print(f"error {error['pdf']}: {error['error']}")


def run(args: Any, cfg: Config) -> int:
    source = Path(getattr(args, "path", ""))
    if not source.exists():
        return _fail(args, f"path not found: {source}")

    try:
        backend = _make_backend(args)
    except ExtractionError as exc:
        return _fail(args, str(exc))

    kb, corpus = ensure_databases(cfg)
    report: dict[str, Any] = {
        "ok": True,
        "papers": 0,
        "sections": 0,
        "calls": 0,
        "candidates": 0,
        "rejected": 0,
        "skipped": 0,
        "batch": {"cap": cfg.batch_cap, "over_cap": False},
        "rejections": [],
        "errors": [],
    }

    try:
        for pdf in _pdfs(source):
            try:
                _ingest_pdf(
                    pdf,
                    args.extractor,
                    backend,
                    kb,
                    corpus,
                    cfg,
                    report,
                    int(getattr(args, "max_sections", 0) or 0),
                    bool(getattr(args, "select_sections", False)),
                )
            except ParseError as exc:
                report["errors"].append({"pdf": str(pdf), "error": str(exc)})
    except BatchCapExceeded as exc:
        report["ok"] = False
        report["batch"]["over_cap"] = True
        report["error"] = f"batch_cap_exceeded: {exc}"
        _output(args, report)
        return 1

    if getattr(backend, "usage", None):
        report["usage"] = dict(backend.usage)
    report["ok"] = not report["errors"]
    _output(args, report)
    return 0 if report["ok"] else 1
