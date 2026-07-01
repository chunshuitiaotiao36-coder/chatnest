#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi
export AGENT_APP_ROOT="${AGENT_APP_ROOT:-$(pwd)}"
exec "${PYTHON:-python3}" -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8787}"
