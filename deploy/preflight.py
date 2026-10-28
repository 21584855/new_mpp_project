"""Preflight checks for a mppkb UI bridge deployment.

Usage:
    python deploy/preflight.py --repo /path/to/MPP_Project --data /path/to/data
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import socket
import sqlite3
import sys
from pathlib import Path


def check_python() -> dict:
    ok = sys.version_info >= (3, 11)
    return {"name": "python", "ok": ok, "detail": platform_version()}


def platform_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def check_modules() -> dict:
    modules = ["mppkb", "langgraph", "pymupdf", "yaml"]
    missing = []
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - report any import problem
            missing.append(f"{module}: {exc}")
    return {"name": "python_modules", "ok": not missing, "detail": missing or "ok"}


def check_ui_repo(repo: Path) -> dict:
    targets = [repo / "web" / "index.html", repo / "web" / "app.js"]
    missing = [str(path) for path in targets if not path.is_file()]
    return {"name": "ui_repo", "ok": not missing, "detail": missing or str(repo)}


def check_ontology(path: Path) -> dict:
    return {"name": "ontology", "ok": path.is_file(), "detail": str(path)}


def check_database(path: Path) -> dict:
    if not path.exists():
        return {"name": "database", "ok": True, "detail": f"{path} (will be created)"}
    try:
        connection = sqlite3.connect(path)
        result = connection.execute("PRAGMA quick_check").fetchone()[0]
        connection.close()
        return {"name": "database", "ok": result == "ok", "detail": f"{path}: {result}"}
    except Exception as exc:  # noqa: BLE001 - report sqlite failures
        return {"name": "database", "ok": False, "detail": f"{path}: {exc}"}


def check_writable(data_dir: Path) -> dict:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return {"name": "data_writable", "ok": True, "detail": str(data_dir)}
    except Exception as exc:  # noqa: BLE001 - report permission errors
        return {"name": "data_writable", "ok": False, "detail": str(exc)}


def check_port(host: str, port: int) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return {"name": "port", "ok": True, "detail": f"{host}:{port} available"}
    except OSError as exc:
        return {"name": "port", "ok": False, "detail": f"{host}:{port} {exc}"}
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    data = Path(args.data).resolve()
    ontology = Path(os.environ.get("MPPKB_ONTOLOGY", "ontology.yaml"))
    if not ontology.is_absolute():
        ontology = (Path(__file__).resolve().parents[1] / ontology).resolve()

    checks = [
        check_python(),
        check_modules(),
        check_ui_repo(repo),
        check_ontology(ontology),
        check_writable(data),
        check_database(Path(os.environ.get("MPPKB_KB_DB", data / "kb.sqlite"))),
        check_database(Path(os.environ.get("MPPKB_CORPUS_DB", data / "corpus.sqlite"))),
        check_port(args.host, args.port),
    ]
    payload = {
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
