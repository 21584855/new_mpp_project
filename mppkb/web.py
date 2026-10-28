"""Review console and answer API (ticket 08).

Standard library only. The console is a small HTML page backed by JSON
endpoints; the same decision service the CLI uses is reused here so the two
surfaces cannot drift.
"""

from __future__ import annotations

import json
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .answer import answer
from .config import Config
from .db import ensure_databases
from .promote import decide, impact_set, list_queue
from .todos import collect as collect_todos

INDEX_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>MPP 知识审核台</title>
<style>
body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}
.card{border:1px solid #ddd;border-radius:8px;padding:.75rem;margin:.5rem 0}
button{margin-right:.4rem}
small{color:#666}
</style></head><body>
<h1>MPP 知识审核台</h1>
<section><h2>待审队列</h2><div id="queue">加载中…</div></section>
<section><h2>待办总览</h2><pre id="todos">加载中…</pre></section>
<section><h2>提问</h2>
<input id="q" size="60" placeholder="例如：儿童支原体肺炎首选什么药">
<button onclick="ask()">提问</button>
<pre id="answer"></pre></section>
<section><h2>审计</h2><button onclick="audit()">刷新审计</button><pre id="audit"></pre></section>
<script>
async function load(){
  const d = await (await fetch('/api/queue')).json();
  const el = document.getElementById('queue');
  el.innerHTML = '';
  if(!d.items.length){ el.textContent = '队列为空'; return; }
  for (const it of d.items) {
    const card = document.createElement('div'); card.className = 'card';
    const anchors = (it.anchors||[]).map(a => a.doc_id + ':' + a.page_start).join(', ');
    card.innerHTML = '<b>' + it.claim_id + '</b> ' + it.status + '<br>' +
      it.slot.population + ' | ' + it.slot.intervention + ' | ' + it.slot.attribute + '<br>' +
      JSON.stringify(it.value) + '<br><small>' + anchors + '</small><br>';
    for (const action of ['approve','reject','hold']) {
      const b = document.createElement('button'); b.textContent = action;
      b.onclick = () => decide(it, action); card.appendChild(b);
    }
    el.appendChild(card);
  }
}
async function decide(it, action){
  const body = {claim_id: it.claim_id, version_id: it.version_id, action: action};
  if (action === 'reject') {
    body.reason_code = prompt('原因码: insufficient_evidence / extraction_error / term_mismatch / duplicate / outdated');
    if (!body.reason_code) return;
  }
  const r = await fetch('/api/decide', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if (!r.ok) { alert((await r.json()).error || 'failed'); return; }
  load();
}
async function ask(){
  const query = document.getElementById('q').value;
  const r = await fetch('/api/answer', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({query})});
  const d = await r.json();
  const lines = [];
  if (d.hot_updates && d.hot_updates.length) {
    lines.push('【存在待审核更新】');
    for (const u of d.hot_updates) {
      lines.push('- ' + u.claim_id + ': ' + (u.diff.old_text || '无') + ' → ' + u.diff.new_text + '（影响 ' + u.impact.length + ' 条）');
    }
    lines.push('');
  }
  if (d.recommendation_suppressed) lines.push('【P0 安全例外已覆盖当前推荐】');
  lines.push(JSON.stringify(d, null, 2));
  document.getElementById('answer').textContent = lines.join('\n');
}
async function audit(){
  const d = await (await fetch('/api/audit')).json();
  document.getElementById('audit').textContent = JSON.stringify(d.items, null, 2);
}
async function todos(){
  const d = await (await fetch('/api/todos')).json();
  document.getElementById('todos').textContent =
    'KB 待审: ' + d.kb_queue + '    P0 补审: ' + d.p0_queue + '    病例 Gate: ' + d.case_gate + '    合计: ' + d.total;
}
load();
todos();
</script></body></html>
"""


def _version_json(row) -> dict:
    return {
        "version_id": row["version_id"],
        "status": row["status"],
        "value": json.loads(row["value_json"]),
        "direction": row["direction"],
        "evidence_type": row["evidence_type"],
        "provenance": row["provenance"],
        "anchors": json.loads(row["anchors_json"]),
        "created_at": row["created_at"],
        "decided_by": row["decided_by"],
    }


def _claim_card(kb, claim_id: str) -> dict | None:
    rows = kb.execute(
        "SELECT v.*, c.population, c.severity, c.intervention, c.attribute "
        "FROM claim_versions v JOIN claims c ON c.claim_id = v.claim_id "
        "WHERE v.claim_id = ? ORDER BY v.created_at, v.version_id",
        (claim_id,),
    ).fetchall()
    if not rows:
        return None
    first = rows[0]
    current = next((row for row in rows if row["status"] == "approved"), None)
    pending = [row for row in rows if row["status"] in ("proposed", "conflicted", "held", "unsupported")]
    card_impact = []
    target = pending[0] if pending else current
    if target is not None:
        try:
            card_impact = impact_set(kb, claim_id, target["version_id"], "approve")["changes"]
        except LookupError:
            card_impact = []
    return {
        "ok": True,
        "claim_id": claim_id,
        "slot": {
            "population": first["population"],
            "severity": first["severity"],
            "intervention": first["intervention"],
            "attribute": first["attribute"],
        },
        "current": _version_json(current) if current is not None else None,
        "candidates": [_version_json(row) for row in pending],
        "impact": card_impact,
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "mppkb/0.1"

    def __init__(self, request, client_address, server, cfg: Config):
        self.cfg = cfg
        super().__init__(request, client_address, server)

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        parsed = urlparse(self.path)
        path = parsed.path
        kb, _corpus = ensure_databases(self.cfg)

        if path == "/":
            self._send_html(INDEX_HTML)
            return
        if path == "/api/queue":
            doc = parse_qs(parsed.query).get("doc", [None])[0]
            items = list_queue(kb)
            if doc:
                items = [
                    item
                    for item in items
                    if any(anchor.get("doc_id") == doc for anchor in item.get("anchors", []))
                ]
            self._send_json(200, {"ok": True, "items": items})
            return
        if path == "/api/audit":
            rows = kb.execute(
                "SELECT seq, action, actor, detail_json, created_at FROM kb_audit "
                "ORDER BY seq DESC LIMIT 200"
            ).fetchall()
            items = [
                {
                    "seq": row["seq"],
                    "action": row["action"],
                    "actor": row["actor"],
                    "detail": json.loads(row["detail_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
            self._send_json(200, {"ok": True, "items": items})
            return
        if path == "/api/todos":
            self._send_json(200, collect_todos(kb))
            return
        if path.startswith("/api/claims/"):
            claim_id = path.rsplit("/", 1)[-1]
            card = _claim_card(kb, claim_id)
            if card is None:
                self._send_json(404, {"ok": False, "error": f"claim not found: {claim_id}"})
                return
            self._send_json(200, card)
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        path = urlparse(self.path).path
        kb, _corpus = ensure_databases(self.cfg)
        try:
            body = self._read_json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "error": "invalid JSON body"})
            return

        if path == "/api/decide":
            action = body.get("action")
            claim_id = body.get("claim_id")
            if action not in ("approve", "reject", "hold") or not claim_id:
                self._send_json(400, {"ok": False, "error": "action and claim_id are required"})
                return
            try:
                payload = decide(
                    kb,
                    self.cfg,
                    action,
                    claim_id,
                    body.get("version_id"),
                    body.get("reason_code"),
                    body.get("note"),
                )
            except LookupError as exc:
                self._send_json(404, {"ok": False, "error": str(exc)})
                return
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, payload)
            return

        if path == "/api/answer":
            query = body.get("query", "")
            if not query:
                self._send_json(400, {"ok": False, "error": "query is required"})
                return
            result = answer(kb, self.cfg, query, body.get("case_id"), body.get("context"))
            self._send_json(200, result)
            return

        self._send_json(404, {"ok": False, "error": "not found"})


def run(args, cfg: Config) -> int:
    ensure_databases(cfg)
    host = getattr(args, "host", None) or "127.0.0.1"
    port = getattr(args, "port", None) or cfg.port
    server = ThreadingHTTPServer((host, port), partial(_Handler, cfg=cfg))
    print(f"serving on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
