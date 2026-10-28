"""Patch the external MPP_Project evidence graph for readability.

The upstream frontend uses 134x78 nodes, 12-char x 2-line titles and 170px
lanes, so long Chinese titles are truncated and edges cross node cards.

Run after cloning MPP_Project:
    python deploy/patch_frontend_graph.py --repo deploy/MPP_Project
"""

from __future__ import annotations

import argparse
from pathlib import Path

REPLACEMENTS = [
    (
        "const viewWidth = Math.max(760, orderedTypes.length * 170 + 70);\n"
        "  const viewHeight = 360;",
        "const maxRows = Math.max(1, ...orderedTypes.map((key) => "
        "nodes.filter((node) => node.evidence_type === key).length));\n"
        "  const viewWidth = Math.max(920, orderedTypes.length * 220 + 80);\n"
        "  const viewHeight = Math.max(420, 110 + maxRows * 150);",
    ),
    ("const x = 35 + laneIndex * 170;", "const x = 45 + laneIndex * 220;"),
    ("x: String(x + 67),", "x: String(x + 90),"),
    (
        "positions.set(node.id, { x, y: 48 + rowIndex * 128, width: 134, height: 78 });",
        "positions.set(node.id, { x, y: 60 + rowIndex * 150, width: 180, height: 100 });",
    ),
    ("splitTitle(node.title, 12, 2)", "splitTitle(node.title, 16, 3)"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="path to MPP_Project checkout")
    args = parser.parse_args()
    app_js = Path(args.repo) / "web" / "app.js"
    if not app_js.is_file():
        raise SystemExit(f"app.js not found: {app_js}")
    text = app_js.read_text(encoding="utf-8")
    applied = []
    for old, new in REPLACEMENTS:
        if old not in text:
            if new in text:
                continue
            raise SystemExit(f"pattern not found (frontend version changed?): {old[:60]}")
        text = text.replace(old, new, 1)
        applied.append(old.split("\n")[0][:50])
    app_js.write_text(text, encoding="utf-8")
    print(f"patched {app_js}: {len(applied)} replacements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
