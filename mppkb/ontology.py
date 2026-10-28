"""Controlled vocabulary (ticket 04).

``ontology.yaml`` is the single source of truth and is append-only: terms may be
added, never renamed or removed, so claim slot identifiers stay stable and the
ledger can be replayed. Every command syncs the file into ``ontology_terms``.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .clock import now_iso
from .config import Config
from .db import ensure_databases, set_meta

KINDS = ("drug", "attribute", "population", "severity", "criterion")
KIND_PREFIX = {
    "drug": "DRUG",
    "attribute": "ATTR",
    "population": "POP",
    "severity": "SEV",
    "criterion": "CRIT",
}
ID_RE = re.compile(r"^(DRUG|ATTR|POP|SEV|CRIT):[A-Z0-9_]+$")
_PUNCT_RE = re.compile(r"[\s\u3000，。；：、（）()【】\[\]\"'“”‘’·…—\-/]+")


class OntologyError(Exception):
    """Raised when the vocabulary file is missing or invalid."""


@dataclass(frozen=True)
class Term:
    term_id: str
    kind: str
    label: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Ontology:
    version: int
    terms: tuple[Term, ...]

    def terms_of_kind(self, kind: str) -> list[Term]:
        return [term for term in self.terms if term.kind == kind]

    def map_text(self, text: Any, kind: str) -> str | None:
        """Map free text to a term id of ``kind`` using labels and aliases."""
        if text is None:
            return None
        if not isinstance(text, str):
            text = str(text)
        normalized = _norm_text(text)
        if not normalized:
            return None
        best_id: str | None = None
        best_len = 0
        for term in self.terms_of_kind(kind):
            for alias in (term.label, *term.aliases):
                needle = _norm_text(alias)
                if not needle:
                    continue
                if normalized == needle:
                    return term.term_id
                if len(needle) >= 2 and needle in normalized and len(needle) > best_len:
                    best_id = term.term_id
                    best_len = len(needle)
        return best_id


def _norm_text(value: str) -> str:
    return _PUNCT_RE.sub("", value.lower())


def load_ontology(path: str | Path) -> Ontology:
    file_path = Path(path)
    if not file_path.is_file():
        raise OntologyError(f"ontology file not found: {file_path}")
    try:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise OntologyError(f"cannot read ontology file: {exc}") from exc
    if not isinstance(data, dict):
        raise OntologyError("ontology file must contain a mapping with version and terms")
    version = data.get("version")
    if not isinstance(version, int) or version < 1:
        raise OntologyError("ontology version must be a positive integer")
    raw_terms = data.get("terms")
    if not isinstance(raw_terms, list):
        raise OntologyError("ontology terms must be a list")

    terms: list[Term] = []
    seen: set[str] = set()
    for entry in raw_terms:
        if not isinstance(entry, dict):
            raise OntologyError("each term must be a mapping")
        term_id = entry.get("id")
        kind = entry.get("kind")
        label = entry.get("label")
        aliases = entry.get("aliases", [])
        if not isinstance(term_id, str) or not ID_RE.match(term_id):
            raise OntologyError(f"invalid term id: {term_id!r}")
        if kind not in KINDS:
            raise OntologyError(f"invalid term kind for {term_id}: {kind!r}")
        if not term_id.startswith(KIND_PREFIX[kind] + ":"):
            raise OntologyError(f"term id prefix does not match kind for {term_id}")
        if term_id in seen:
            raise OntologyError(f"duplicate term id: {term_id}")
        if not isinstance(label, str) or not label.strip():
            raise OntologyError(f"term {term_id} needs a non-empty label")
        if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
            raise OntologyError(f"term {term_id} aliases must be a list of strings")
        seen.add(term_id)
        terms.append(Term(term_id, kind, label, tuple(aliases)))

    return Ontology(version, tuple(terms))


def save_ontology(path: str | Path, ontology: Ontology) -> None:
    data = {
        "version": ontology.version,
        "terms": [
            {"id": term.term_id, "kind": term.kind, "label": term.label, "aliases": list(term.aliases)}
            for term in ontology.terms
        ],
    }
    Path(path).write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _append_term(path: str | Path, term: Term, new_version: int) -> None:
    """Append a term without rewriting (and thus losing) the file's comments."""
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    updated = re.sub(
        r"^version:\s*\d+\s*$",
        f"version: {new_version}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if updated == text:
        raise OntologyError("ontology file has no version line")
    block = yaml.safe_dump(
        [
            {
                "id": term.term_id,
                "kind": term.kind,
                "label": term.label,
                "aliases": list(term.aliases),
            }
        ],
        allow_unicode=True,
        sort_keys=False,
    )
    indented = "\n".join(("  " + line) if line else line for line in block.splitlines())
    file_path.write_text(updated.rstrip() + "\n" + indented + "\n", encoding="utf-8")


def sync(conn, ontology: Ontology, now: str | None = None) -> None:
    """Upsert the vocabulary into ``ontology_terms``, enforcing append-only."""
    now = now or now_iso()
    with conn:
        for term in ontology.terms:
            existing = conn.execute(
                "SELECT kind, label, aliases_json FROM ontology_terms WHERE term_id = ?",
                (term.term_id,),
            ).fetchone()
            aliases_json = json.dumps(list(term.aliases), ensure_ascii=False)
            if existing is not None:
                if (
                    existing["kind"] != term.kind
                    or existing["label"] != term.label
                    or existing["aliases_json"] != aliases_json
                ):
                    raise OntologyError(
                        f"ontology is append-only; term {term.term_id} would change"
                    )
                continue
            conn.execute(
                "INSERT INTO ontology_terms(term_id, kind, label, aliases_json, "
                "ontology_version, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (term.term_id, term.kind, term.label, aliases_json, ontology.version, now),
            )
        set_meta(conn, "ontology_version", ontology.version)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _fail(args: Any, message: str, code: int = 1) -> int:
    if getattr(args, "json", False):
        _emit({"ok": False, "error": message})
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def run(args: Any, cfg: Config) -> int:
    command = getattr(args, "ontology_command", "list")
    try:
        ontology = load_ontology(cfg.ontology)
    except OntologyError as exc:
        return _fail(args, str(exc))

    if command == "list":
        terms = [
            {"id": term.term_id, "kind": term.kind, "label": term.label, "aliases": list(term.aliases)}
            for term in ontology.terms
        ]
        if getattr(args, "json", False):
            _emit({"ok": True, "version": ontology.version, "terms": terms})
        else:
            print(f"version: {ontology.version}")
            for term in terms:
                print(f"{term['id']:<32} {term['kind']:<10} {term['label']}")
        return 0

    # add
    term_id = str(getattr(args, "term", "")).strip()
    kind = str(getattr(args, "kind", "")).strip()
    label = str(getattr(args, "label", "")).strip()
    if kind not in KINDS:
        return _fail(args, f"kind must be one of {', '.join(KINDS)}")
    if not ID_RE.match(term_id):
        return _fail(args, f"invalid term id: {term_id!r}")
    if not term_id.startswith(KIND_PREFIX[kind] + ":"):
        return _fail(args, f"term id must start with {KIND_PREFIX[kind]}:")
    if not label:
        return _fail(args, "label must be non-empty")
    if any(term.term_id == term_id for term in ontology.terms):
        return _fail(args, f"term already exists and the ontology is append-only: {term_id}")

    new_version = ontology.version + 1
    term = Term(term_id, kind, label, ())
    try:
        _append_term(cfg.ontology, term, new_version)
        updated = load_ontology(cfg.ontology)
        kb, _corpus = ensure_databases(cfg)
        sync(kb, updated)
    except OntologyError as exc:
        return _fail(args, str(exc))

    if getattr(args, "json", False):
        _emit({"ok": True, "term_id": term_id, "version": new_version})
    else:
        print(f"added {term_id} (version {new_version})")
    return 0
