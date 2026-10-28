#!/usr/bin/env bash
set -euo pipefail
URL="${1:-http://127.0.0.1:8791/api/health}"
BODY="$(curl -fsS --max-time 5 "$URL")"
echo "$BODY" | grep -q '"status": *"ok"' || { echo "unhealthy: $BODY" >&2; exit 1; }
echo "ok"
