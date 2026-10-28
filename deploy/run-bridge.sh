#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UI_REPO="${UI_REPO:-$SCRIPT_DIR/MPP_Project}"
DATA_DIR="${MPPKB_DATA_DIR:-$SCRIPT_DIR/data}"
PORT="${MPPKB_PORT:-8791}"
ENV_FILE="${MPPKB_ENV_FILE:-$SCRIPT_DIR/.env}"

if [[ ! -f "$UI_REPO/web/index.html" ]]; then
  echo "MPP_Project UI not found at $UI_REPO (clone it or set UI_REPO)" >&2
  exit 1
fi
mkdir -p "$DATA_DIR" "$DATA_DIR/pdfs"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

export MPPKB_KB_DB="${MPPKB_KB_DB:-$DATA_DIR/kb.sqlite}"
export MPPKB_CORPUS_DB="${MPPKB_CORPUS_DB:-$DATA_DIR/corpus.sqlite}"
export MPPKB_CORPUS_DIR="${MPPKB_CORPUS_DIR:-$DATA_DIR/pdfs}"
export MPPKB_CHROMA_DIR="${MPPKB_CHROMA_DIR:-$DATA_DIR/chroma}"
export MPPKB_ONTOLOGY="${MPPKB_ONTOLOGY:-$ROOT/ontology.yaml}"
export MPPKB_RETRIEVAL_MODE="${MPPKB_RETRIEVAL_MODE:-lexical}"
export MPPKB_REVIEWER="${MPPKB_REVIEWER:-clinical-reviewer}"
export PYTHONIOENCODING=utf-8

cd "$ROOT"
exec python -m mppkb.ui_bridge --repo "$UI_REPO" --host 127.0.0.1 --port "$PORT"
