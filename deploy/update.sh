#!/usr/bin/env bash
# Jako użytkownik brandwise: pobiera main; przy zmianie instaluje zależności i zostawia znacznik do restartu.
set -euo pipefail
cd "$(dirname "$0")/.."
git fetch -q origin main
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  git merge -q --ff-only origin/main
  .venv/bin/pip install -q -r requirements.txt
  .venv/bin/pip install -q -e . --no-deps
  .venv/bin/python -m pytest -q -x >/dev/null 2>&1 || { echo "testy nie przechodzą – bez restartu"; exit 1; }
  touch data/.updated
fi
