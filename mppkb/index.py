"""Derived vector index over claims (ticket 30).

Chroma is only a disposable index: documents are rebuilt from SQLite, embeddings
come from the cloud (Bailian/DashScope), and a failed cloud call degrades to a
deterministic 2-gram scan instead of failing the answer path.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from .cloud import CloudError, embed_texts, rerank
from .config import Config
from .db import ensure_databases, get_meta, library_version, set_meta
from .ontology import load_ontology
from .text import to_grams

DIRECTION_LABELS = {"first_line": "首选", "alternative": "备选", "avoid": "避免"}
COLD_STATUSES = ("approved",)
HOT_STATUSES = ("proposed", "conflicted")


def _labels(cfg: Config) -> dict[str, str]:
    try:
        ontology = load_ontology(cfg.ontology)
    except Exception:  # noqa: BLE001 - labels are optional, ids still work
        return {}
    return {term.term_id: term.label for term in ontology.terms}


def _value_text(value: dict[str, Any], direction: str | None) -> str:
    if direction:
        return DIRECTION_LABELS.get(direction, direction)
    kind = value.get("kind")
    if kind == "numeric":
        return f"{value.get('value')}{value.get('unit') or ''}"
    if kind == "range":
        return f"{value.get('low')}–{value.get('high')}{value.get('unit') or ''}"
    if kind == "direction":
        return DIRECTION_LABELS.get(str(value.get("value")), str(value.get("value")))
    if kind == "text":
        return str(value.get("value") or "")
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _documents(kb, cfg: Config) -> list[dict[str, Any]]:
    labels = _labels(cfg)
    rows = kb.execute(
        "SELECT v.version_id, v.claim_id, v.status, v.evidence_type, v.value_json, "
        "v.direction, v.anchors_json, v.effective_date, c.population, c.severity, "
        "c.intervention, c.attribute FROM claim_versions v "
        "JOIN claims c ON c.claim_id = v.claim_id ORDER BY v.version_id"
    ).fetchall()
    documents: list[dict[str, Any]] = []
    for row in rows:
        status = row["status"]
        if status in COLD_STATUSES:
            library = "cold"
        elif status in HOT_STATUSES:
            library = "hot"
        else:
            continue
        value = json.loads(row["value_json"])
        anchors = json.loads(row["anchors_json"])
        quotes = " ".join(str(anchor.get("quote") or "") for anchor in anchors[:3])
        text = " ".join(
            [
                labels.get(row["population"], row["population"] or ""),
                labels.get(row["severity"], row["severity"] or ""),
                labels.get(row["intervention"], row["intervention"] or ""),
                labels.get(row["attribute"], row["attribute"] or ""),
                _value_text(value, row["direction"]),
                quotes,
            ]
        ).strip()
        documents.append(
            {
                "version_id": row["version_id"],
                "claim_id": row["claim_id"],
                "status": status,
                "library": library,
                "text": text,
                "evidence_type": row["evidence_type"],
                "effective_date": row["effective_date"],
            }
        )
    return documents


def _corpus_hash(documents: list[dict[str, Any]]) -> str:
    raw = "|".join(f"{item['version_id']}:{item['text']}" for item in documents)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _client(cfg: Config):
    try:
        import chromadb
    except ImportError as exc:  # pragma: no cover - dependency is installed in dev
        raise CloudError("chromadb is not installed") from exc
    try:
        from chromadb.config import Settings

        return chromadb.PersistentClient(
            path=str(Path(cfg.chroma_dir)),
            settings=Settings(anonymized_telemetry=False),
        )
    except Exception:  # noqa: BLE001 - older/newer settings signature
        return chromadb.PersistentClient(path=str(Path(cfg.chroma_dir)))


def _collection_name(library: str) -> str:
    return f"mpp_claims_{library}"


def rebuild(kb, cfg: Config) -> dict[str, Any]:
    documents = _documents(kb, cfg)
    client = _client(cfg)
    counts: dict[str, int] = {}
    calls = 0
    tokens = 0
    for library in ("cold", "hot"):
        subset = [item for item in documents if item["library"] == library]
        name = _collection_name(library)
        try:
            client.delete_collection(name)
        except Exception:  # noqa: BLE001 - collection may not exist yet
            pass
        if not subset:
            counts[library] = 0
            continue
        result = embed_texts(cfg, [item["text"] for item in subset])
        calls += result.calls
        tokens += result.tokens
        collection = client.create_collection(
            name=name,
            metadata={
                "hnsw:space": "cosine",
                "library_version": library_version(kb),
                "embedding_model": result.model,
            },
        )
        collection.add(
            ids=[item["version_id"] for item in subset],
            embeddings=result.vectors,
            documents=[item["text"] for item in subset],
            metadatas=[
                {
                    "claim_id": item["claim_id"],
                    "status": item["status"],
                    "evidence_type": item["evidence_type"] or "",
                    "library_version": library_version(kb),
                }
                for item in subset
            ],
        )
        counts[library] = len(subset)

    with kb:
        set_meta(kb, "index_corpus_hash", _corpus_hash(documents))
        set_meta(kb, "index_library_version", library_version(kb))
        set_meta(kb, "index_embedding_model", cfg.embedding_model)
        set_meta(kb, "index_counts", json.dumps(counts))
        set_meta(kb, "index_embedding_calls", calls)
        set_meta(kb, "index_embedding_tokens", tokens)
    return {
        "ok": True,
        "counts": counts,
        "embedding_calls": calls,
        "embedding_tokens": tokens,
        "model": cfg.embedding_model,
        "corpus_hash": _corpus_hash(documents),
        "chroma_dir": cfg.chroma_dir,
    }


def _fallback(
    kb, cfg: Config, query: str, library: str, top_k: int, reason: str
) -> dict[str, Any]:
    query_grams = set(to_grams(query).split())
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in _documents(kb, cfg):
        if item["library"] != library:
            continue
        overlap = len(query_grams & set(to_grams(item["text"]).split()))
        if overlap:
            scored.append((overlap, item))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["version_id"]))
    return {
        "ok": True,
        "library": library,
        "degraded": True,
        "reason": reason,
        "items": [
            {
                "claim_id": item["claim_id"],
                "version_id": item["version_id"],
                "status": item["status"],
                "text": item["text"],
                "vector_score": None,
                "rerank_score": None,
            }
            for _score, item in scored[:top_k]
        ],
    }


def search(kb, cfg: Config, query: str, library: str, top_k: int = 5) -> dict[str, Any]:
    try:
        result = embed_texts(cfg, [query])
        client = _client(cfg)
        collection = client.get_collection(_collection_name(library))
        total = collection.count()
        if total == 0:
            raise CloudError("index is empty")
        raw = collection.query(
            query_embeddings=[result.vectors[0]],
            n_results=min(top_k * 3, total),
            include=["documents", "metadatas", "distances"],
        )
    except CloudError as exc:
        return _fallback(kb, cfg, query, library, top_k, str(exc))
    except Exception as exc:  # noqa: BLE001 - chroma or transport failure
        return _fallback(kb, cfg, query, library, top_k, f"index unavailable: {exc}")

    items: list[dict[str, Any]] = []
    for index, version_id in enumerate(raw["ids"][0]):
        metadata = (raw["metadatas"][0][index] or {})
        distance = float(raw["distances"][0][index])
        items.append(
            {
                "claim_id": metadata.get("claim_id"),
                "version_id": version_id,
                "status": metadata.get("status"),
                "text": raw["documents"][0][index],
                "vector_score": round(1.0 - distance, 6),
                "rerank_score": None,
            }
        )

    rerank_degraded = False
    if len(items) > 1:
        try:
            ranked = rerank(cfg, query, [item["text"] for item in items], top_n=top_k)
            scores = {int(item["index"]): float(item["relevance_score"]) for item in ranked}
            for index, item in enumerate(items):
                item["rerank_score"] = scores.get(index)
            items.sort(key=lambda item: -(item["rerank_score"] if item["rerank_score"] is not None else -1))
        except CloudError:
            rerank_degraded = True

    return {
        "ok": True,
        "library": library,
        "degraded": False,
        "rerank_degraded": rerank_degraded,
        "items": items[:top_k],
    }


def status(kb, cfg: Config) -> dict[str, Any]:
    documents = _documents(kb, cfg)
    current_hash = _corpus_hash(documents)
    stored_hash = get_meta(kb, "index_corpus_hash")
    counts: dict[str, int] = {}
    try:
        client = _client(cfg)
        for library in ("cold", "hot"):
            try:
                counts[library] = client.get_collection(_collection_name(library)).count()
            except Exception:  # noqa: BLE001 - missing collection
                counts[library] = 0
    except CloudError:
        counts = {"cold": 0, "hot": 0}
    return {
        "ok": True,
        "model": get_meta(kb, "index_embedding_model") or cfg.embedding_model,
        "library_version": library_version(kb),
        "indexed_library_version": int(get_meta(kb, "index_library_version") or 0),
        "corpus_hash": current_hash,
        "indexed_hash": stored_hash,
        "stale": stored_hash != current_hash,
        "counts_indexed": counts,
        "counts_current": {
            "cold": sum(1 for item in documents if item["library"] == "cold"),
            "hot": sum(1 for item in documents if item["library"] == "hot"),
        },
    }


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _fail(args: Any, message: str, code: int = 1) -> int:
    if getattr(args, "json", False):
        _emit({"ok": False, "error": message})
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def run(args: Any, cfg: Config) -> int:
    kb, _corpus = ensure_databases(cfg)
    command = getattr(args, "index_command", "status")
    try:
        if command == "rebuild":
            payload = rebuild(kb, cfg)
        elif command == "search":
            payload = search(
                kb,
                cfg,
                getattr(args, "query", ""),
                getattr(args, "library", "cold"),
                int(getattr(args, "top_k", 5)),
            )
        elif command == "status":
            payload = status(kb, cfg)
        else:
            return _fail(args, f"unknown index command: {command}")
    except CloudError as exc:
        return _fail(args, str(exc))

    if getattr(args, "json", False):
        _emit(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0
