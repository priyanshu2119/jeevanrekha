#!/usr/bin/env bash
# One-command bring-up for JeevanRekha (local).
set -euo pipefail
cd "$(dirname "$0")"

PY=python3.11
if ! command -v "$PY" >/dev/null 2>&1; then PY=python3; fi

if [ ! -d .venv ]; then
  echo "==> creating virtualenv (.venv)"
  "$PY" -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

echo "==> seeding database (no-op if already seeded)"
.venv/bin/python scripts/seed.py

PORT="${PORT:-8300}"
echo "==> starting JeevanRekha on http://127.0.0.1:${PORT}"
echo "    caller flows : http://127.0.0.1:${PORT}/triage   (web companion)"
echo "    phone path   : http://127.0.0.1:${PORT}/sim      (DTMF simulator + dispatch desk)"
echo "    staff        : admin/admin123 · sunita/asha123 · operator/op123"
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
