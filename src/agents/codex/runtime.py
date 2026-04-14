from __future__ import annotations

import contextlib
import errno
import fnmatch
import json
import os
import shlex
import shutil
import time
from pathlib import Path

from json_utils import JSONInputTooLargeError, read_json_file
from paths import ensure_directories, locks_dir

from .toml_utils import (
    strip_notify_assignments,
    upsert_codex_hooks_feature,
    upsert_hide_rate_limit_model_nudge,
    upsert_project_trust,
    upsert_top_level_notify,
    upsert_update_check_setting,
    validate_toml_document,
)
from ..common import (
    DEFAULT_RUNTIME_HOME_ROOT,
    session_key,
    validate_discord_channel_id,
    write_notify_hook,
    write_text_atomically,
)


DEFAULT_CODEX_SOURCE_HOME = Path.home() / ".codex"
MANAGED_CODEX_COPY_FILES = (".personality_migration", "auth.json", "config.toml", "hooks.json", "mcp.json", "version.json")
MANAGED_CODEX_COPY_GLOBS = ("state_*.sqlite*",)
MANAGED_CODEX_COPY_DIRS = ("hooks", "memories", "rules", "skills")
MANAGED_CODEX_EXCLUDE_FILES = ("config.toml.orche.bak", "history.jsonl", "models_cache.json")
MANAGED_CODEX_EXCLUDE_FILE_GLOBS = ("*.lock", "*.log", "*.pid", "*.sock", "*.tmp", "logs_*.sqlite*")
MANAGED_CODEX_EXCLUDE_DIRS = {".tmp", "cache", "log", "sessions", "shell_snapshots", "tmp"}
SOURCE_CONFIG_LOCK_NAME = "codex-source-config"
SOURCE_CONFIG_BACKUP_SUFFIX = ".orche.bak"


def default_codex_home_path(session: str) -> Path:
    return DEFAULT_RUNTIME_HOME_ROOT / f"orche-codex-{session_key(session)}"


def default_notify_hook_path(codex_home: Path) -> Path:
    return codex_home / "hooks" / "discord-turn-notify.sh"


def default_hooks_path(codex_home: Path) -> Path:
    return codex_home / "hooks.json"


def source_hooks_path() -> Path:
    return default_hooks_path(DEFAULT_CODEX_SOURCE_HOME)


def source_codex_config_path() -> Path:
    return DEFAULT_CODEX_SOURCE_HOME / "config.toml"


def source_codex_config_backup_path() -> Path:
    return source_codex_config_path().with_name(source_codex_config_path().name + SOURCE_CONFIG_BACKUP_SUFFIX)


def render_hook_command(hook_path: Path, *, session: str, discord_channel_id: str | None, status: str | None = None) -> str:
    values = ["/bin/bash", str(hook_path), "--session", session]
    if discord_channel_id:
        values.extend(["--channel-id", validate_discord_channel_id(discord_channel_id)])
    if status:
        values.extend(["--status", status])
    return f"{' '.join(shlex.quote(value) for value in values)} >/dev/null"


def render_notify_assignment(hook_path: Path, *, session: str, discord_channel_id: str | None) -> str:
    values = ["/bin/bash", str(hook_path), "--session", session]
    if discord_channel_id:
        values.extend(["--channel-id", validate_discord_channel_id(discord_channel_id)])
    return "notify = [" + ", ".join(json.dumps(value) for value in values) + "]"


def read_text_or_empty(path: Path) -> str:
    return "" if not path.exists() else path.read_text(encoding="utf-8")


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = read_json_file(path)
    except (json.JSONDecodeError, JSONInputTooLargeError) as exc:
        raise RuntimeError(f"Refusing to write invalid JSON for {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Refusing to rewrite non-object Codex hooks config at {path}")
    return payload


def _matches_any(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _managed_codex_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in MANAGED_CODEX_EXCLUDE_DIRS:
            ignored.add(name)
            continue
        if name in MANAGED_CODEX_EXCLUDE_FILES or _matches_any(name, MANAGED_CODEX_EXCLUDE_FILE_GLOBS):
            ignored.add(name)
    return ignored


def build_hooks_payload(codex_home: Path, *, session: str, discord_channel_id: str | None, source_payload: dict[str, object] | None = None) -> dict[str, object]:
    payload = dict(source_payload or {})
    existing_hooks = payload.get("hooks")
    hooks = dict(existing_hooks) if isinstance(existing_hooks, dict) else {}
    command_hook = {"type": "command", "command": render_hook_command(default_notify_hook_path(codex_home), session=session, discord_channel_id=discord_channel_id)}
    session_start_entries = list(hooks.get("SessionStart")) if isinstance(hooks.get("SessionStart"), list) else []
    session_start_entries.append({"matcher": "startup", "hooks": [command_hook]})
    hooks["SessionStart"] = session_start_entries
    prompt_submit_entries = list(hooks.get("UserPromptSubmit")) if isinstance(hooks.get("UserPromptSubmit"), list) else []
    prompt_submit_entries.append({"hooks": [command_hook]})
    hooks["UserPromptSubmit"] = prompt_submit_entries
    payload["hooks"] = hooks
    return payload


def _read_lock_pid(path: Path) -> int | None:
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError:
        return None
    first_line = contents.splitlines()[0].strip() if contents else ""
    if not first_line:
        return None
    try:
        pid = int(first_line)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ESRCH}:
            return False
        if exc.errno == errno.EPERM:
            return True
        raise
    return True


