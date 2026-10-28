"""Configuration resolution and validation.

Precedence: CLI overrides > environment variables > ``mppkb.json`` (cwd) > defaults.
Invalid values fail fast with :class:`ConfigError`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


DEFAULTS: dict[str, Any] = {
    "kb_db": "data/kb.sqlite",
    "corpus_db": "data/corpus.sqlite",
    "corpus_dir": r"D:\MPP_Project\MPP",
    "legacy_dir": r"D:\MPP_Project",
    "migration_anchors": ".scratch/mpp-kb/legacy/migration_anchors.json",
    "ttl_days": 90,
    "batch_cap": 200,
    "sample_rate": 0.1,
    "evidence_min": "guideline,systematic_review,randomized_controlled_trial",
    "reviewer": "local-reviewer",
    "port": 8765,
    "ontology": "ontology.yaml",
    "embedding_backend": "http",
    "embedding_endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
    "embedding_model": "text-embedding-v4",
    "embedding_dim": 1024,
    "rerank_endpoint": "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
    "rerank_model": "qwen3-rerank",
    "llm_endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "llm_model": "qwen-plus",
    "cloud_timeout": 30,
    "chroma_dir": "data/chroma",
}

ENV_VARS: dict[str, str] = {
    "kb_db": "MPPKB_KB_DB",
    "corpus_db": "MPPKB_CORPUS_DB",
    "corpus_dir": "MPPKB_CORPUS_DIR",
    "legacy_dir": "MPPKB_LEGACY",
    "migration_anchors": "MPPKB_MIGRATION_ANCHORS",
    "ttl_days": "MPPKB_TTL_DAYS",
    "batch_cap": "MPPKB_BATCH_CAP",
    "sample_rate": "MPPKB_SAMPLE_RATE",
    "evidence_min": "MPPKB_EVIDENCE_MIN",
    "reviewer": "MPPKB_REVIEWER",
    "port": "MPPKB_PORT",
    "ontology": "MPPKB_ONTOLOGY",
    "embedding_backend": "MPPKB_EMBEDDING_BACKEND",
    "embedding_endpoint": "MPPKB_EMBEDDING_ENDPOINT",
    "embedding_model": "MPPKB_EMBEDDING_MODEL",
    "embedding_dim": "MPPKB_EMBEDDING_DIM",
    "rerank_endpoint": "MPPKB_RERANK_ENDPOINT",
    "rerank_model": "MPPKB_RERANK_MODEL",
    "llm_endpoint": "MPPKB_LLM_ENDPOINT",
    "llm_model": "MPPKB_LLM_MODEL",
    "cloud_timeout": "MPPKB_CLOUD_TIMEOUT",
    "chroma_dir": "MPPKB_CHROMA_DIR",
}

_INT_KEYS = ("ttl_days", "batch_cap", "port", "embedding_dim", "cloud_timeout")
_FLOAT_KEYS = ("sample_rate",)
_STR_KEYS = (
    "kb_db",
    "corpus_db",
    "corpus_dir",
    "legacy_dir",
    "migration_anchors",
    "evidence_min",
    "reviewer",
    "ontology",
    "embedding_backend",
    "embedding_endpoint",
    "embedding_model",
    "rerank_endpoint",
    "rerank_model",
    "llm_endpoint",
    "llm_model",
    "chroma_dir",
)

CONFIG_FILE = "mppkb.json"


@dataclass(frozen=True)
class Config:
    kb_db: str
    corpus_db: str
    corpus_dir: str
    legacy_dir: str
    migration_anchors: str
    ttl_days: int
    batch_cap: int
    sample_rate: float
    evidence_min: tuple[str, ...]
    reviewer: str
    port: int
    ontology: str
    api_key: str
    embedding_backend: str
    embedding_endpoint: str
    embedding_model: str
    embedding_dim: int
    rerank_endpoint: str
    rerank_model: str
    llm_endpoint: str
    llm_model: str
    cloud_timeout: int
    chroma_dir: str


def _coerce(key: str, value: Any) -> Any:
    if key in _INT_KEYS:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{key} must be an integer, got {value!r}") from exc
    if key in _FLOAT_KEYS:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{key} must be a number, got {value!r}") from exc
    if key in _STR_KEYS:
        return str(value)
    raise ConfigError(f"unknown configuration key: {key}")


def _read_file(cwd: Path) -> dict[str, Any]:
    path = cwd / CONFIG_FILE
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read {CONFIG_FILE}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_FILE} must contain a JSON object")
    return {key: _coerce(key, value) for key, value in raw.items() if key in DEFAULTS}


def _read_env() -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, var in ENV_VARS.items():
        raw = os.environ.get(var)
        if raw is not None and raw.strip() != "":
            values[key] = _coerce(key, raw)
    return values


def load_config(
    cli_overrides: Mapping[str, Any] | None = None,
    cwd: Path | str | None = None,
) -> Config:
    base = dict(DEFAULTS)
    base.update(_read_file(Path(cwd or Path.cwd())))
    base.update(_read_env())
    base["api_key"] = (
        os.environ.get("MPPKB_API_KEY") or os.environ.get("DASHSCOPE_API_KEY") or ""
    )
    for key, value in (cli_overrides or {}).items():
        if value is not None:
            if key not in DEFAULTS:
                raise ConfigError(f"unknown configuration key: {key}")
            base[key] = _coerce(key, value)

    if base["ttl_days"] <= 0:
        raise ConfigError("ttl_days must be a positive integer")
    if base["batch_cap"] <= 0:
        raise ConfigError("batch_cap must be a positive integer")
    rate = float(base["sample_rate"])
    if not 0.0 <= rate <= 1.0:
        raise ConfigError("sample_rate must be between 0 and 1")
    if not 1 <= int(base["port"]) <= 65535:
        raise ConfigError("port must be between 1 and 65535")
    if not str(base["reviewer"]).strip():
        raise ConfigError("reviewer must be a non-empty string")
    if str(base["embedding_backend"]) not in ("http", "fake", "fail"):
        raise ConfigError("embedding_backend must be one of http, fake, fail")
    if int(base["embedding_dim"]) <= 0:
        raise ConfigError("embedding_dim must be a positive integer")

    evidence = tuple(
        item.strip()
        for item in str(base["evidence_min"]).split(",")
        if item.strip()
    )
    if not evidence:
        raise ConfigError("evidence_min must list at least one evidence type")

    return Config(
        kb_db=str(base["kb_db"]),
        corpus_db=str(base["corpus_db"]),
        corpus_dir=str(base["corpus_dir"]),
        legacy_dir=str(base["legacy_dir"]),
        migration_anchors=str(base["migration_anchors"]),
        ttl_days=int(base["ttl_days"]),
        batch_cap=int(base["batch_cap"]),
        sample_rate=rate,
        evidence_min=evidence,
        reviewer=str(base["reviewer"]),
        port=int(base["port"]),
        ontology=str(base["ontology"]),
        api_key=str(base["api_key"]),
        embedding_backend=str(base["embedding_backend"]),
        embedding_endpoint=str(base["embedding_endpoint"]),
        embedding_model=str(base["embedding_model"]),
        embedding_dim=int(base["embedding_dim"]),
        rerank_endpoint=str(base["rerank_endpoint"]),
        rerank_model=str(base["rerank_model"]),
        llm_endpoint=str(base["llm_endpoint"]),
        llm_model=str(base["llm_model"]),
        cloud_timeout=int(base["cloud_timeout"]),
        chroma_dir=str(base["chroma_dir"]),
    )
