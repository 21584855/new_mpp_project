"""Ticket 08 acceptance: review console HTTP API and persistence."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from support import REPO_ROOT, run_cli

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINI = FIXTURES / "mini_two_pages.pdf"


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    values = {
        "MPPKB_KB_DB": str(tmp_path / "data" / "kb.sqlite"),
        "MPPKB_CORPUS_DB": str(tmp_path / "data" / "corpus.sqlite"),
        "MPPKB_CORPUS_DIR": str(corpus),
        "MPPKB_ONTOLOGY": str(REPO_ROOT / "ontology.yaml"),
        "MPPKB_REVIEWER": "web-tester",
    }
    values.update(extra)
    return values


def inspect_pdf(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def make_payload(info: dict, section: dict, **overrides) -> dict:
    payload = {
        "population": "儿童",
        "severity": None,
        "intervention": "阿奇霉素",
        "attribute": "一线治疗",
        "value": {"kind": "direction", "value": "first_line"},
        "direction": "first_line",
        "evidence_type": "guideline",
        "anchors": [
            {
                "doc_id": info["sha256"][:16],
                "page_start": section["page_start"],
                "page_end": section["page_end"],
                "quote": section["text"].strip()[:24],
            }
        ],
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


def prepare(tmp_path: Path, env: dict[str, str]) -> None:
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
    replay = tmp_path / "replay.json"
    replay.write_text(
        json.dumps(
            {
                "__default__": [],
                section["chunk_hash"]: [
                    make_payload(info, section),
                    make_payload(
                        info,
                        section,
                        intervention="红霉素",
                        attribute="剂量",
                        value={"kind": "numeric", "value": 10, "unit": "mg/kg/d"},
                        direction=None,
                        evidence_type="observational_study",
                    ),
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = run_cli(
        tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    remap = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert remap.returncode == 0, remap.stderr


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_server(tmp_path: Path, env: dict[str, str], port: int) -> subprocess.Popen:
    process_env = {key: value for key, value in os.environ.items() if not key.startswith("MPPKB_")}
    process_env["PYTHONPATH"] = str(REPO_ROOT)
    process_env.update(env)
    process = subprocess.Popen(
        [sys.executable, "-m", "mppkb", "serve", "--port", str(port)],
        cwd=str(tmp_path),
        env=process_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/queue", timeout=1) as response:
                if response.status == 200:
                    return process
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.2)
    process.terminate()
    raise AssertionError("server did not start")


def request(port: int, path: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            body = response.read().decode("utf-8")
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_console_flow_and_persistence(tmp_path):
    env = env_for(tmp_path)
    prepare(tmp_path, env)
    port = free_port()
    process = start_server(tmp_path, env, port)
    try:
        status, body = request(port, "/")
        assert status == 200 and "审核" in body

        status, body = request(port, "/api/queue")
        assert status == 200
        queue = json.loads(body)["items"]
        assert len(queue) == 1
        item = queue[0]

        status, body = request(port, f"/api/claims/{item['claim_id']}")
        card = json.loads(body)
        assert status == 200
        assert len(card["candidates"]) == 1

        approved = json.loads(
            run_cli(tmp_path, "claims", "list", "--status", "approved", "--json", env_extra=env).stdout
        )["claims"]
        status, body = request(port, f"/api/claims/{approved[0]['claim_id']}")
        card = json.loads(body)
        assert status == 200
        assert card["current"] is not None

        status, body = request(port, "/api/decide", "POST", {"claim_id": item["claim_id"], "action": "reject"})
        assert status == 400
        assert "reason" in json.loads(body)["error"]

        status, body = request(
            port,
            "/api/decide",
            "POST",
            {
                "claim_id": item["claim_id"],
                "version_id": item["version_id"],
                "action": "reject",
                "reason_code": "outdated",
            },
        )
        assert status == 200, body

        status, body = request(port, "/api/queue")
        assert json.loads(body)["items"] == []

        status, body = request(port, "/api/audit")
        audit = json.loads(body)["items"]
        assert any(entry["action"] == "reject" for entry in audit)

        status, body = request(port, "/api/answer", "POST", {"query": "阿奇霉素 一线治疗 儿童"})
        assert status == 200
        answer = json.loads(body)
        assert answer["ok"] is True
        assert answer["sections"]

        status, body = request(port, "/api/queue?doc=nonexistent-paper")
        assert json.loads(body)["items"] == []
    finally:
        process.terminate()
        process.wait(timeout=10)

    restarted = start_server(tmp_path, env, port)
    try:
        status, body = request(port, "/api/audit")
        assert status == 200
        assert json.loads(body)["items"]
    finally:
        restarted.terminate()
        restarted.wait(timeout=10)