@contextlib.contextmanager
def source_config_lock(*, timeout: float = 5.0):
    ensure_directories()
    path = locks_dir() / f"{SOURCE_CONFIG_LOCK_NAME}.lock"
    deadline = time.time() + timeout
    while True:
        try:
            fd = path.open("x")
            break
        except FileExistsError:
            lock_pid = _read_lock_pid(path)
            if lock_pid is not None and not _pid_is_alive(lock_pid):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                continue
            if time.time() > deadline:
                raise RuntimeError("Timed out waiting for Codex source config lock")
            time.sleep(0.1)
    try:
        fd.write(f"{os.getpid()}\n{Path.cwd()}\n")
        fd.flush()
        yield
    finally:
        fd.close()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def sync_trust_to_source_config(cwd: Path) -> str:
    config_path = source_codex_config_path()
    with source_config_lock():
        original = read_text_or_empty(config_path)
        if original:
            validate_toml_document(original, label=str(config_path))
        updated = upsert_project_trust(original, cwd)
        if updated != original:
            validate_toml_document(updated, label=str(config_path))
            write_text_atomically(config_path, updated, backup_path=source_codex_config_backup_path())
        return updated


def _copy_path(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True, ignore=_managed_codex_ignore)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def cleanup_managed_codex_home(codex_home: Path) -> None:
    for root, dir_names, file_names in os.walk(codex_home, topdown=True):
        root_path = Path(root)
        kept_dir_names: list[str] = []
        for name in dir_names:
            if name in MANAGED_CODEX_EXCLUDE_DIRS:
                shutil.rmtree(root_path / name, ignore_errors=True)
                continue
            kept_dir_names.append(name)
        dir_names[:] = kept_dir_names
        for name in file_names:
            if name in MANAGED_CODEX_EXCLUDE_FILES or _matches_any(name, MANAGED_CODEX_EXCLUDE_FILE_GLOBS):
                with contextlib.suppress(OSError):
                    (root_path / name).unlink()


def materialize_managed_codex_home(source_home: Path, target_home: Path) -> None:
    target_home.mkdir(parents=True, exist_ok=True)
    if not source_home.exists():
        cleanup_managed_codex_home(target_home)
        return
    for name in MANAGED_CODEX_COPY_FILES:
        source_path = source_home / name
        if source_path.exists():
            _copy_path(source_path, target_home / name)
    for pattern in MANAGED_CODEX_COPY_GLOBS:
        for source_path in sorted(source_home.glob(pattern)):
            if source_path.exists() and source_path.is_file():
                _copy_path(source_path, target_home / source_path.name)
    for name in MANAGED_CODEX_COPY_DIRS:
        source_path = source_home / name
        if source_path.exists() and source_path.is_dir():
            _copy_path(source_path, target_home / name)
    cleanup_managed_codex_home(target_home)


def rewrite_codex_config(codex_home: Path, *, session: str, cwd: Path, discord_channel_id: str | None) -> None:
    config_toml_path = codex_home / "config.toml"
    hooks_json_path = default_hooks_path(codex_home)
    base_content = sync_trust_to_source_config(cwd)
    notify_line = render_notify_assignment(default_notify_hook_path(codex_home), session=session, discord_channel_id=discord_channel_id)
    updated = "".join(strip_notify_assignments(base_content.splitlines(keepends=True)))
    updated = upsert_update_check_setting(updated, enabled=False)
    updated = upsert_hide_rate_limit_model_nudge(updated, enabled=True)
    updated = upsert_top_level_notify(updated, notify_line)
    updated = upsert_codex_hooks_feature(updated, enabled=True)
    validate_toml_document(updated, label=str(config_toml_path))
    write_text_atomically(config_toml_path, updated)
    hooks_payload = build_hooks_payload(codex_home, session=session, discord_channel_id=discord_channel_id, source_payload=_read_json_object(source_hooks_path()))
    write_text_atomically(hooks_json_path, json.dumps(hooks_payload, indent=2, ensure_ascii=False) + "\n")
