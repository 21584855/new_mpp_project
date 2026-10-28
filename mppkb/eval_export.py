"""Structured evaluation export (ticket 22).

Answers already carry a structured response (path, claims, citations, status,
budgets, model versions). This command exports the cached responses as one
deterministic JSON document so evaluation scripts never parse prose.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .config import Config
from .db import ensure_databases

EXPORT_FIELDS = (
    "path",
    "routing",
    "library_version",
    "claims",
    "citations",
    "sections",
    "evidence_status",
    "p0_warning",
    "note",
    "refused",
    "message",
    "budgets",
    "model",
    "disclaimer",
)


def export_records(kb) -> list[dict[str, Any]]:
    rows = kb.execute(
        "SELECT seq, case_id, question_hash, snapshot_id, record_json, created_at "
        "FROM answer_log ORDER BY seq"
    ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        try:
            response = json.loads(row["record_json"])
        except json.JSONDecodeError:
            continue
        record = {
            "seq": row["seq"],
            "case_id": row["case_id"],
            "question_hash": row["question_hash"],
            "snapshot_id": row["snapshot_id"],
            "generated_at": row["created_at"],
            "library_version": response.get("library_version"),
        }
        for field in EXPORT_FIELDS:
            record[field] = response.get(field)
        # Missing values are explicit nulls, never omitted.
        records.append(record)
    return records


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _fail(args: Any, message: str, code: int = 1) -> int:
    if getattr(args, "json", False):
        _emit({"ok": False, "error": message})
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def run(args: Any, cfg: Config) -> int:
    out_path = Path(getattr(args, "out", "") or "")
    if not str(out_path):
        return _fail(args, "export requires --out")
    kb, _corpus = ensure_databases(cfg)
    records = export_records(kb)
    payload = {"ok": True, "count": len(records), "records": records}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if getattr(args, "json", False):
        _emit({"ok": True, "count": len(records), "out": str(out_path)})
    else:
        print(f"exported {len(records)} records to {out_path}")
    return 0
