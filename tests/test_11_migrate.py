"""Ticket 11 acceptance: legacy migration and held records."""

from __future__ import annotations

import json
from pathlib import Path

from support import run_cli

REPO = Path(__file__).resolve().parents[1]


def env_for(tmp_path: Path, legacy: Path, **extra: str) -> dict[str, str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    values = {
        "MPPKB_KB_DB": str(tmp_path / "data" / "kb.sqlite"),
        "MPPKB_CORPUS_DB": str(tmp_path / "data" / "corpus.sqlite"),
        "MPPKB_CORPUS_DIR": str(corpus),
        "MPPKB_ONTOLOGY": str(REPO / "ontology.yaml"),
        "MPPKB_LEGACY": str(legacy),
    }
    values.update(extra)
    return values


def make_legacy(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "evidence_nodes.json").write_text(
        json.dumps(
            [
                {
                    "id": "N1",
                    "population": "儿童",
                    "intervention": "阿奇霉素",
                    "attribute": "一线治疗",
                    "recommendation_direction": "推荐",
                    "evidence_type": "guideline",
                    "anchors": [
                        {
                            "doc_id": "LEGACY-1",
                            "page_start": 3,
                            "page_end": 3,
                            "quote": "推荐阿奇霉素",
                        }
                    ],
                },
                {
                    "id": "N2",
                    "population": "儿童",
                    "intervention": "红霉素",
                    "attribute": "疗程",
                    "recommendation_direction": "推荐",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "evidence_edges.json").write_text(
        json.dumps([{"source": "N1", "target": "N2", "relation": "supports"}], ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def migrate(tmp_path: Path, env: dict[str, str]) -> dict:
    result = run_cli(tmp_path, "migrate", "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def claims(tmp_path: Path, env: dict[str, str], status: str) -> list[dict]:
    result = run_cli(tmp_path, "claims", "list", "--status", status, "--json", env_extra=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["claims"]


def test_migration_imports_anchored_and_holds_unanchored(tmp_path):
    legacy = make_legacy(tmp_path / "legacy")
    env = env_for(tmp_path, legacy)

    report = migrate(tmp_path, env)

    assert report["migrated"] == 1
    assert report["held"] == 1
    assert report["edges"] == 1
    approved = claims(tmp_path, env, "approved")
    assert len(approved) == 1
    assert approved[0]["provenance"] == "migration"
    held = claims(tmp_path, env, "held")
    assert len(held) == 1
    assert held[0]["reason_code"] == "missing_anchors"


def test_migration_is_idempotent(tmp_path):
    legacy = make_legacy(tmp_path / "legacy")
    env = env_for(tmp_path, legacy)
    migrate(tmp_path, env)

    second = migrate(tmp_path, env)

    assert second["migrated"] == 0
    assert second["held"] == 0
    assert second["skipped"] == 2
    assert len(claims(tmp_path, env, "approved")) == 1
    assert len(claims(tmp_path, env, "held")) == 1


def test_anchor_overlay_promotes_held_record(tmp_path):
    legacy = make_legacy(tmp_path / "legacy")
    overlay_dir = tmp_path / "overlay"
    overlay_dir.mkdir()
    overlay_path = overlay_dir / "migration_anchors.json"
    overlay_path.write_text(
        json.dumps(
            {
                "generated_by": "ai-draft",
                "anchors": {
                    "N2": [
                        {
                            "doc_id": "LEGACY-2",
                            "page_start": 5,
                            "page_end": 5,
                            "quote": "推荐红霉素",
                            "ai_drafted": True,
                        }
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    env = env_for(tmp_path, legacy, MPPKB_MIGRATION_ANCHORS=str(overlay_path))

    report = migrate(tmp_path, env)

    assert report["anchors_applied"] == 1
    assert report["migrated"] == 2
    assert report["held"] == 0
    assert len(claims(tmp_path, env, "approved")) == 2


def test_migrated_claim_answers_and_verifies(tmp_path):
    legacy = make_legacy(tmp_path / "legacy")
    env = env_for(tmp_path, legacy)
    migrate(tmp_path, env)

    answer = run_cli(
        tmp_path, "answer", "--query", "阿奇霉素 一线治疗 儿童", "--json", env_extra=env
    )
    assert answer.returncode == 0, answer.stderr
    payload = json.loads(answer.stdout)
    assert payload["sections"]
    assert any(citation["page"] == 3 for citation in payload["citations"])

    verified = run_cli(tmp_path, "rebuild", "--verify", "--json", env_extra=env)
    assert verified.returncode == 0, verified.stdout
