"""Command line interface for the MPP cold/hot knowledge base.

The CLI is the primary application boundary used by behaviour tests (see
``docs/contracts.md``). Commands that belong to later tickets are declared here
but resolve lazily to their implementation module.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .config import Config, ConfigError, load_config
from .db import (
    ensure_databases,
    library_version,
    ontology_version,
)

# command -> module that exposes ``run(args, cfg) -> int``
MODULE_COMMANDS: dict[str, str] = {
    "answer": "mppkb.answer",
    "graph": "mppkb.graph",
    "inspect": "mppkb.parse",
    "index": "mppkb.index",
    "metrics": "mppkb.metrics",
    "tools": "mppkb.tools",
    "todos": "mppkb.todos",
    "synth": "mppkb.synthesis",
    "ingest": "mppkb.ingest",
    "ontology": "mppkb.ontology",
    "remap": "mppkb.normalize",
    "review": "mppkb.promote",
    "rebuild": "mppkb.rebuild",
    "serve": "mppkb.web",
    "migrate": "mppkb.migrate",
    "p0": "mppkb.p0",
    "export": "mppkb.eval_export",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mppkb", description="MPP cold/hot knowledge base"
    )
    parser.add_argument("--kb-db", help="path to the governance database")
    parser.add_argument("--corpus-db", help="path to the corpus index database")
    parser.add_argument("--corpus-dir", help="directory containing PDF sources")
    parser.add_argument("--ttl-days", type=int, help="hot store TTL in days")
    parser.add_argument("--batch-cap", type=int, help="max candidates per batch")
    parser.add_argument("--sample-rate", type=float, help="auto-promotion sampling rate")
    parser.add_argument("--evidence-min", help="comma separated evidence types for auto-promotion")
    parser.add_argument("--reviewer", help="reviewer name recorded on decisions")
    parser.add_argument("--port", type=int, help="HTTP port for serve")
    parser.add_argument("--ontology", help="path to the ontology file")

    sub = parser.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("status", help="show database status and counts")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("claims", help="query claims")
    csub = sp.add_subparsers(dest="claims_command", required=True)
    cl = csub.add_parser("list", help="list claims")
    cl.add_argument("--status", help="filter by claim version status")
    cl.add_argument("--json", action="store_true")

    sp = sub.add_parser("candidates", help="query extracted candidates")
    casub = sp.add_subparsers(dest="candidates_command", required=True)
    cal = casub.add_parser("list", help="list candidates")
    cal.add_argument("--status", help="filter by candidate status")
    cal.add_argument("--json", action="store_true")

    sp = sub.add_parser("inspect", help="parse a PDF into sections")
    sp.add_argument("pdf")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("ingest", help="ingest PDFs into the hot store")
    sp.add_argument("path")
    sp.add_argument("--extractor", default="replay", choices=["replay", "real", "two_pass"])
    sp.add_argument("--replay-file", help="JSON replays keyed by chunk hash (replay extractor)")
    sp.add_argument("--max-sections", type=int, default=0, help="cap sections per PDF (0 = all)")
    sp.add_argument(
        "--select-sections",
        action="store_true",
        help="prefer sections whose heading/text looks treatment-related",
    )
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("answer", help="answer a question from the cold library")
    sp.add_argument("--query", required=True)
    sp.add_argument("--case-id")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("graph", help="LangGraph orchestration")
    gsub = sp.add_subparsers(dest="graph_command", required=True)
    grun = gsub.add_parser("run", help="run a case through the graph")
    grun.add_argument("--case-json", required=True)
    grun.add_argument("--thread-id")
    grun.add_argument("--json", action="store_true")
    gres = gsub.add_parser("resume", help="resume an interrupted graph run")
    gres.add_argument("--thread-id", required=True)
    gres.add_argument("--decision", default="approve", choices=["approve", "reject", "timeout"])
    gres.add_argument("--json", action="store_true")

    sp = sub.add_parser("metrics", help="metrics, gates and monitoring")
    msub = sp.add_subparsers(dest="metrics_command", required=True)
    mrep = msub.add_parser("report", help="live metrics and monitoring snapshot")
    mrep.add_argument("--json", action="store_true")
    mgat = msub.add_parser("gate", help="promotion gate checks")
    mgat.add_argument("--baseline")
    mgat.add_argument("--json", action="store_true")
    mev = msub.add_parser("eval", help="evaluate labeled cases")
    mev.add_argument("--cases", required=True)
    mev.add_argument("--json", action="store_true")

    sp = sub.add_parser("index", help="derived vector index")
    isub = sp.add_subparsers(dest="index_command", required=True)
    ireb = isub.add_parser("rebuild", help="rebuild cold/hot collections from SQLite")
    ireb.add_argument("--json", action="store_true")
    isea = isub.add_parser("search", help="vector search with rerank")
    isea.add_argument("--query", required=True)
    isea.add_argument("--library", default="cold", choices=["cold", "hot"])
    isea.add_argument("--top-k", type=int, default=5)
    isea.add_argument("--json", action="store_true")
    ist = isub.add_parser("status", help="index freshness and counts")
    ist.add_argument("--json", action="store_true")

    sp = sub.add_parser("tools", help="deterministic tool registry")
    tsub = sp.add_subparsers(dest="tools_command", required=True)
    tlist = tsub.add_parser("list", help="list available tools")
    tlist.add_argument("--json", action="store_true")
    trun = tsub.add_parser("run", help="run one tool with JSON input")
    trun.add_argument("--name", required=True)
    trun.add_argument("--input-json", required=True)
    trun.add_argument("--json", action="store_true")

    sp = sub.add_parser("todos", help="merged todo view (KB queue, P0, case gate)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("synth", help="constrained synthesis and citation check")
    ssub = sp.add_subparsers(dest="synth_command", required=True)
    srun = ssub.add_parser("run", help="synthesize attributed sentences from facts")
    srun.add_argument("--facts", required=True)
    srun.add_argument("--json", action="store_true")
    scheck = ssub.add_parser("check", help="check and repair sentence citations")
    scheck.add_argument("--facts", required=True)
    scheck.add_argument("--sentences", required=True)
    scheck.add_argument("--json", action="store_true")

    sp = sub.add_parser("ontology", help="manage the controlled vocabulary")
    osub = sp.add_subparsers(dest="ontology_command", required=True)
    ol = osub.add_parser("list", help="list vocabulary terms")
    ol.add_argument("--json", action="store_true")
    oa = osub.add_parser("add", help="append a vocabulary term")
    oa.add_argument("--kind", required=True)
    oa.add_argument("--term", required=True)
    oa.add_argument("--label", required=True)
    oa.add_argument("--json", action="store_true")

    sp = sub.add_parser("remap", help="re-run ontology mapping for unsupported candidates")
    sp.add_argument("--include-labs", action="store_true", help="also accept lab-threshold claims")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("review", help="review and decide candidate claims")
    rsub = sp.add_subparsers(dest="review_command", required=True)
    rl = rsub.add_parser("list", help="list pending review items")
    rl.add_argument("--json", action="store_true")
    ra = rsub.add_parser("approve", help="approve a candidate")
    ra.add_argument("claim_id")
    ra.add_argument("--version-id")
    ra.add_argument("--json", action="store_true")
    rr = rsub.add_parser("reject", help="reject a candidate")
    rr.add_argument("claim_id")
    rr.add_argument("--reason", required=True)
    rr.add_argument("--note", help="custom reviewer note")
    rr.add_argument("--version-id")
    rr.add_argument("--json", action="store_true")
    rh = rsub.add_parser("hold", help="hold a candidate")
    rh.add_argument("claim_id")
    rh.add_argument("--note", help="custom reviewer note")
    rh.add_argument("--version-id")
    rh.add_argument("--json", action="store_true")

    sp = sub.add_parser("rebuild", help="replay the ledger and verify the cold library")
    sp.add_argument("--verify", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("serve", help="run the review console and answer API")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int)

    sp = sub.add_parser("migrate", help="import legacy hand-written knowledge")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("p0", help="manage P0 safety rules")
    psub = sp.add_subparsers(dest="p0_command", required=True)
    pl = psub.add_parser("list", help="list P0 rules")
    pl.add_argument("--json", action="store_true")

    sp = sub.add_parser("export", help="export structured evaluation records")
    sp.add_argument("--out", required=True)
    sp.add_argument("--case-json")
    sp.add_argument("--json", action="store_true")

    return parser


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "kb_db": args.kb_db,
        "corpus_db": args.corpus_db,
        "corpus_dir": args.corpus_dir,
        "ttl_days": args.ttl_days,
        "batch_cap": args.batch_cap,
        "sample_rate": args.sample_rate,
        "evidence_min": args.evidence_min,
        "reviewer": args.reviewer,
        "port": args.port,
        "ontology": args.ontology,
    }


def _wants_json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _fail(args: argparse.Namespace, message: str, code: int) -> int:
    if _wants_json(args):
        _emit({"ok": False, "error": message})
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def _count(conn, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def _status_payload(cfg: Config, kb, ontology_version_value: int) -> dict[str, Any]:
    counts = {
        "claims": _count(kb, "SELECT COUNT(*) FROM claims"),
        "versions": _count(kb, "SELECT COUNT(*) FROM claim_versions"),
        "proposed": _count(kb, "SELECT COUNT(*) FROM claim_versions WHERE status = 'proposed'"),
        "queue": _count(
            kb,
            "SELECT COUNT(*) FROM claim_versions "
            "WHERE status IN ('proposed', 'conflicted', 'unsupported')",
        ),
        "expired": _count(kb, "SELECT COUNT(*) FROM claim_versions WHERE status = 'expired'"),
        "held": _count(kb, "SELECT COUNT(*) FROM claim_versions WHERE status = 'held'"),
    }
    return {
        "ok": True,
        "kb_db": str(cfg.kb_db),
        "corpus_db": str(cfg.corpus_db),
        "corpus_dir": str(cfg.corpus_dir),
        "counts": counts,
        "library_version": library_version(kb),
        "ontology_version": ontology_version_value,
        "ttl_days": cfg.ttl_days,
    }


def run_status(args: argparse.Namespace, cfg: Config) -> int:
    corpus_dir = Path(cfg.corpus_dir)
    if not corpus_dir.is_dir():
        return _fail(args, f"corpus directory not found: {corpus_dir}", 1)

    kb, _corpus = ensure_databases(cfg)
    payload = _status_payload(cfg, kb, ontology_version(kb))

    if _wants_json(args):
        _emit(payload)
        return 0

    print(f"kb_db: {payload['kb_db']}")
    print(f"corpus_db: {payload['corpus_db']}")
    print(f"corpus_dir: {payload['corpus_dir']}")
    print(f"library_version: {payload['library_version']}")
    print(f"ontology_version: {payload['ontology_version']}")
    for key, value in payload["counts"].items():
        print(f"{key}: {value}")
    return 0


def _claim_item(row) -> dict[str, Any]:
    return {
        "claim_id": row["claim_id"],
        "version_id": row["version_id"],
        "slot_key": row["slot_key"],
        "slot": {
            "population": row["population"],
            "severity": row["severity"],
            "intervention": row["intervention"],
            "attribute": row["attribute"],
            "arm": row["arm"],
        },
        "status": row["status"],
        "provenance": row["provenance"],
        "evidence_type": row["evidence_type"],
        "value": json.loads(row["value_json"]),
        "direction": row["direction"],
        "anchors": json.loads(row["anchors_json"]),
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
        "decided_by": row["decided_by"],
        "reason_code": row["reason_code"],
        "published_date": row["published_date"],
        "effective_date": row["effective_date"],
        "retrieved_date": row["retrieved_date"],
        "supersedes": row["supersedes"],
        "superseded_by": row["superseded_by"],
    }


def run_claims(args: argparse.Namespace, cfg: Config) -> int:
    kb, _corpus = ensure_databases(cfg)
    sql = (
        "SELECT v.*, c.slot_key, c.population, c.severity, c.intervention, c.attribute, c.arm "
        "FROM claim_versions v JOIN claims c ON c.claim_id = v.claim_id"
    )
    params: list[Any] = []
    status = getattr(args, "status", None)
    if status:
        sql += " WHERE v.status = ?"
        params.append(status)
    sql += " ORDER BY v.claim_id, v.created_at, v.version_id"
    rows = kb.execute(sql, params).fetchall()

    if status:
        selected = list(rows)
    else:
        latest: dict[str, Any] = {}
        for row in rows:
            latest[row["claim_id"]] = row
        selected = [latest[claim_id] for claim_id in sorted(latest)]

    items = [_claim_item(row) for row in selected]

    if _wants_json(args):
        _emit({"ok": True, "claims": items})
        return 0

    if not items:
        print("no claims")
        return 0
    for item in items:
        slot = item["slot"]
        print(
            f"{item['claim_id']}  {item['status']:<10} "
            f"{slot['population']}|{slot['severity'] or '-'}|"
            f"{slot['intervention']}|{slot['attribute']}  "
            f"{json.dumps(item['value'], ensure_ascii=False)}"
        )
    return 0


def run_candidates(args: argparse.Namespace, cfg: Config) -> int:
    kb, _corpus = ensure_databases(cfg)
    sql = (
        "SELECT candidate_id, paper_id, chunk_hash, section_path, payload_json, "
        "status, reason, created_at FROM candidates"
    )
    params: list[Any] = []
    status = getattr(args, "status", None)
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at, candidate_id"
    rows = kb.execute(sql, params).fetchall()

    items = [
        {
            "candidate_id": row["candidate_id"],
            "paper_id": row["paper_id"],
            "chunk_hash": row["chunk_hash"],
            "section_path": row["section_path"],
            "status": row["status"],
            "reason": row["reason"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]

    if _wants_json(args):
        _emit({"ok": True, "candidates": items})
        return 0

    if not items:
        print("no candidates")
        return 0
    for item in items:
        payload = item["payload"]
        print(
            f"{item['candidate_id']}  {item['status']:<10} {item['paper_id']}  "
            f"{payload.get('population')}|{payload.get('intervention')}|"
            f"{payload.get('attribute')}  {json.dumps(payload.get('value'), ensure_ascii=False)}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = load_config(_overrides(args))
    except ConfigError as exc:
        return _fail(args, str(exc), 2)

    try:
        if args.command == "status":
            return run_status(args, cfg)
        if args.command == "claims":
            return run_claims(args, cfg)
        if args.command == "candidates":
            return run_candidates(args, cfg)

        module_name = MODULE_COMMANDS.get(args.command)
        if module_name is None:
            return _fail(args, f"unknown command: {args.command}", 2)
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            return _fail(args, f"command '{args.command}' is not implemented yet", 2)
        run = getattr(module, "run", None)
        if run is None:
            return _fail(args, f"command '{args.command}' is not implemented yet", 2)
        return int(run(args, cfg) or 0)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        if os.environ.get("MPPKB_DEBUG"):
            raise
        return _fail(args, str(exc), 1)


if __name__ == "__main__":
    raise SystemExit(main())
