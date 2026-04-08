#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  else
    echo "python or python3 is required to build the binary" >&2
    exit 1
  fi
fi

rm -rf build-nuitka dist

"${PYTHON_BIN}" -m nuitka \
  --standalone \
  --static-libpython=no \
  --assume-yes-for-downloads \
  --output-dir=build-nuitka \
  --output-filename=orche \
  --include-package=agents \
  --include-package=certifi \
  --include-package-data=certifi \
  --include-package=notify \
  --include-package=rich._unicode_data \
  --include-module=backend \
  --include-module=paths \
  --include-module=self_update \
  --include-module=tls \
  --include-module=version \
  src/cli.py

mkdir -p dist
mv build-nuitka/cli.dist dist/orche
