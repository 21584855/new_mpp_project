"""Ticket 33 acceptance: hybrid claims retrieval with degradation."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from support import run_cli

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINI = FIXTURES / "mini_two_pages.pdf"


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    values = {
        "MPPKB_KB_DB": str(tmp_path / "data" / "kb.sqlite"),
        "MPPKB_CORPUS_DB": str(tmp_path / "data" / "corpus.sqlite"),
        "MPPKB_CORPUS_DIR": str(corpus),
        "MPPKB_ONTOLOGY": str(REPO / "ontology.yaml"),
        "MPPKB_REVIEWER": "retrieval-tester",
        "MPPKB_EMBEDDING_BACKEND": "fake",
        "MPPKB_CHROMA_DIR": str(tmp_path / "data" / "chroma"),
        "ANONYMIZED_TELEMETRY": "False",
    }
    values.update(extra)
    return values


def inspect_pdf(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "inspect", str(MINI), "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def setup(tmp_path: Path, env: dict[str, str]) -> None:
    info = inspect_pdf(tmp_path, env)
    section = info["sections"][0]
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
    }
    replay = tmp_path / "replay.json"
    replay.write_text(
        json.dumps({"__default__": [], section["chunk_hash"]: [payload]}), encoding="utf-8"
    )
    ingest = run_cli(
        tmp_path, "ingest", str(MINI), "--replay-file", str(replay), "--json", env_extra=env
    )
    assert ingest.returncode == 0, ingest.stdout + ingest.stderr
    remap = run_cli(tmp_path, "remap", "--json", env_extra=env)
    assert remap.returncode == 0, remap.stderr


def ask(tmp_path: Path, env: dict[str, str], query: str) -> dict:
    result = run_cli(tmp_path, "answer", "--query", query, "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_lexical_is_default(tmp_path):
    env = env_for(tmp_path)
    setup(tmp_path, env)
    payload = ask(tmp_path, env, "阿奇霉素 一线治疗 儿童")
    assert payload["retrieval"]["mode"] == "lexical"
    assert payload["retrieval"]["degraded"] is False
    assert payload["citations"]


def test_hybrid_uses_vector_index(tmp_path):
    env = env_for(tmp_path)
    setup(tmp_path, env)
    built = run_cli(tmp_path, "index", "rebuild", "--json", env_extra=env)
    assert built.returncode == 0, built.stdout + built.stderr

    hybrid_env = {**env, "MPPKB_RETRIEVAL_MODE": "hybrid"}
    payload = ask(tmp_path, hybrid_env, "阿奇霉素 一线治疗 儿童")
    assert payload["retrieval"]["mode"] == "hybrid"
    assert payload["retrieval"]["degraded"] is False
    assert payload["citations"]
    assert "阿奇霉素" in payload["sections"][0]["text"]

    again = ask(tmp_path, hybrid_env, "阿奇霉素 一线治疗 儿童")
    assert again["claims"] == payload["claims"]
    assert again["retrieval"]["mode"] == "hybrid"


def test_hybrid_degrades_when_embedding_fails(tmp_path):
    env = env_for(tmp_path)
    setup(tmp_path, env)
    built = run_cli(tmp_path, "index", "rebuild", "--json", env_extra=env)
    assert built.returncode == 0, built.stdout + built.stderr

    failing = {
        **env,
        "MPPKB_RETRIEVAL_MODE": "hybrid",
        "MPPKB_EMBEDDING_BACKEND": "fail",
    }
    payload = ask(tmp_path, failing, "阿奇霉素 一线治疗 儿童")
    assert payload["retrieval"]["mode"] == "hybrid"
    assert payload["retrieval"]["degraded"] is True
    assert payload["retrieval"]["reasons"]
    assert payload["citations"]
    assert "阿奇霉素" in payload["sections"][0]["text"]


def test_embedding_batches_are_capped_at_ten(monkeypatch):
    from mppkb import cloud

    cfg = SimpleNamespace(
        embedding_backend="http",
        api_key="test",
        embedding_model="text-embedding-v4",
        embedding_dim=1024,
        embedding_endpoint="http://example.invalid",
        cloud_timeout=1,
    )
    sizes: list[int] = []

    def fake_post(endpoint, payload, key, timeout):
        sizes.append(len(payload["input"]))
        return {
            "data": [{"index": index, "embedding": [0.0]} for index in range(len(payload["input"]))],
            "usage": {"total_tokens": 1},
        }

    monkeypatch.setattr(cloud, "_post", fake_post)
    result = cloud.embed_texts(cfg, [f"text-{index}" for index in range(25)])
    assert sizes == [10, 10, 5]
    assert len(result.vectors) == 25
