from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from paths import bridges_dir, ensure_directories


DEFAULT_RUNTIME_HOME_ROOT = Path(tempfile.gettempdir())


def normalize_runtime_home(runtime_home: str | Path | None) -> str:
    if runtime_home in (None, ""):
        return ""
    return str(Path(str(runtime_home)).expanduser().resolve())


def session_key(session: str) -> str:
    lowered = []
    for ch in session.lower():
        if ch.isalnum():
            lowered.append(ch)
        elif ch in ("-", "_", "/", "."):
            lowered.append("-")
    value = "".join(lowered)
    while "--" in value:
        value = value.replace("--", "-")
    return value.strip("-") or "root"


def validate_discord_channel_id(value: str) -> str:
    channel_id = re.sub(r"\s+", "", value or "")
    if not channel_id or not channel_id.isdigit():
        raise ValueError("--discord-channel-id must be a numeric Discord channel ID")
    return channel_id


def write_text_atomically(path: Path, content: str, *, backup_path: Path | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    try:
        if backup_path is not None and path.exists():
            shutil.copy2(path, backup_path)
        temp_path.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


def remove_runtime_home(runtime_home: str | Path) -> None:
    normalized = Path(normalize_runtime_home(runtime_home))
    candidates: list[Path] = []
    for candidate in (normalized, normalized.resolve()):
        if candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if candidate.exists():
            shutil.rmtree(candidate, ignore_errors=True)


def write_notify_hook(hook_path: Path) -> None:
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    orche_command = " ".join(shlex.quote(part) for part in orche_bootstrap_command())
    lines = [
        "#!/bin/sh",
        "set -eu",
    ]
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            lines.append(f"export {name}={shlex.quote(value)}")
    lines.append(f'exec {orche_command} notify-internal "$@"')
    lines.append("")
    hook_path.write_text("\n".join(lines), encoding="utf-8")
    hook_path.chmod(0o755)


def _resolve_executable(path: Path | str | None) -> Path | None:
    if path in (None, ""):
        return None
    candidate = Path(str(path)).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.exists() or not os.access(resolved, os.X_OK):
        return None
    return resolved


def _current_orche_executable(*, shim_path: Path) -> Path | None:
    candidates: list[Path] = []
    argv = list(getattr(sys, "argv", []) or [])
    argv0 = str(argv[0]).strip() if argv else ""
    if argv0:
        argv0_path = Path(argv0).expanduser()
        if argv0_path.name.startswith("orche"):
            candidates.append(argv0_path)
        argv0_lookup = shutil.which(argv0)
        if argv0_lookup:
            candidates.append(Path(argv0_lookup))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).expanduser())
        which_orche = shutil.which("orche")
        if which_orche:
            candidates.append(Path(which_orche))
    excluded = _resolve_executable(shim_path)
    for candidate in candidates:
        resolved = _resolve_executable(candidate)
        if resolved is None:
            continue
        if excluded is not None and resolved == excluded:
            continue
        if resolved.name.startswith("orche"):
            return resolved
    return None


def orche_bootstrap_command(*, shim_path: Path | None = None) -> list[str]:
    effective_shim_path = shim_path or (bridges_dir() / "bin" / "orche")
    executable = _current_orche_executable(shim_path=effective_shim_path)
    if executable is not None:
        return [str(executable)]
    source_root = Path(__file__).resolve().parent.parent
    bootstrap = (
        "import sys; "
        f"sys.path.insert(0, {str(source_root)!r}); "
        "import cli; "
        'sys.argv = ["orche", *sys.argv[1:]]; '
        "raise SystemExit(cli.main())"
    )
    python_executable = _resolve_executable(sys.executable)
    if python_executable is None:
        raise RuntimeError("Unable to resolve Python executable for orche bootstrap")
    return [str(python_executable), "-c", bootstrap]


def ensure_orche_shim() -> Path:
    ensure_directories()
    shim_path = bridges_dir() / "bin" / "orche"
    shim_command = " ".join(shlex.quote(part) for part in orche_bootstrap_command(shim_path=shim_path))
    shim_body = "\n".join(
        (
            "#!/bin/sh",
            "set -eu",
            f'exec {shim_command} "$@"',
            "",
        )
    )
    if not shim_path.exists() or shim_path.read_text(encoding="utf-8") != shim_body:
        shim_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomically(shim_path, shim_body)
        shim_path.chmod(0o755)
    return shim_path
