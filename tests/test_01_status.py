"""Ticket 01 acceptance: skeleton, dual databases, config, status view."""

from __future__ import annotations

import json

from support import run_cli


def env_for(tmp_path, corpus: bool = True, **extra: str) -> dict[str, str]:
    corpus_dir = tmp_path / "corpus"
    if corpus:
        corpus_dir.mkdir(parents=True, exist_ok=True)
    values = {
        "MPPKB_KB_DB": str(tmp_path / "data" / "kb.sqlite"),
        "MPPKB_CORPUS_DB": str(tmp_path / "data" / "corpus.sqlite"),
        "MPPKB_CORPUS_DIR": str(corpus_dir),
    }
    values.update(extra)
    return values


def test_status_creates_databases_and_reports_zero(tmp_path):
    result = run_cli(tmp_path, "status", "--json", env_extra=env_for(tmp_path))

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["library_version"] == 0
    assert payload["ontology_version"] == 0
    assert payload["counts"] == {
        "claims": 0,
        "versions": 0,
        "proposed": 0,
        "queue": 0,
        "expired": 0,
        "held": 0,
    }
    assert (tmp_path / "data" / "kb.sqlite").is_file()
    assert (tmp_path / "data" / "corpus.sqlite").is_file()


def test_status_is_idempotent(tmp_path):
    env = env_for(tmp_path)
    first = run_cli(tmp_path, "status", "--json", env_extra=env)
    second = run_cli(tmp_path, "status", "--json", env_extra=env)

    assert first.returncode == 0
    assert second.returncode == 0
    assert json.loads(first.stdout) == json.loads(second.stdout)


def test_missing_corpus_dir_fails_with_readable_error(tmp_path):
    result = run_cli(tmp_path, "status", "--json", env_extra=env_for(tmp_path, corpus=False))

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "corpus" in payload["error"]


def test_invalid_ttl_fails_fast(tmp_path):
    env = env_for(tmp_path, MPPKB_TTL_DAYS="-1")

    result = run_cli(tmp_path, "status", "--json", env_extra=env)

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "ttl_days" in payload["error"]


def test_invalid_sample_rate_fails_fast(tmp_path):
    env = env_for(tmp_path, MPPKB_SAMPLE_RATE="1.5")

    result = run_cli(tmp_path, "status", "--json", env_extra=env)

    assert result.returncode == 2
    assert "sample_rate" in json.loads(result.stdout)["error"]


def test_config_file_is_honoured(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (tmp_path / "mppkb.json").write_text(
        json.dumps(
            {
                "corpus_dir": str(corpus),
                "kb_db": str(tmp_path / "data" / "kb.sqlite"),
                "corpus_db": str(tmp_path / "data" / "corpus.sqlite"),
            }
        ),
        encoding="utf-8",
    )

    result = run_cli(tmp_path, "status", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["corpus_dir"] == str(corpus)


def test_unknown_command_is_rejected(tmp_path):
    # Every declared command now resolves to a module; an unknown command must
    # still fail cleanly at the argument parser.
    result = run_cli(tmp_path, "definitely-not-a-command")

    assert result.returncode == 2
    assert "invalid choice" in (result.stderr + result.stdout)


def test_help_lists_command_surface(tmp_path):
    result = run_cli(tmp_path, "--help")

    assert result.returncode == 0
    for name in [
        "status",
        "inspect",
        "ingest",
        "claims",
        "ontology",
        "review",
        "rebuild",
        "serve",
        "migrate",
    ]:
        assert name in result.stdout
