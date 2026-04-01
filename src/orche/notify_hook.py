from __future__ import annotations


NOTIFY_DISCORD_SH = r"""#!/bin/bash

set -euo pipefail

DEFAULT_ORCHE_BIN="orche"
ORCHE_BIN="${ORCHE_BIN:-${DEFAULT_ORCHE_BIN}}"

exec "${ORCHE_BIN}" _notify-discord "$@"
"""
