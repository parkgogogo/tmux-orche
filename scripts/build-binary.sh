#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

python -m PyInstaller \
  --clean \
  --noconfirm \
  --onefile \
  --name orche \
  --paths src \
  --hidden-import agents.codex \
  --hidden-import agents.claude \
  src/cli.py
