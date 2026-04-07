#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

rm -rf build-nuitka dist

python -m nuitka \
  --standalone \
  --static-libpython=no \
  --assume-yes-for-downloads \
  --output-dir=build-nuitka \
  --output-filename=orche \
  --include-package=agents \
  --include-package=notify \
  --include-package=rich._unicode_data \
  --include-module=backend \
  --include-module=paths \
  --include-module=self_update \
  --include-module=version \
  src/cli.py

mkdir -p dist
mv build-nuitka/cli.dist dist/orche
