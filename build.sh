#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -x .build-venv/bin/python ]]; then
    python3 -m venv .build-venv
fi

.build-venv/bin/python -m pip install -r requirements-build.txt
.build-venv/bin/python -m PyInstaller \
    --clean \
    --noconfirm \
    --onefile \
    --windowed \
    --name manjaro-treiber-check \
    --distpath dist \
    --workpath build \
    treiber_check.py

printf '\nFertig: %s/dist/manjaro-treiber-check\n' "$PWD"
