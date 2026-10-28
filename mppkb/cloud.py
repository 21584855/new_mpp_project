"""Cloud model access: embeddings and rerank (ticket 30).

Bailian / DashScope compatible-mode endpoints are used through plain urllib so
no vendor SDK is required. Offline backends (``fake``, ``fail``) make the index
testable without network; the real backend is selected by configuration.
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import Config


class CloudError(Exception):
    """Raised when a cloud call is unavailable or fails."""


EMBEDDING_BATCH = 10


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    calls: int
    tokens: int
    model: str


def _post(url: str, payload: dict, key: str, timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        raise CloudError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudError(f"network error: {exc}") from exc


def _fake_vector(text: str, dim: int) -> list[float]:
    vector = [0.0] * dim
    for index in range(max(0, len(text) - 1)):
        gram = text[index : index + 2]
        digest = int(hashlib.sha256(gram.encode("utf-8")).hexdigest(), 16)
        vector[digest % dim] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def embed_texts(cfg: Config, texts: list[str]) -> EmbeddingResult:
    if not texts:
        return EmbeddingResult([], 0, 0, cfg.embedding_model)
    backend = cfg.embedding_backend
    if backend == "fake":
        return EmbeddingResult(
            [_fake_vector(text, 16) for text in texts], 1, 0, "fake-embedding"
        )
    if backend == "fail":
        raise CloudError("simulated embedding failure")
    if not cfg.api_key:
        raise CloudError("MPPKB_API_KEY / DASHSCOPE_API_KEY is not set")

    vectors: list[list[float]] = []
    tokens = 0
    calls = 0
    for start in range(0, len(texts), EMBEDDING_BATCH):
        batch = texts[start : start + EMBEDDING_BATCH]
        payload = {
            "model": cfg.embedding_model,
            "input": batch,
            "dimensions": cfg.embedding_dim,
        }
        data = _post(cfg.embedding_endpoint, payload, cfg.api_key, cfg.cloud_timeout)
        items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
        if len(items) != len(batch):
            raise CloudError(f"embedding returned {len(items)} vectors for {len(batch)} inputs")
        vectors.extend(item["embedding"] for item in items)
        tokens += int((data.get("usage") or {}).get("total_tokens") or 0)
        calls += 1
    return EmbeddingResult(vectors, calls, tokens, cfg.embedding_model)


def rerank(cfg: Config, query: str, documents: list[str], top_n: int | None = None) -> list[dict]:
    if not documents:
        return []
    backend = cfg.embedding_backend
    if backend == "fake":
        query_grams = {query[index : index + 2] for index in range(max(0, len(query) - 1))}
        scored = []
        for index, document in enumerate(documents):
            grams = {document[i : i + 2] for i in range(max(0, len(document) - 1))}
            score = len(query_grams & grams) / (len(query_grams) or 1)
            scored.append({"index": index, "relevance_score": round(score, 6)})
        scored.sort(key=lambda item: (-item["relevance_score"], item["index"]))
        return scored[: top_n or len(scored)]
    if backend == "fail":
        raise CloudError("simulated rerank failure")
    if not cfg.api_key:
        raise CloudError("MPPKB_API_KEY / DASHSCOPE_API_KEY is not set")

    payload = {
        "model": cfg.rerank_model,
        "input": {"query": query, "documents": documents},
        "parameters": {"return_documents": False, "top_n": top_n or len(documents)},
    }
    data = _post(cfg.rerank_endpoint, payload, cfg.api_key, cfg.cloud_timeout)
    results = ((data.get("output") or {}).get("results")) or []
    return [
        {"index": int(item["index"]), "relevance_score": float(item["relevance_score"])}
        for item in results
    ]
