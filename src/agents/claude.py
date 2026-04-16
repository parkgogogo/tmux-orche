from __future__ import annotations

import contextlib
import importlib
import json
import re
import shlex
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

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

from .base import AgentConfig, AgentPlugin, AgentRuntime, BridgeIO
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
    "Claude Code",
    "permission mode",
    "/help",
    "shift+tab",
    "esc to interrupt",
)
SOURCE_CONFIG_LOCK_NAME = "claude-source-config"
SOURCE_CONFIG_BACKUP_SUFFIX = ".orche.bak"
DEFAULT_CLAUDE_COMMAND = "claude"
DEFAULT_CLAUDE_SOURCE_CONFIG_PATH = Path.home() / ".claude.json"
DEFAULT_CLAUDE_SOURCE_HOME = Path.home() / ".claude"
CLAUDE_SUBMIT_SETTLE_SECONDS = 0.2


def _compact_prompt_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


def _is_claude_separator(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and all(ch == "─" for ch in stripped)


def _find_claude_prompt_block(lines: list[str], prompt: str) -> tuple[int, int] | None:
    prompt_inline = _compact_prompt_text(prompt)
    if not prompt_inline:
        return None
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped.startswith("❯ "):
            continue
        parts = [stripped[2:].strip()]
        end_index = index
        cursor = index + 1
        while cursor < len(lines):
            continuation = lines[cursor].strip()
            if not continuation or continuation.startswith(("❯ ", "⏺ ", "⎿ ")):
                break
            if _is_claude_separator(continuation):
                break
            parts.append(continuation)
            end_index = cursor
            cursor += 1
        rendered_prompt = _compact_prompt_text(" ".join(parts))
        if rendered_prompt and (
            rendered_prompt in prompt_inline or prompt_inline in rendered_prompt
        ):
            return index, end_index
    return None


def _extract_claude_completion_summary(capture: str, prompt: str) -> str:
    lines = capture.splitlines()
    prompt_block = _find_claude_prompt_block(lines, prompt)
    if prompt_block is None:
        return ""
    _prompt_start, prompt_end = prompt_block
    next_prompt_index = _find_next_claude_prompt(lines, prompt_end + 1)
    if next_prompt_index is None:
        return ""
    summaries: list[str] = []
    current_block: list[str] = []
    for raw_line in lines[prompt_end + 1 : next_prompt_index]:
        stripped = raw_line.strip()
        if not stripped:
            if current_block:
                summaries.append(_compact_prompt_text(" ".join(current_block)))
                current_block = []
            continue
        if stripped.startswith("❯"):
            break
        if _is_claude_separator(stripped):
            continue
        if stripped.startswith("⎿ "):
            continue
        if stripped.startswith("⏺ "):
            if current_block:
                summaries.append(_compact_prompt_text(" ".join(current_block)))
            current_block = [stripped[2:].strip()]
            continue
        if not current_block:
            continue
        current_block.append(stripped)
    if current_block:
        summaries.append(_compact_prompt_text(" ".join(current_block)))
    cleaned = [
        summary for summary in summaries if summary and not re.match(r"^⏵⏵\s+", summary)
    ]
    return cleaned[-1] if cleaned else ""


def _find_next_claude_prompt(lines: list[str], start_index: int) -> int | None:
    for index in range(max(start_index, 0), len(lines)):
        stripped = lines[index].strip()
        if stripped == "❯" or stripped.startswith("❯ "):
            return index
    return None


def default_claude_home_path(
    session: str, runtime_home_root: Path | None = None
) -> Path:
    root = Path(runtime_home_root or DEFAULT_RUNTIME_HOME_ROOT)
    return root / f"orche-claude-{session_key(session)}"


def default_notify_hook_path(runtime_home: Path) -> Path:
    return runtime_home / "hooks" / "discord-turn-notify.sh"


def default_settings_path(runtime_home: Path) -> Path:
    return runtime_home / "settings.json"


def claude_command_tokens(command: str | None = None) -> list[str]:
    raw = str(command or DEFAULT_CLAUDE_COMMAND or "").strip() or "claude"
    tokens = [token for token in shlex.split(raw) if token]
    return tokens or ["claude"]


def claude_process_names(command: str | None = None) -> set[str]:
    primary = Path(claude_command_tokens(command)[0]).name.lower()
    names = {"claude", "claude-code", "node"}
    if primary:
        names.add(primary)
    return names


def source_claude_config_path(config_path: Path | None = None) -> Path:
    return Path(config_path or DEFAULT_CLAUDE_SOURCE_CONFIG_PATH).expanduser()


def source_claude_config_backup_path(config_path: Path | None = None) -> Path:
    resolved_config_path = source_claude_config_path(config_path)
    return resolved_config_path.with_name(
        resolved_config_path.name + SOURCE_CONFIG_BACKUP_SUFFIX
    )


def source_claude_home_path(home_path: Path | None = None) -> Path:
    return Path(home_path or DEFAULT_CLAUDE_SOURCE_HOME).expanduser()


def source_settings_path(home_path: Path | None = None) -> Path:
    return source_claude_home_path(home_path) / "settings.json"


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
            if time.time() > deadline:
                raise RuntimeError("Timed out waiting for Claude source config lock")
            time.sleep(0.1)
    try:
        fd.write(str(Path.cwd()))
        fd.flush()
        yield
    finally:
        fd.close()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = read_json_file(path)
    except (json.JSONDecodeError, JSONInputTooLargeError) as exc:
        raise RuntimeError(f"Refusing to write invalid JSON for {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Refusing to rewrite non-object Claude source config at {path}"
        )
    return payload


def sync_trust_to_source_config(
    cwd: Path, *, config_path: Path | None = None
) -> dict[str, object]:
    resolved_config_path = source_claude_config_path(config_path)
    target = str(cwd.resolve())
    with source_config_lock():
        original = _read_json_object(resolved_config_path)
        projects = original.get("projects")
        if projects is None:
            projects_dict: dict[str, object] = {}
        elif isinstance(projects, dict):
            projects_dict = dict(projects)
        else:
            raise RuntimeError(
                f"Refusing to rewrite invalid Claude projects config at {resolved_config_path}"
            )
        project_entry = projects_dict.get(target)
        if project_entry is None:
            project_payload: dict[str, object] = {}
        elif isinstance(project_entry, dict):
            project_payload = dict(project_entry)
        else:
            raise RuntimeError(
                f"Refusing to rewrite invalid Claude project entry for {target}"
            )
        if project_payload.get("hasTrustDialogAccepted") is True:
            return original
        project_payload["hasTrustDialogAccepted"] = True
        projects_dict[target] = project_payload
        updated = dict(original)
        updated["projects"] = projects_dict
        write_text_atomically(
            resolved_config_path,
            json.dumps(updated, indent=2, ensure_ascii=False) + "\n",
            backup_path=source_claude_config_backup_path(resolved_config_path),
        )
        return updated


def render_hook_command(
    hook_path: Path,
    *,
    session: str,
    discord_channel_id: str | None,
    status: str | None = None,
) -> str:
    parts = ["/bin/bash", str(hook_path), "--session", session]
    if discord_channel_id:
        parts.extend(["--channel-id", validate_discord_channel_id(discord_channel_id)])
    if status:
        parts.extend(["--status", status])
    return " ".join(shlex.quote(part) for part in parts)


def build_settings_payload(
    runtime_home: Path,
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
            default_notify_hook_path(runtime_home),
            session=session,
            discord_channel_id=discord_channel_id,
        ),
    }
    warning_hook = {
        "type": "command",
        "command": render_hook_command(
            default_notify_hook_path(runtime_home),
            session=session,
            discord_channel_id=discord_channel_id,
            status="warning",
        ),
    }
    raw_session_start_entries = hooks.get("SessionStart")
    session_start_entries = (
        list(raw_session_start_entries)
        if isinstance(raw_session_start_entries, list)
        else []
    )
    session_start_entries.append(
        {
            "matcher": "startup",
            "hooks": [command_hook],
        }
    )
    hooks["SessionStart"] = session_start_entries
    raw_prompt_submit_entries = hooks.get("UserPromptSubmit")
    prompt_submit_entries = (
        list(raw_prompt_submit_entries)
        if isinstance(raw_prompt_submit_entries, list)
        else []
    )
    prompt_submit_entries.append({"hooks": [command_hook]})
    hooks["UserPromptSubmit"] = prompt_submit_entries
    raw_notification_entries = hooks.get("Notification")
    notification_entries = (
        list(raw_notification_entries)
        if isinstance(raw_notification_entries, list)
        else []
    )
    notification_entries.append({"hooks": [warning_hook]})
    hooks["Notification"] = notification_entries
    raw_permission_request_entries = hooks.get("PermissionRequest")
    permission_request_entries = (
        list(raw_permission_request_entries)
        if isinstance(raw_permission_request_entries, list)
        else []
    )
    permission_request_entries.append({"hooks": [warning_hook]})
    hooks["PermissionRequest"] = permission_request_entries
    raw_stop_entries = hooks.get("Stop")
    stop_entries = list(raw_stop_entries) if isinstance(raw_stop_entries, list) else []
    stop_entries.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": render_hook_command(
                        default_notify_hook_path(runtime_home),
                        session=session,
                        discord_channel_id=discord_channel_id,
                    ),
                }
            ]
        }
    )
    hooks["Stop"] = stop_entries
    payload["hooks"] = hooks
    return payload


