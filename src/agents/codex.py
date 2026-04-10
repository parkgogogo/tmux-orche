from __future__ import annotations

import contextlib
import errno
import fnmatch
import importlib
import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None

if __package__ and "." in __package__:
    _json_utils = importlib.import_module("..json_utils", __package__)
    JSONInputTooLargeError = _json_utils.JSONInputTooLargeError
    read_json_file = _json_utils.read_json_file
    _paths = importlib.import_module("..paths", __package__)
    ensure_directories = _paths.ensure_directories
    locks_dir = _paths.locks_dir
else:
    from json_utils import JSONInputTooLargeError, read_json_file
    from paths import ensure_directories, locks_dir

from .base import AgentPlugin, AgentRuntime, BridgeIO
from .common import (
    DEFAULT_RUNTIME_HOME_ROOT,
    ensure_orche_shim,
    normalize_runtime_home,
    remove_runtime_home,
    session_key,
    validate_discord_channel_id,
    write_notify_hook,
    write_text_atomically,
)


READY_SURFACE_HINTS = (
    "OpenAI Codex",
    "Approvals:",
    "model:",
    "full-auto",
    "dangerously-bypass-approvals-and-sandbox",
    "Esc to interrupt",
    "Ctrl-C to interrupt",
)
CODEX_FAILURE_HINTS = (
    "stream disconnected before completion",
    "error sending request for url",
)
CODEX_SUBMIT_SETTLE_MIN_SECONDS = 0.5
CODEX_SUBMIT_SETTLE_MAX_SECONDS = 1.5
CODEX_SUBMIT_SECONDS_PER_CHAR = 0.01
DEFAULT_CODEX_SOURCE_HOME = Path.home() / ".codex"
MANAGED_CODEX_COPY_FILES = (
    ".personality_migration",
    "auth.json",
    "config.toml",
    "hooks.json",
    "mcp.json",
    "version.json",
)
MANAGED_CODEX_COPY_GLOBS = ("state_*.sqlite*",)
MANAGED_CODEX_COPY_DIRS = ("hooks", "memories", "rules", "skills")
MANAGED_CODEX_EXCLUDE_FILES = (
    "config.toml.orche.bak",
    "history.jsonl",
    "models_cache.json",
)
MANAGED_CODEX_EXCLUDE_FILE_GLOBS = ("*.lock", "*.log", "*.pid", "*.sock", "*.tmp", "logs_*.sqlite*")
MANAGED_CODEX_EXCLUDE_DIRS = {".tmp", "cache", "log", "sessions", "shell_snapshots", "tmp"}
TOML_TABLE_HEADER_RE = re.compile(r"^\s*\[\[?.*\]\]?\s*$")
TOML_NOTICE_HEADER_RE = re.compile(r"^\s*\[notice\]\s*$")
TOML_FEATURES_HEADER_RE = re.compile(r"^\s*\[features\]\s*$")
TOML_NOTIFY_KEY_RE = re.compile(r"^\s*notify\s*=")
TOML_UPDATE_CHECK_RE = re.compile(r"^\s*check_for_update_on_startup\s*=")
TOML_HIDE_RATE_LIMIT_MODEL_NUDGE_RE = re.compile(r"^\s*hide_rate_limit_model_nudge\s*=")
TOML_CODEX_HOOKS_RE = re.compile(r"^\s*codex_hooks\s*=")
TOML_PROJECT_HEADER_RE = re.compile(r"^\s*\[projects\.(.+)\]\s*$")
TOML_TRUST_LEVEL_RE = re.compile(r"^\s*trust_level\s*=")
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


def render_hook_command(
    hook_path: Path,
    *,
    session: str,
    discord_channel_id: str | None,
    status: str | None = None,
) -> str:
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


def render_update_check_setting(enabled: bool) -> str:
    return f"check_for_update_on_startup = {'true' if enabled else 'false'}"


def render_notice_boolean_setting(name: str, enabled: bool) -> str:
    return f"{name} = {'true' if enabled else 'false'}"


