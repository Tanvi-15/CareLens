#!/usr/bin/env bash
# One-command start: creates the venv, installs deps, builds the sample reports, starts the app.
set -euo pipefail
cd "$(dirname "$0")"

MODEL="${CARELENS_MODEL:-medgemma}"
PORT="${PORT:-8765}"

command -v ollama >/dev/null || { echo "Install Ollama first: https://ollama.com"; exit 1; }
# capture first: piping into grep -q under pipefail can fail with SIGPIPE and trigger a needless pull
installed="$(ollama list)"
TAG="$MODEL"; [[ "$TAG" == *:* ]] || TAG="$TAG:latest"
grep -q "^${TAG}[[:space:]]" <<<"$installed" || ollama pull "$MODEL"

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
[ -f samples/sample_lab_report.pdf ] || .venv/bin/python samples/make_sample.py

echo "CareLens running at http://localhost:$PORT (model: $MODEL)"
CARELENS_MODEL="$MODEL" exec .venv/bin/uvicorn main:app --app-dir backend --port "$PORT"
