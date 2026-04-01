#!/bin/bash

set -euo pipefail

DEFAULT_ORCHE_BIN="orche"
ORCHE_BIN="${ORCHE_BIN:-${DEFAULT_ORCHE_BIN}}"

exec "${ORCHE_BIN}" _notify-discord "$@"
