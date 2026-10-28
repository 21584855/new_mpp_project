#!/usr/bin/env python3
"""Create a fully isolated, synthetic demo database.

This script never touches the production database or PDFs. It writes only to
``demo/data`` and uses the synthetic fixture in ``demo/fixtures``.

Usage:
    python tools/seed_demo.py            # reset demo data and seed
    python tools/seed_demo.py --keep     # keep existing demo data
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
DATA = DEMO / "data"
WORK = DEMO / "work"
FIXTURE = DEMO / "fixtures" / "demo_mini.pdf"
QUERY = "儿童 阿奇霉素 剂量 疗程 禁忌"


def env() -> dict[str, str]:
    values = {k: v for k, v in os.environ.items() if not k.startswith("MPPKB_")}
    values.update(
        {
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "MPPKB_KB_DB": str(DATA / "kb.sqlite"),
            "MPPKB_CORPUS_DB": str(DATA / "corpus.sqlite"),
            "MPPKB_CORPUS_DIR": str(DATA / "pdfs"),
            "MPPKB_ONTOLOGY": str(ROOT / "ontology.yaml"),
            "MPPKB_RETRIEVAL_MODE": "lexical",
            "MPPKB_ORCHESTRATION_V2": "1",
            "MPPKB_ANSWER_WRITEBACK": "0",
        }
    )
    return values


def run(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "mppkb", *args],
        cwd=str(ROOT),
        env=env(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
    )
    if result.returncode != 0:
        if capture:
            sys.stderr.write(result.stdout or "")
            sys.stderr.write(result.stderr or "")
        raise SystemExit(f"command failed ({result.returncode}): mppkb {' '.join(args)}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep existing demo/data")
    args = parser.parse_args()

    if not FIXTURE.is_file():
        raise SystemExit(f"missing synthetic fixture: {FIXTURE}")
    if DATA.exists() and not args.keep:
        shutil.rmtree(DATA)
    if WORK.exists():
        shutil.rmtree(WORK)
    DATA.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    (DATA / "pdfs").mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURE, DATA / "pdfs" / FIXTURE.name)

    print("[1/4] inspect synthetic fixture")
    info = json.loads(run("inspect", str(FIXTURE), "--json", capture=True).stdout)
    section = info["sections"][0]
    doc_id = info["sha256"][:16]
    anchor = {
        "doc_id": doc_id,
        "page_start": section["page_start"],
        "page_end": section["page_end"],
        "quote": section["text"].strip()[:120],
    }

    def claim(attribute: str, value: dict, direction: str | None = None, severity: str | None = None) -> dict:
        return {
            "population": "儿童",
            "severity": severity,
            "intervention": "阿奇霉素",
            "attribute": attribute,
            "value": value,
            "direction": direction,
            "evidence_type": "guideline",
            "confidence": 0.9,
            "anchors": [anchor],
        }

    claims = [
        claim("一线治疗", {"kind": "direction", "value": "first_line"}, "first_line"),
        claim("二线治疗", {"kind": "direction", "value": "second_line"}, "second_line"),
        claim("剂量", {"kind": "numeric", "value": 10, "unit": "mg/kg/d"}),
        claim("疗程", {"kind": "numeric", "value": 3, "unit": "天"}),
        claim("禁忌", {"kind": "direction", "value": "avoid"}, "avoid"),
        claim("联合用药", {"kind": "direction", "value": "first_line"}, "first_line"),
        claim("剂量", {"kind": "numeric", "value": 20, "unit": "mg/kg/d"}, severity="重症"),
        claim("疗程", {"kind": "numeric", "value": 5, "unit": "天"}, severity="重症"),
    ]
    replay = {"__default__": [], section["chunk_hash"]: claims}
    replay_path = WORK / "replay.json"
    replay_path.write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding="utf-8")

    print("[2/4] ingest synthetic claims")
    run("ingest", str(FIXTURE), "--replay-file", str(replay_path), "--json", capture=True)

    print("[3/4] normalize and promote")
    run("remap", "--json", capture=True)
    status = json.loads(run("status", "--json", capture=True).stdout)

    print("[4/4] smoke test a demo answer")
    answer = json.loads(run("answer", "--query", QUERY, "--json", capture=True).stdout)
    (WORK / "demo_answer.json").write_text(
        json.dumps(answer, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    block = answer.get("evidence_status_block") or {}
    print("")
    print("=" * 66)
    print("SYNTHETIC DEMO — NOT CLINICAL DATA / 测试版本，不可用于临床")
    print("=" * 66)
    print(f"data    : {DATA}")
    print(f"claims  : {status.get('counts', {}).get('claims')}")
    print(f"queue   : {status.get('counts', {}).get('queue')}")
    print(f"status  : {block.get('summary')}")
    print(f"query   : {QUERY}")
    print(f"refused : {answer.get('refused')}")
    print(f"gaps    : {answer.get('evidence_gaps')}")
    print(f"fills   : {[(f.get('drug'), f.get('action')) for f in answer.get('gap_fills') or []]}")
    print("")
    print("Launch UI with:")
    kb_path = DATA / "kb.sqlite"
    corpus_path = DATA / "corpus.sqlite"
    pdf_path = DATA / "pdfs"
    print(f'  $env:MPPKB_KB_DB="{kb_path}"')
    print(f'  $env:MPPKB_CORPUS_DB="{corpus_path}"')
    print(f'  $env:MPPKB_CORPUS_DIR="{pdf_path}"')
    print('  $env:MPPKB_ORCHESTRATION_V2="1"')
    print("  python -m mppkb.ui_bridge --repo ..\\MPP_Project --port 8793")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