def strip_notify_assignments(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    skipping = False
    bracket_depth = 0
    for line in lines:
        if not skipping and TOML_NOTIFY_KEY_RE.match(line):
            skipping = True
            bracket_depth = line.count("[") - line.count("]")
            if bracket_depth <= 0:
                skipping = False
            continue
        if skipping:
            bracket_depth += line.count("[") - line.count("]")
            if bracket_depth <= 0:
                skipping = False
            continue
        cleaned.append(line)
    return cleaned


def read_text_or_empty(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


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


def _compact_prompt_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


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


def codex_submit_settle_seconds(prompt: str) -> float:
    if not prompt:
        return 0.0
    scaled = len(prompt) * CODEX_SUBMIT_SECONDS_PER_CHAR
    return max(CODEX_SUBMIT_SETTLE_MIN_SECONDS, min(CODEX_SUBMIT_SETTLE_MAX_SECONDS, scaled))


def _is_codex_status_line(line: str) -> bool:
    compact = _compact_prompt_text(line).lower()
    if not compact:
        return False
    if compact.startswith(("tip:", "command:", "chunk id:", "wall time:", "output:")):
        return True
    return "gpt-" in compact and "% left" in compact


def _is_codex_prompt_continuation(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(("› ", "• ", "⚠ ", "╭", "╰", "│", "└")):
        return False
    return not _is_codex_status_line(stripped)


def _find_codex_prompt_block(lines: list[str], prompt: str) -> tuple[int, int] | None:
    prompt_inline = _compact_prompt_text(prompt)
    if not prompt_inline:
        return None
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped.startswith("› "):
            continue
        parts = [stripped[2:].strip()]
        end_index = index
        cursor = index + 1
        while cursor < len(lines) and _is_codex_prompt_continuation(lines[cursor]):
            parts.append(lines[cursor].strip())
            end_index = cursor
            cursor += 1
        rendered_prompt = _compact_prompt_text(" ".join(parts))
        if rendered_prompt and (rendered_prompt in prompt_inline or prompt_inline in rendered_prompt):
            return index, end_index
    return None


def _find_next_codex_prompt(lines: list[str], start_index: int) -> int | None:
    for index in range(max(start_index, 0), len(lines)):
        if lines[index].strip().startswith("› "):
            return index
    return None


def _is_codex_transient_output(line: str) -> bool:
    compact = _compact_prompt_text(line).lower()
    if not compact:
        return True
    if "esc to interrupt" in compact:
        return True
    return compact.startswith(("working ", "working("))


def _is_codex_output_continuation(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(("› ", "• ", "⚠ ", "╭", "╰", "│", "└")):
        return False
    return not _is_codex_status_line(stripped)


def _collect_codex_wrapped_block(lines: list[str], start_index: int) -> str:
    parts = [lines[start_index].strip()]
    cursor = start_index + 1
    while cursor < len(lines):
        stripped = lines[cursor].strip()
        if not stripped:
            break
        if stripped.startswith(("› ", "• ", "⚠ ", "╭", "╰", "│", "└")):
            break
        if _is_codex_status_line(stripped):
            break
        parts.append(stripped)
        cursor += 1
    return _compact_prompt_text(" ".join(parts))


def _extract_codex_failure_summary(capture: str, prompt: str) -> str:
    lines = capture.splitlines()
    prompt_inline = _compact_prompt_text(prompt)
    for index, _raw_line in enumerate(lines):
        candidate = _collect_codex_wrapped_block(lines, index)
        if not candidate:
            continue
        lowered = candidate.lower()
        if prompt_inline and _compact_prompt_text(candidate) == prompt_inline:
            continue
        if not any(hint in lowered for hint in CODEX_FAILURE_HINTS):
            continue
        return candidate
    return ""


def _extract_codex_completion_summary(capture: str, prompt: str) -> str:
    lines = capture.splitlines()
    prompt_block = _find_codex_prompt_block(lines, prompt)
    if prompt_block is None:
        return ""
    _prompt_start, prompt_end = prompt_block
    next_prompt_index = _find_next_codex_prompt(lines, prompt_end + 1)
    if next_prompt_index is None:
        return ""
    summaries: list[str] = []
    current_output: list[str] = []
    for raw_line in lines[prompt_end + 1 : next_prompt_index]:
        stripped = raw_line.strip()
        if not stripped:
            if current_output:
                summary = _compact_prompt_text(" ".join(current_output))
                if summary and not _is_codex_transient_output(summary):
                    summaries.append(summary)
                current_output = []
            continue
        if stripped.startswith("• "):
            if current_output:
                summary = _compact_prompt_text(" ".join(current_output))
                if summary and not _is_codex_transient_output(summary):
                    summaries.append(summary)
            current_output = [stripped[2:].strip()]
            continue
        if current_output and _is_codex_output_continuation(raw_line):
            current_output.append(stripped)
            continue
        if current_output:
            summary = _compact_prompt_text(" ".join(current_output))
            if summary and not _is_codex_transient_output(summary):
                summaries.append(summary)
            current_output = []
    if current_output:
        summary = _compact_prompt_text(" ".join(current_output))
        if summary and not _is_codex_transient_output(summary):
            summaries.append(summary)
    return summaries[-1] if summaries else ""


def validate_toml_document(content: str, *, label: str) -> None:
    if tomllib is None:
        return
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"Refusing to write invalid TOML for {label}: {exc}") from exc


def _project_header_path(line: str) -> str | None:
    match = TOML_PROJECT_HEADER_RE.match(line)
    if match is None:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def render_project_trust_block(cwd: Path) -> str:
    return f"[projects.{json.dumps(str(cwd.resolve()))}]\ntrust_level = \"trusted\"\n"


def upsert_project_trust(content: str, cwd: Path) -> str:
    target = str(cwd.resolve())
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if _project_header_path(line) != target:
            continue
        section_end = index + 1
        while section_end < len(lines) and not TOML_TABLE_HEADER_RE.match(lines[section_end]):
            section_end += 1
        for trust_index in range(index + 1, section_end):
            if not TOML_TRUST_LEVEL_RE.match(lines[trust_index]):
                continue
            replacement = 'trust_level = "trusted"\n'
            if lines[trust_index] == replacement:
                return content
            lines[trust_index] = replacement
            return "".join(lines)
        lines.insert(section_end, 'trust_level = "trusted"\n')
        return "".join(lines)
    updated = content
    if updated and not updated.endswith("\n"):
        updated += "\n"
    if updated.strip():
        updated += "\n"
    updated += render_project_trust_block(cwd)
    return updated


def upsert_top_level_setting(content: str, *, notify_line: str, matcher: re.Pattern[str]) -> str:
    lines = content.splitlines(keepends=True)
    first_table_index = next((index for index, line in enumerate(lines) if TOML_TABLE_HEADER_RE.match(line)), len(lines))
    prefix = [line for line in lines[:first_table_index] if not matcher.match(line)]
    suffix = lines[first_table_index:]
    while prefix and not prefix[-1].strip():
        prefix.pop()
    while suffix and not suffix[0].strip():
        suffix.pop(0)
    updated: list[str] = list(prefix)
    if updated and not updated[-1].endswith("\n"):
        updated[-1] += "\n"
    if updated:
        updated.append("\n")
    updated.append(notify_line + "\n")
    if suffix:
        updated.append("\n")
        updated.extend(suffix)
    return "".join(updated)


def upsert_top_level_notify(content: str, notify_line: str) -> str:
    return upsert_top_level_setting(content, notify_line=notify_line, matcher=TOML_NOTIFY_KEY_RE)


def upsert_update_check_setting(content: str, *, enabled: bool) -> str:
    return upsert_top_level_setting(
        content,
        notify_line=render_update_check_setting(enabled),
        matcher=TOML_UPDATE_CHECK_RE,
    )


def upsert_notice_setting(
    content: str,
    *,
    matcher: re.Pattern[str],
    setting_line: str,
) -> str:
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not TOML_NOTICE_HEADER_RE.match(line):
            continue
        section_end = index + 1
        while section_end < len(lines) and not TOML_TABLE_HEADER_RE.match(lines[section_end]):
            section_end += 1
        for setting_index in range(index + 1, section_end):
            if not matcher.match(lines[setting_index]):
                continue
            replacement = setting_line + "\n"
            if lines[setting_index] == replacement:
                return content
            lines[setting_index] = replacement
            return "".join(lines)
        lines.insert(section_end, setting_line + "\n")
        return "".join(lines)
    updated = content
    if updated and not updated.endswith("\n"):
        updated += "\n"
    if updated.strip():
        updated += "\n"
    updated += "[notice]\n"
    updated += setting_line + "\n"
    return updated


def upsert_hide_rate_limit_model_nudge(content: str, *, enabled: bool) -> str:
    return upsert_notice_setting(
        content,
        matcher=TOML_HIDE_RATE_LIMIT_MODEL_NUDGE_RE,
        setting_line=render_notice_boolean_setting("hide_rate_limit_model_nudge", enabled),
    )


def upsert_features_setting(
    content: str,
    *,
    matcher: re.Pattern[str],
    setting_line: str,
) -> str:
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not TOML_FEATURES_HEADER_RE.match(line):
            continue
        section_end = index + 1
        while section_end < len(lines) and not TOML_TABLE_HEADER_RE.match(lines[section_end]):
            section_end += 1
        for setting_index in range(index + 1, section_end):
            if not matcher.match(lines[setting_index]):
                continue
            replacement = setting_line + "\n"
            if lines[setting_index] == replacement:
                return content
            lines[setting_index] = replacement
            return "".join(lines)
        lines.insert(section_end, setting_line + "\n")
        return "".join(lines)
    updated = content
    if updated and not updated.endswith("\n"):
        updated += "\n"
    if updated.strip():
        updated += "\n"
    updated += "[features]\n"
    updated += setting_line + "\n"
    return updated


def upsert_codex_hooks_feature(content: str, *, enabled: bool) -> str:
    return upsert_features_setting(
        content,
        matcher=TOML_CODEX_HOOKS_RE,
        setting_line=render_notice_boolean_setting("codex_hooks", enabled),
    )


def build_hooks_payload(
    codex_home: Path,
    *,
    session: str,
    discord_channel_id: str | None,
    source_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = dict(source_payload or {})
    existing_hooks = payload.get("hooks")
    hooks = dict(existing_hooks) if isinstance(existing_hooks, dict) else {}
    command_hook = {
        "type": "command",
        "command": render_hook_command(
            default_notify_hook_path(codex_home),
            session=session,
            discord_channel_id=discord_channel_id,
        ),
    }
    session_start_entries = list(hooks.get("SessionStart")) if isinstance(hooks.get("SessionStart"), list) else []
    session_start_entries.append(
        {
            "matcher": "startup",
            "hooks": [command_hook],
        }
    )
    hooks["SessionStart"] = session_start_entries
    prompt_submit_entries = (
        list(hooks.get("UserPromptSubmit")) if isinstance(hooks.get("UserPromptSubmit"), list) else []
    )
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
    if pid <= 0:
        return None
    return pid


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
            write_text_atomically(
                config_path,
                updated,
                backup_path=source_codex_config_backup_path(),
            )
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


def rewrite_codex_config(
    codex_home: Path,
    *,
    session: str,
    cwd: Path,
    discord_channel_id: str | None,
) -> None:
    config_toml_path = codex_home / "config.toml"
    hooks_json_path = default_hooks_path(codex_home)
    base_content = sync_trust_to_source_config(cwd)
    notify_line = render_notify_assignment(
        default_notify_hook_path(codex_home),
        session=session,
        discord_channel_id=discord_channel_id,
    )
    updated = "".join(strip_notify_assignments(base_content.splitlines(keepends=True)))
    updated = upsert_update_check_setting(updated, enabled=False)
    updated = upsert_hide_rate_limit_model_nudge(updated, enabled=True)
    updated = upsert_top_level_notify(updated, notify_line)
    updated = upsert_codex_hooks_feature(updated, enabled=True)
    validate_toml_document(updated, label=str(config_toml_path))
    write_text_atomically(config_toml_path, updated)
    hooks_payload = build_hooks_payload(
        codex_home,
        session=session,
        discord_channel_id=discord_channel_id,
        source_payload=_read_json_object(source_hooks_path()),
    )
    write_text_atomically(hooks_json_path, json.dumps(hooks_payload, indent=2, ensure_ascii=False) + "\n")


class CodexAgent(AgentPlugin):
    name = "codex"
    display_name = "Codex"
    runtime_label = "CODEX_HOME"
    login_prompts = ("Login with ChatGPT", "Please login")

    def ensure_managed_runtime(
        self,
        session: str,
        *,
        cwd: Path,
        discord_channel_id: str | None,
    ) -> AgentRuntime:
        target = default_codex_home_path(session)
        materialize_managed_codex_home(DEFAULT_CODEX_SOURCE_HOME, target)
        write_notify_hook(default_notify_hook_path(target))
        rewrite_codex_config(target, session=session, cwd=cwd, discord_channel_id=discord_channel_id)
        return AgentRuntime(home=str(target.resolve()), managed=True, label=self.runtime_label)

    def build_launch_command(
        self,
        *,
        cwd: Path,
        runtime: AgentRuntime,
        session: str,
        discord_channel_id: str | None,
        approve_all: bool,
    ) -> str:
        _ = approve_all
        prefix = [f"cd {shlex.quote(str(cwd))}"]
        orche_shim = ensure_orche_shim()
        prefix.append(f"export ORCHE_BIN={shlex.quote(str(orche_shim))}")
        prefix.append(f"export PATH={shlex.quote(str(orche_shim.parent))}:$PATH")
        normalized_runtime_home = normalize_runtime_home(runtime.home)
        if normalized_runtime_home:
            prefix.append(f"mkdir -p {shlex.quote(normalized_runtime_home)}")
            prefix.append(f"export CODEX_HOME={shlex.quote(normalized_runtime_home)}")
        if session:
            prefix.append(f"export ORCHE_SESSION={shlex.quote(session)}")
        if discord_channel_id:
            prefix.append(f"export ORCHE_DISCORD_CHANNEL_ID={shlex.quote(validate_discord_channel_id(discord_channel_id))}")
        command = [
            "codex",
            "--enable",
            "codex_hooks",
            "--no-alt-screen",
            "-C",
            str(cwd),
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        prefix.append(f"exec {' '.join(shlex.quote(part) for part in command)}")
        return " && ".join(prefix)

    def native_launch_args(self, *, cwd: Path, cli_args: list[str] | tuple[str, ...]) -> list[str]:
        args = [str(value) for value in cli_args]
        command: list[str] = []
        if "--no-alt-screen" not in args:
            command.append("--no-alt-screen")
        if "-C" not in args:
            command.extend(["-C", str(cwd)])
        if "--dangerously-bypass-approvals-and-sandbox" not in args:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        command.extend(args)
        return command

    def matches_process(self, pane_command: str, descendant_commands: list[str]) -> bool:
        if pane_command == "codex":
            return True
        for proc in descendant_commands:
            lowered = proc.lower()
            if "codex" in lowered or "@openai/codex" in lowered:
                return True
        return False

    def capture_has_ready_surface(self, capture: str, cwd: Path) -> bool:
        lowered = capture.lower()
        has_brand = "openai codex" in lowered or "\ncodex" in lowered or " codex" in lowered
        has_context = str(cwd) in capture or any(hint.lower() in lowered for hint in READY_SURFACE_HINTS)
        return has_brand and has_context

    def submit_prompt(self, session: str, prompt: str, *, bridge: BridgeIO) -> None:
        if prompt:
            bridge.type(session, prompt)
            # Codex can drop an immediate Enter and leave the prompt staged but unsubmitted.
            time.sleep(codex_submit_settle_seconds(prompt))
        bridge.keys(session, ["Enter"])

    def extract_completion_summary(self, capture: str, prompt: str) -> str:
        return _extract_codex_completion_summary(capture, prompt)

    def extract_failure_summary(self, capture: str, prompt: str) -> str:
        return _extract_codex_failure_summary(capture, prompt)

    def cleanup_runtime(self, runtime: AgentRuntime) -> None:
        if runtime.home:
            remove_runtime_home(runtime.home)


PLUGINS = [CodexAgent()]
