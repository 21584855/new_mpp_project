"""SQLite access and schema initialisation.

Two databases:

* ``kb.sqlite`` — governance data (precious, backupable single file).
* ``corpus.sqlite`` — derived corpus index (disposable, rebuildable).

Table names and columns are fixed by ``docs/contracts.md``; later tickets must
not rename them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import Config

KB_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS ontology_terms (
  term_id          TEXT PRIMARY KEY,
  kind             TEXT NOT NULL CHECK (kind IN ('drug','attribute','population','severity','criterion')),
  label            TEXT NOT NULL,
  aliases_json     TEXT NOT NULL DEFAULT '[]',
  ontology_version INTEGER NOT NULL,
  created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
  claim_id     TEXT PRIMARY KEY,
  slot_key     TEXT NOT NULL UNIQUE,
  population   TEXT NOT NULL,
  severity     TEXT,
  intervention TEXT NOT NULL,
  attribute    TEXT NOT NULL,
  arm          TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_versions (
  version_id       TEXT PRIMARY KEY,
  claim_id         TEXT NOT NULL REFERENCES claims(claim_id),
  value_json       TEXT NOT NULL,
  direction        TEXT,
  status           TEXT NOT NULL,
  evidence_type    TEXT,
  provenance       TEXT NOT NULL,
  anchors_json     TEXT NOT NULL,
  ontology_version INTEGER NOT NULL,
  created_at       TEXT NOT NULL,
  decided_at       TEXT,
  decided_by       TEXT,
  reason_code      TEXT,
  review_note      TEXT,
  published_date   TEXT,
  effective_date   TEXT,
  retrieved_date   TEXT,
  supersedes       TEXT,
  superseded_by    TEXT
);

CREATE TABLE IF NOT EXISTS claim_events (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type   TEXT NOT NULL,
  claim_id     TEXT,
  version_id   TEXT,
  actor        TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  occurred_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_audit (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  event_seq   INTEGER,
  action      TEXT NOT NULL,
  actor       TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS p0_rules (
  rule_id       TEXT PRIMARY KEY,
  drug          TEXT NOT NULL,
  age_max_years INTEGER,
  condition     TEXT,
  template      TEXT NOT NULL,
  enabled       INTEGER NOT NULL DEFAULT 1,
  confirmed_by  TEXT,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_audit (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id         TEXT,
  library_version INTEGER,
  path            TEXT,
  decision        TEXT,
  actor           TEXT,
  detail_json     TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
  candidate_id TEXT PRIMARY KEY,
  paper_id     TEXT NOT NULL,
  chunk_hash   TEXT NOT NULL,
  section_path TEXT,
  payload_json TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'proposed',
  reason       TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_log (
  paper_id   TEXT NOT NULL,
  chunk_hash TEXT NOT NULL,
  extractor  TEXT NOT NULL,
  outcome    TEXT NOT NULL,
  detail     TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (paper_id, chunk_hash, extractor)
);

CREATE TABLE IF NOT EXISTS answer_cache (
  cache_key      TEXT PRIMARY KEY,
  library_version INTEGER NOT NULL,
  response_json  TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retrieval_cache (
  cache_key        TEXT PRIMARY KEY,
  cache_type       TEXT NOT NULL,
  value_json       TEXT NOT NULL,
  library_version  INTEGER NOT NULL,
  embedding_model  TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  expires_at       TEXT,
  hit_count        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS answer_log (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id         TEXT,
  question_hash   TEXT NOT NULL,
  question        TEXT,
  snapshot_id     TEXT,
  record_json     TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  parent_seq      INTEGER,
  superseded_by   INTEGER,
  auto_writeback  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kb_snapshots (
  snapshot_id    TEXT PRIMARY KEY,
  library        TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  effective_date TEXT,
  reviewer       TEXT,
  status         TEXT NOT NULL,
  claim_ids_json TEXT NOT NULL,
  eval_json      TEXT,
  note           TEXT
);

CREATE TABLE IF NOT EXISTS review_assignments (
  assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_id      TEXT NOT NULL,
  version_id    TEXT NOT NULL UNIQUE,
  reviewer      TEXT NOT NULL,
  assigned_by   TEXT,
  assigned_at   TEXT NOT NULL,
  note          TEXT
);

CREATE INDEX IF NOT EXISTS idx_versions_claim ON claim_versions(claim_id, status);
CREATE INDEX IF NOT EXISTS idx_events_claim  ON claim_events(claim_id);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_assign_reviewer ON review_assignments(reviewer);
"""

CORPUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS papers (
  paper_id      TEXT PRIMARY KEY,
  sha256        TEXT NOT NULL UNIQUE,
  path          TEXT NOT NULL,
  title         TEXT,
  year          INTEGER,
  publisher     TEXT,
  evidence_type TEXT,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
  chunk_id     TEXT PRIMARY KEY,
  paper_id     TEXT NOT NULL REFERENCES papers(paper_id),
  section_path TEXT,
  page_start   INTEGER NOT NULL,
  page_end     INTEGER NOT NULL,
  text         TEXT NOT NULL,
  token_count  INTEGER NOT NULL,
  chunk_hash   TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED, text, tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS relations (
  relation_id TEXT PRIMARY KEY,
  from_claim  TEXT NOT NULL,
  to_claim    TEXT NOT NULL,
  relation    TEXT NOT NULL,
  note        TEXT,
  created_at  TEXT NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_kb(conn: sqlite3.Connection) -> None:
    conn.executescript(KB_SCHEMA)
    _ensure_columns(
        conn,
        "claims",
        {"arm": "TEXT NOT NULL DEFAULT ''"},
    )
    _ensure_columns(
        conn,
        "claim_versions",
        {
            "published_date": "TEXT",
            "effective_date": "TEXT",
            "retrieved_date": "TEXT",
            "supersedes": "TEXT",
            "superseded_by": "TEXT",
            "review_note": "TEXT",
        },
    )
    _ensure_columns(
        conn,
        "answer_log",
        {
            "question": "TEXT",
            "parent_seq": "INTEGER",
            "superseded_by": "INTEGER",
            "auto_writeback": "INTEGER NOT NULL DEFAULT 0",
        },
    )
    for key, value in (
        ("schema_version", "3"),
        ("library_version", "0"),
        ("ontology_version", "0"),
    ):
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)", (key, value))
    conn.commit()


def init_corpus(conn: sqlite3.Connection) -> None:
    conn.executescript(CORPUS_SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')")
    conn.commit()


def ensure_databases(cfg: Config) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    kb = connect(cfg.kb_db)
    init_kb(kb)
    corpus = connect(cfg.corpus_db)
    init_corpus(corpus)
    return kb, corpus


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_meta(conn: sqlite3.Connection, key: str, value: str | int) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def library_version(conn: sqlite3.Connection) -> int:
    return int(get_meta(conn, "library_version", "0") or 0)


def ontology_version(conn: sqlite3.Connection) -> int:
    return int(get_meta(conn, "ontology_version", "0") or 0)
