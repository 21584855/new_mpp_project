"""Merged todo view: KB queue + P0 confirm + case gate (ticket 21)."""

from __future__ import annotations

import json
import sys
from typing import Any

from .config import Config
from .db import ensure_databases
from .promote import list_queue


def collect(kb) -> dict[str, Any]:
    items = list_queue(kb)
    p0_items = [item for item in items if item.get("provenance") == "p0_exception"]
    case_gate = kb.execute(
        "SELECT COUNT(*) FROM case_audit WHERE decision = 'pending'"
    ).fetchone()[0]
    return {
        "ok": True,
        "kb_queue": len(items),
        "p0_queue": len(p0_items),
        "case_gate": int(case_gate),
        "total": len(items) + int(case_gate),
    }


def run(args: Any, cfg: Config) -> int:
    kb, _corpus = ensure_databases(cfg)
    payload = collect(kb)
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"KB 待审: {payload['kb_queue']}")
        print(f"P0 补审: {payload['p0_queue']}")
        print(f"病例 Gate: {payload['case_gate']}")
        print(f"合计: {payload['total']}")
    return 0
