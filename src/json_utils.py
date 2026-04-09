from __future__ import annotations

import json
from pathlib import Path
from typing import Any


MAX_JSON_INPUT_BYTES = 10 * 1024 * 1024


class JSONInputTooLargeError(ValueError):
    pass


def _json_size_bytes(raw: str) -> int:
    return len(raw.encode("utf-8"))


def ensure_json_size(raw: str, *, source: str, max_bytes: int = MAX_JSON_INPUT_BYTES) -> None:
    if _json_size_bytes(raw) > max_bytes:
        raise JSONInputTooLargeError(
            f"Refusing to parse JSON larger than {max_bytes} bytes from {source}"
        )


def loads_json(raw: str, *, source: str, max_bytes: int = MAX_JSON_INPUT_BYTES) -> Any:
    ensure_json_size(raw, source=source, max_bytes=max_bytes)
    return json.loads(raw)


def read_json_file(path: Path, *, max_bytes: int = MAX_JSON_INPUT_BYTES) -> Any:
    size = path.stat().st_size
    if size > max_bytes:
        raise JSONInputTooLargeError(
            f"Refusing to parse JSON larger than {max_bytes} bytes from {path}"
        )
    return loads_json(path.read_text(encoding="utf-8"), source=str(path), max_bytes=max_bytes)
