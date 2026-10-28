"""Consistent SQLite backup for mppkb deployment data.

Usage:
    python deploy/backup.py --data /srv/mppkb/data --out /srv/mppkb/backups [--keep 7]

Backs up kb.sqlite, corpus.sqlite, graph.sqlite via SQLite's online backup API
and copies ontology.yaml; creates a timestamped zip archive.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def backup_sqlite(source: Path, target: Path) -> None:
    if not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--keep", type=int, default=7)
    parser.add_argument("--ontology", default="ontology.yaml")
    args = parser.parse_args()

    data = Path(args.data).resolve()
    out = Path(args.out).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work = out / f"_work_{stamp}"
    work.mkdir(parents=True, exist_ok=True)

    files = []
    for name in ("kb.sqlite", "corpus.sqlite", "graph.sqlite"):
        source = data / name
        if source.is_file():
            backup_sqlite(source, work / name)
            files.append(name)
    ontology = Path(args.ontology)
    if ontology.is_file():
        shutil.copy2(ontology, work / ontology.name)
        files.append(ontology.name)
    (work / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": stamp,
                "source": str(data),
                "files": files,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    archive = out / f"mppkb-backup-{stamp}"
    shutil.make_archive(str(archive), "zip", root_dir=work)
    shutil.rmtree(work, ignore_errors=True)
    print(archive.with_suffix(".zip"))

    backups = sorted(out.glob("mppkb-backup-*.zip"), key=lambda path: path.stat().st_mtime)
    for old in backups[: max(0, len(backups) - args.keep)]:
        old.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