class ClaudeAgent(AgentPlugin):
    name = "claude"
    display_name = "Claude Code"
    runtime_label = "Claude settings"
    login_prompts = ("Please run /login", "Login required")

    def __init__(self, config: AgentConfig | None = None) -> None:
        super().__init__(config=config)
        runtime_home_root = self._config.get("runtime_home_root")
        claude_command = self._config.get("claude_command")
        claude_home_path = self._config.get("claude_home_path")
        claude_config_path = self._config.get("claude_config_path")
        self._runtime_home_root = Path(
            runtime_home_root or DEFAULT_RUNTIME_HOME_ROOT
        ).expanduser()
        self._claude_command = (
            str(claude_command or DEFAULT_CLAUDE_COMMAND).strip()
            or DEFAULT_CLAUDE_COMMAND
        )
        self._source_home_path = Path(
            claude_home_path or DEFAULT_CLAUDE_SOURCE_HOME
        ).expanduser()
        self._source_config_path = Path(
            claude_config_path or DEFAULT_CLAUDE_SOURCE_CONFIG_PATH
        ).expanduser()

    def ensure_managed_runtime(
        self,
        session: str,
        *,
        cwd: Path,
        discord_channel_id: str | None,
    ) -> AgentRuntime:
        sync_trust_to_source_config(cwd, config_path=self._source_config_path)
        target = default_claude_home_path(session, self._runtime_home_root)
        target.mkdir(parents=True, exist_ok=True)
        write_notify_hook(default_notify_hook_path(target))
        runtime_settings_path = default_settings_path(target)
        settings_payload = build_settings_payload(
            target,
            session=session,
            discord_channel_id=discord_channel_id,
            source_payload=_read_json_object(
                source_settings_path(self._source_home_path)
            ),
        )
        serialized_settings = (
            json.dumps(settings_payload, indent=2, ensure_ascii=False) + "\n"
        )
        write_text_atomically(
            runtime_settings_path,
            serialized_settings,
        )
        return AgentRuntime(
            home=str(target.resolve()), managed=True, label=self.runtime_label
        )

    def submit_prompt(self, session: str, prompt: str, *, bridge: BridgeIO) -> None:
        if prompt:
            bridge.type(session, prompt)
            time.sleep(CLAUDE_SUBMIT_SETTLE_SECONDS)
        bridge.keys(session, ["Enter"])

    def build_launch_command(
        self,
        *,
        cwd: Path,
        runtime: AgentRuntime,
        session: str,
        discord_channel_id: str | None,
    ) -> str:
        prefix = [f"cd {shlex.quote(str(cwd))}"]
        orche_shim = ensure_orche_shim()
        prefix.append(f"export ORCHE_BIN={shlex.quote(str(orche_shim))}")
        prefix.append(f"export PATH={shlex.quote(str(orche_shim.parent))}:$PATH")
        normalized_runtime_home = normalize_runtime_home(runtime.home)
        if normalized_runtime_home:
            prefix.append(f"mkdir -p {shlex.quote(normalized_runtime_home)}")
        if session:
            prefix.append(f"export ORCHE_SESSION={shlex.quote(session)}")
        if discord_channel_id:
            prefix.append(
                f"export ORCHE_DISCORD_CHANNEL_ID={shlex.quote(validate_discord_channel_id(discord_channel_id))}"
            )
        settings_path = default_settings_path(Path(normalized_runtime_home))
        command = [
            *self.command_tokens(),
            "--dangerously-skip-permissions",
            "--setting-sources",
            "user",
            "--settings",
            str(settings_path),
        ]
        prefix.append(f"exec {' '.join(shlex.quote(part) for part in command)}")
        return " && ".join(prefix)

    def command_tokens(self) -> list[str]:
        return claude_command_tokens(self._claude_command)

    def matches_process(
        self, pane_command: str, descendant_commands: Iterable[str]
    ) -> bool:
        process_names = claude_process_names(self._claude_command)
        if pane_command in process_names:
            return True
        for proc in descendant_commands:
            lowered = proc.lower()
            if re_matches_claude(lowered, process_names=process_names):
                return True
        return False

    def capture_has_ready_surface(self, capture: str, cwd: Path) -> bool:
        lowered = capture.lower()
        has_brand = (
            "claude code" in lowered or "\nclaude" in lowered or " claude" in lowered
        )
        has_context = str(cwd) in capture or any(
            hint in lowered for hint in READY_SURFACE_HINTS
        )
        return has_brand and has_context

    def extract_completion_summary(self, capture: str, prompt: str) -> str:
        return _extract_claude_completion_summary(capture, prompt)

    def cleanup_runtime(self, runtime: AgentRuntime) -> None:
        if runtime.home:
            remove_runtime_home(runtime.home)


def re_matches_claude(command: str, *, process_names: set[str] | None = None) -> bool:
    lowered = str(command or "").lower()
    if "claude" in lowered or "claude-code" in lowered:
        return True
    names = process_names or claude_process_names()
    return any(name != "node" and name in lowered for name in names)


PLUGINS = [ClaudeAgent()]
