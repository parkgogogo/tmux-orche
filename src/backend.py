from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None

from notify_hook import NOTIFY_DISCORD_SH
from paths import config_path, ensure_directories, history_dir, locks_dir, meta_dir, orch_log_path

BACKEND = "smux"
TMUX_SESSION = "orche-smux"
DEFAULT_CAPTURE_LINES = 200
STARTUP_TIMEOUT = 90.0
READY_STREAK_REQUIRED = 2
CONFIG_COMMENT = (
    "orche runtime config. session is the orche codex session label; "
    "discord_session is the Discord/OpenClaw session key used for notify routing."
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
TMUX_BRIDGE_FALLBACK = Path.home() / ".smux" / "bin" / "tmux-bridge"
DEFAULT_CODEX_HOME_ROOT = Path(tempfile.gettempdir())
DEFAULT_CODEX_SOURCE_HOME = Path.home() / ".codex"
MANAGED_CODEX_RUNTIME_DIRS = {".tmp", "log", "shell_snapshots", "tmp"}
MANAGED_CODEX_RUNTIME_FILE_GLOBS = ("history.jsonl", "logs_*.sqlite*", "state_*.sqlite*")
TOML_TABLE_HEADER_RE = re.compile(r"^\s*\[\[?.*\]\]?\s*$")
TOML_NOTIFY_KEY_RE = re.compile(r"^\s*notify\s*=")
TOML_PROJECT_HEADER_RE = re.compile(r"^\s*\[projects\.(.+)\]\s*$")
TOML_TRUST_LEVEL_RE = re.compile(r"^\s*trust_level\s*=")
SOURCE_CONFIG_LOCK_NAME = "codex-source-config"
SOURCE_CONFIG_BACKUP_SUFFIX = ".orche.bak"
CONFIG_KEY_MAP = {
    "discord.bot-token": "discord_bot_token",
    "discord.channel-id": "discord_channel_id",
    "discord.mention-user-id": "notify_mention_user_id",
    "discord.webhook-url": "discord_webhook_url",
    "notify.enabled": "notify_enabled",
}


class OrcheError(RuntimeError):
    pass


def shorten(text: object, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", str(text)).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def longest_common_prefix(before: str, after: str) -> int:
    limit = min(len(before), len(after))
    index = 0
    while index < limit and before[index] == after[index]:
        index += 1
    return index


def turn_delta(before: str, after: str) -> str:
    if before and after and before in after:
        return after.split(before, 1)[1]
    return after[longest_common_prefix(before, after) :]


def extract_summary_candidate(text: str, *, prompt: str = "") -> str:
    lines: List[str] = []
    prompt_inline = compact_text(prompt)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("```"):
            continue
        if line.startswith(("╭", "╰", "│", "› ", "└ ")):
            continue
        if line.startswith("• "):
            line = line[2:].strip()
            if not line:
                continue
        if re.match(r"^[─━]{6,}$", line):
            continue
        if re.match(r"^[\W_─━]{20,}$", line):
            continue
        if line.startswith(("Tip:", "Command:", "Chunk ID:", "Wall time:", "Output:")):
            continue
        if line in {"Explored", "Ran", "Read", "List", "Updated Plan"}:
            continue
        if line.startswith(("Explored", "Ran ", "Read ", "List ", "Edited ")):
            continue
        if line.startswith(("OpenAI Codex", "dnq@", "^C")):
            continue
        if "gpt-" in line and "% left" in line:
            continue
        if line.startswith(("session:", "cwd:")):
            continue
        if prompt_inline and compact_text(line) == prompt_inline:
            continue
        if prompt_inline and compact_text(line).endswith(prompt_inline):
            continue
        line = compact_text(line.replace("`", ""))
        if not line:
            continue
        lines.append(line)
    return lines[-1] if lines else ""


def log_event(event: str, **fields: Any) -> None:
    ensure_directories()
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pid": os.getpid(),
        "event": event,
        **fields,
    }
    try:
        with orch_log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def log_exception(event: str, exc: BaseException, **fields: Any) -> None:
    log_event(
        event,
        error_type=type(exc).__name__,
        error=str(exc),
        traceback=traceback.format_exc(),
        **fields,
    )


def slugify(text: str) -> str:
    out: List[str] = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in ("-", "_", "/", "."):
            out.append("-")
    value = "".join(out)
    while "--" in value:
        value = value.replace("--", "-")
    return value.strip("-") or "root"


def repo_name(cwd: Path) -> str:
    return slugify(cwd.resolve().name)


def normalize_codex_home(codex_home: Optional[Union[Path, str]]) -> str:
    if codex_home in (None, ""):
        return ""
    return str(Path(str(codex_home)).expanduser().resolve())


def default_session_name(cwd: Path, agent: str, purpose: str = "main") -> str:
    return f"{repo_name(cwd)}-{slugify(agent)}-{slugify(purpose)}"


def window_name(session: str) -> str:
    return f"orche-{slugify(session)}"


def session_key(session: str) -> str:
    return slugify(session)


def default_codex_home_path(session: str) -> Path:
    return DEFAULT_CODEX_HOME_ROOT / f"orche-codex-{session_key(session)}"


def default_notify_hook_path(codex_home: Path) -> Path:
    return codex_home / "hooks" / "discord-turn-notify.sh"


def source_codex_config_path() -> Path:
    return DEFAULT_CODEX_SOURCE_HOME / "config.toml"


def source_codex_config_backup_path() -> Path:
    return source_codex_config_path().with_name(source_codex_config_path().name + SOURCE_CONFIG_BACKUP_SUFFIX)


def render_notify_command(hook_path: Path, *, session: str, discord_channel_id: Optional[str]) -> str:
    values = ["/bin/bash", str(hook_path), "--session", session]
    if discord_channel_id:
        values.extend(["--channel-id", validate_discord_channel_id(discord_channel_id)])
    return "notify = [" + ", ".join(json.dumps(value) for value in values) + "]"


def write_notify_hook(hook_path: Path) -> None:
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(NOTIFY_DISCORD_SH, encoding="utf-8")
    hook_path.chmod(0o755)


def strip_notify_assignments(lines: List[str]) -> List[str]:
    cleaned: List[str] = []
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


def validate_toml_document(content: str, *, label: str) -> None:
    if tomllib is None:
        return
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise OrcheError(f"Refusing to write invalid TOML for {label}: {exc}") from exc


def write_text_atomically(path: Path, content: str, *, backup_path: Optional[Path] = None) -> None:
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


def _project_header_path(line: str) -> Optional[str]:
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


def upsert_top_level_notify(content: str, notify_line: str) -> str:
    lines = strip_notify_assignments(content.splitlines(keepends=True))
    first_table_index = next((index for index, line in enumerate(lines) if TOML_TABLE_HEADER_RE.match(line)), len(lines))
    prefix = lines[:first_table_index]
    suffix = lines[first_table_index:]
    while prefix and not prefix[-1].strip():
        prefix.pop()
    while suffix and not suffix[0].strip():
        suffix.pop(0)
    updated: List[str] = list(prefix)
    if updated and not updated[-1].endswith("\n"):
        updated[-1] += "\n"
    if updated:
        updated.append("\n")
    updated.append(notify_line + "\n")
    if suffix:
        updated.append("\n")
        updated.extend(suffix)
    return "".join(updated)


@contextlib.contextmanager
def source_config_lock(*, timeout: float = 5.0):
    ensure_directories()
    path = locks_dir() / f"{SOURCE_CONFIG_LOCK_NAME}.lock"
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() > deadline:
                raise OrcheError("Timed out waiting for Codex source config lock")
            time.sleep(0.1)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
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


def prune_managed_codex_home(codex_home: Path) -> None:
    for name in MANAGED_CODEX_RUNTIME_DIRS:
        shutil.rmtree(codex_home / name, ignore_errors=True)
    for pattern in MANAGED_CODEX_RUNTIME_FILE_GLOBS:
        for path in codex_home.glob(pattern):
            with contextlib.suppress(OSError):
                path.unlink()


def remove_managed_codex_home(codex_home: str) -> None:
    normalized = Path(normalize_codex_home(codex_home))
    candidates: List[Path] = []
    for candidate in (normalized, normalized.resolve()):
        if candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        for _ in range(3):
            if not candidate.exists():
                break
            shutil.rmtree(candidate, ignore_errors=True)
            if not candidate.exists():
                break
            time.sleep(0.1)


def rewrite_codex_config(
    codex_home: Path,
    *,
    session: str,
    cwd: Path,
    discord_channel_id: Optional[str],
) -> None:
    config_toml_path = codex_home / "config.toml"
    base_content = sync_trust_to_source_config(cwd)
    notify_line = render_notify_command(
        default_notify_hook_path(codex_home),
        session=session,
        discord_channel_id=discord_channel_id,
    )
    updated = upsert_top_level_notify(base_content, notify_line)
    validate_toml_document(updated, label=str(config_toml_path))
    write_text_atomically(config_toml_path, updated)


def ensure_managed_codex_home(session: str, *, cwd: Path, discord_channel_id: Optional[str]) -> Path:
    target = default_codex_home_path(session)
    if not target.exists():
        if DEFAULT_CODEX_SOURCE_HOME.exists():
            shutil.copytree(DEFAULT_CODEX_SOURCE_HOME, target)
        else:
            target.mkdir(parents=True, exist_ok=True)
    prune_managed_codex_home(target)
    write_notify_hook(default_notify_hook_path(target))
    rewrite_codex_config(target, session=session, cwd=cwd, discord_channel_id=discord_channel_id)
    return target.resolve()


def history_path(session: str) -> Path:
    return history_dir() / f"{session_key(session)}.jsonl"


def meta_path(session: str) -> Path:
    return meta_dir() / f"{session_key(session)}.json"


def lock_path(session: str) -> Path:
    return locks_dir() / f"{session_key(session)}.lock"


def run(
    cmd: List[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        check=check,
        capture_output=capture,
        cwd=None if cwd is None else str(cwd),
        env=env,
    )


def require_tmux() -> None:
    if shutil.which("tmux"):
        return
    raise OrcheError("tmux is not installed; smux backend requires tmux")


def require_tmux_bridge() -> str:
    candidate = shutil.which("tmux-bridge")
    if candidate:
        return candidate
    if TMUX_BRIDGE_FALLBACK.exists():
        return str(TMUX_BRIDGE_FALLBACK)
    raise OrcheError("tmux-bridge is not installed; run the official smux installer first")


def tmux(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    require_tmux()
    return run(["tmux", *args], check=check, capture=capture)


def tmux_bridge(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    bridge = require_tmux_bridge()
    return run([bridge, *args], check=check, capture=capture)


def pane_exists(pane_id: str) -> bool:
    result = tmux("display-message", "-p", "-t", pane_id, "#{pane_id}", check=False, capture=True)
    return result.returncode == 0 and result.stdout.strip() == pane_id


def tmux_session_exists() -> bool:
    result = tmux("has-session", "-t", TMUX_SESSION, check=False, capture=True)
    return result.returncode == 0


def list_windows() -> List[Dict[str, str]]:
    if not tmux_session_exists():
        return []
    result = tmux(
        "list-windows",
        "-t",
        TMUX_SESSION,
        "-F",
        "#{window_id}\t#{window_name}",
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return []
    windows: List[Dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            windows.append({"window_id": parts[0], "window_name": parts[1]})
    return windows


def find_window(name: str) -> Optional[Dict[str, str]]:
    for window in list_windows():
        if window["window_name"] == name:
            return window
    return None


def list_panes(target: Optional[str] = None) -> List[Dict[str, str]]:
    args = ["list-panes"]
    if target:
        args.extend(["-t", target])
    else:
        if not tmux_session_exists():
            return []
        args.extend(["-t", TMUX_SESSION])
    args.extend(
        [
            "-F",
            "#{pane_id}\t#{window_id}\t#{window_name}\t#{pane_dead}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_title}",
        ]
    )
    result = tmux(*args, check=False, capture=True)
    if result.returncode != 0:
        return []
    panes: List[Dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 8:
            continue
        panes.append(
            {
                "pane_id": parts[0],
                "window_id": parts[1],
                "window_name": parts[2],
                "pane_dead": parts[3],
                "pane_pid": parts[4],
                "pane_current_command": parts[5],
                "pane_current_path": parts[6],
                "pane_title": parts[7],
            }
        )
    return panes


def get_pane_info(pane_id: str) -> Optional[Dict[str, str]]:
    if not pane_exists(pane_id):
        return None
    panes = list_panes(pane_id)
    return panes[0] if panes else None


def read_pane(pane_id: str, lines: int = DEFAULT_CAPTURE_LINES) -> str:
    start = f"-{max(lines, 1)}"
    result = tmux("capture-pane", "-p", "-J", "-t", pane_id, "-S", start, check=False, capture=True)
    if result.returncode != 0:
        return ""
    return "\n".join(result.stdout.splitlines()[-lines:])


def save_meta(session: str, meta: Dict[str, Any]) -> None:
    ensure_directories()
    meta_path(session).write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_meta(session: str) -> Dict[str, Any]:
    path = meta_path(session)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def remove_meta(session: str) -> None:
    path = meta_path(session)
    if path.exists():
        path.unlink()


def append_history_entry(session: str, entry: Dict[str, Any]) -> None:
    ensure_directories()
    path = history_path(session)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_history_entries(session: str) -> List[Dict[str, Any]]:
    path = history_path(session)
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


def list_sessions() -> List[Dict[str, Any]]:
    ensure_directories()
    sessions: List[Dict[str, Any]] = []
    for path in sorted(meta_dir().glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        session = str(payload.get("session") or path.stem).strip()
        if not session:
            continue
        payload["session"] = session
        sessions.append(payload)
    sessions.sort(key=lambda item: str(item.get("session") or ""))
    return sessions


def load_config() -> Dict[str, Any]:
    ensure_directories()
    default = {
        "_comment": CONFIG_COMMENT,
        "codex_turn_complete_channel_id": "",
        "discord_bot_token": "",
        "discord_channel_id": "",
        "discord_webhook_url": "",
        "notify_enabled": True,
        "session": "",
        "discord_session": "",
        "codex_home": "",
        "codex_home_managed": False,
    }
    path = config_path()
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default
    if not isinstance(data, dict):
        return default
    merged = dict(default)
    merged.update(data)
    return merged


def save_config(config: Dict[str, Any]) -> None:
    ensure_directories()
    payload = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    write_text_atomically(config_path(), payload)


def validate_discord_channel_id(value: str) -> str:
    channel_id = re.sub(r"\s+", "", value or "")
    if not channel_id or not channel_id.isdigit():
        raise OrcheError("--discord-channel-id must be a numeric Discord channel ID")
    return channel_id


def derive_discord_session(channel_id: str) -> str:
    return f"agent:main:discord:channel:{channel_id}"


def config_key_field(key: str) -> str:
    field = CONFIG_KEY_MAP.get(key)
    if field is None:
        supported = ", ".join(sorted(CONFIG_KEY_MAP))
        raise OrcheError(f"Unsupported config key: {key}. Supported keys: {supported}")
    return field


def get_config_value(key: str) -> str:
    config = load_config()
    if key == "discord.channel-id":
        value = str(config.get("discord_channel_id") or config.get("codex_turn_complete_channel_id") or "").strip()
        if value:
            return validate_discord_channel_id(value)
        return ""
    field = config_key_field(key)
    value = config.get(field)
    if key == "notify.enabled":
        return "true" if bool(value) else "false"
    return "" if value is None else str(value)


def set_config_value(key: str, value: str) -> Dict[str, Any]:
    config = load_config()
    field = config_key_field(key)
    normalized = value
    if key == "discord.channel-id":
        normalized = validate_discord_channel_id(value)
        config["codex_turn_complete_channel_id"] = normalized
        config["discord_channel_id"] = normalized
        config["discord_session"] = derive_discord_session(normalized)
    elif key == "notify.enabled":
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            normalized = True
        elif lowered in {"0", "false", "no", "off"}:
            normalized = False
        else:
            raise OrcheError("notify.enabled must be one of: true, false, 1, 0, yes, no, on, off")
    else:
        normalized = value.strip()
    config[field] = normalized
    save_config(config)
    return config


def list_config_values() -> Dict[str, str]:
    return {key: get_config_value(key) for key in sorted(CONFIG_KEY_MAP)}


def update_runtime_config(
    *,
    session: str,
    cwd: Path,
    agent: str,
    pane_id: str,
    codex_home: Optional[str] = None,
    codex_home_managed: Optional[bool] = None,
    discord_channel_id: Optional[str] = None,
    discord_session: Optional[str] = None,
) -> Dict[str, Any]:
    config = load_config()
    config["_comment"] = CONFIG_COMMENT
    config.pop("orch_session", None)
    config.pop("parent_session_key", None)
    if discord_channel_id:
        normalized_channel_id = validate_discord_channel_id(discord_channel_id)
        config["codex_turn_complete_channel_id"] = normalized_channel_id
        config["discord_channel_id"] = normalized_channel_id
    config["session"] = session
    if discord_session:
        config["discord_session"] = discord_session
    elif not config.get("discord_session") and str(config.get("codex_turn_complete_channel_id") or "").isdigit():
        config["discord_session"] = derive_discord_session(str(config["codex_turn_complete_channel_id"]))
    config["cwd"] = str(cwd)
    config["agent"] = agent
    config["pane_id"] = pane_id
    config["codex_home"] = normalize_codex_home(codex_home)
    if codex_home_managed is not None:
        config["codex_home_managed"] = bool(codex_home_managed)
    config["updated_at"] = time.time()
    save_config(config)
    return config


@contextlib.contextmanager
def session_lock(session: str, *, timeout: float = 5.0):
    ensure_directories()
    path = lock_path(session)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() > deadline:
                raise OrcheError(f"Timed out waiting for session lock: {session}")
            time.sleep(0.1)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def bridge_name_pane(pane_id: str, session: str) -> None:
    tmux_bridge("name", pane_id, session, check=True, capture=True)


def bridge_resolve(session: str) -> Optional[str]:
    result = tmux_bridge("resolve", session, check=False, capture=True)
    if result.returncode != 0:
        return None
    pane_id = result.stdout.strip()
    return pane_id or None


def bridge_read(session: str, lines: int = DEFAULT_CAPTURE_LINES) -> str:
    result = tmux_bridge("read", session, str(lines), check=True, capture=True)
    return result.stdout.rstrip("\n")


def bridge_type(session: str, text: str) -> None:
    if not text:
        return
    tmux_bridge("read", session, "1", check=True, capture=True)
    tmux_bridge("type", session, text, check=True, capture=True)


def bridge_keys(session: str, keys: Union[Iterable[str], str]) -> None:
    values = [keys] if isinstance(keys, str) else list(keys)
    if not values:
        return
    tmux_bridge("read", session, "1", check=True, capture=True)
    tmux_bridge("keys", session, *values, check=True, capture=True)


def ensure_window(name: str, cwd: Path) -> Dict[str, str]:
    window = find_window(name)
    if window is not None:
        return window
    if tmux_session_exists():
        tmux("new-window", "-d", "-t", TMUX_SESSION, "-n", name, "-c", str(cwd), check=True, capture=True)
    else:
        tmux("new-session", "-d", "-s", TMUX_SESSION, "-n", name, "-c", str(cwd), check=True, capture=True)
    created = find_window(name)
    if created is None:
        raise OrcheError(f"Failed to create tmux window for {name}")
    return created


def normalize_pane(session: str, cwd: Path, pane: Dict[str, str]) -> str:
    pane_id = pane["pane_id"]
    if pane.get("pane_dead") == "1":
        tmux("respawn-pane", "-k", "-t", pane_id, "-c", str(cwd), check=True, capture=True)
    tmux("select-pane", "-t", pane_id, "-T", session, check=False, capture=True)
    bridge_name_pane(pane_id, session)
    return pane_id


def ensure_pane(session: str, cwd: Path, agent: str) -> str:
    cwd = cwd.resolve()
    with session_lock(session):
        meta = load_meta(session)
        pane_id = str(meta.get("pane_id") or "")
        if pane_id and pane_exists(pane_id):
            info = get_pane_info(pane_id)
            if info is not None:
                pane_id = normalize_pane(session, cwd, info)
                meta.update(
                    {
                        "backend": BACKEND,
                        "session": session,
                        "cwd": str(cwd),
                        "agent": agent,
                        "pane_id": pane_id,
                        "window_id": info["window_id"],
                        "window_name": info["window_name"],
                        "last_seen_at": time.time(),
                    }
                )
                save_meta(session, meta)
                return pane_id

        window = ensure_window(window_name(session), cwd)
        panes = list_panes(window["window_id"])
        if not panes:
            tmux("split-window", "-d", "-t", window["window_id"], "-c", str(cwd), check=True, capture=True)
            panes = list_panes(window["window_id"])
        if not panes:
            raise OrcheError(f"Failed to create tmux pane for {session}")
        pane = panes[0]
        pane_id = normalize_pane(session, cwd, pane)
        meta.update(
            {
                "backend": BACKEND,
                "session": session,
                "cwd": str(cwd),
                "agent": agent,
                "pane_id": pane_id,
                "window_id": pane["window_id"],
                "window_name": pane["window_name"],
                "last_seen_at": time.time(),
            }
        )
        save_meta(session, meta)
        return pane_id


def process_descendants(root_pid: int) -> List[str]:
    result = run(["ps", "-axo", "pid=,ppid=,command="], check=False, capture=True)
    if result.returncode != 0:
        return []
    children: Dict[int, List[Tuple[int, str]]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append((pid, parts[2]))
    commands: List[str] = []
    stack = [root_pid]
    seen: Set[int] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for child_pid, command in children.get(current, []):
            commands.append(command)
            stack.append(child_pid)
    return commands


def is_codex_running(pane_id: str) -> bool:
    info = get_pane_info(pane_id)
    if info is None or info.get("pane_dead") == "1":
        return False
    command = (info.get("pane_current_command") or "").lower()
    if command == "codex":
        return True
    try:
        pane_pid = int(info.get("pane_pid") or "0")
    except ValueError:
        return False
    for proc in process_descendants(pane_pid):
        lowered = proc.lower()
        if "codex" in lowered or "@openai/codex" in lowered:
            return True
    return False


def build_codex_command(
    cwd: Path,
    *,
    approve_all: bool,
    codex_home: Optional[str] = None,
    session: Optional[str] = None,
    discord_channel_id: Optional[str] = None,
) -> str:
    _ = approve_all
    prefix: List[str] = [f"cd {shlex.quote(str(cwd))}"]
    normalized_codex_home = normalize_codex_home(codex_home)
    if normalized_codex_home:
        prefix.append(f"mkdir -p {shlex.quote(normalized_codex_home)}")
        prefix.append(f"export CODEX_HOME={shlex.quote(normalized_codex_home)}")
    if session:
        prefix.append(f"export ORCHE_SESSION={shlex.quote(session)}")
    if discord_channel_id:
        prefix.append(f"export ORCHE_DISCORD_CHANNEL_ID={shlex.quote(validate_discord_channel_id(discord_channel_id))}")
    command = ["codex", "--no-alt-screen", "-C", str(cwd)]
    command.append("--dangerously-bypass-approvals-and-sandbox")
    prefix.append(f"exec {' '.join(shlex.quote(part) for part in command)}")
    return " && ".join(prefix)


def capture_has_ready_surface(capture: str, cwd: Path) -> bool:
    lowered = capture.lower()
    has_brand = "openai codex" in lowered or "\ncodex" in lowered or " codex" in lowered
    has_context = str(cwd) in capture or any(hint.lower() in lowered for hint in READY_SURFACE_HINTS)
    return has_brand and has_context


def wait_for_codex_ready(pane_id: str, cwd: Path, *, timeout: float = STARTUP_TIMEOUT) -> str:
    deadline = time.time() + timeout
    ready_streak = 0
    while time.time() <= deadline:
        capture = read_pane(pane_id, DEFAULT_CAPTURE_LINES)
        if "Login with ChatGPT" in capture or "Please login" in capture:
            raise OrcheError("Codex is not logged in inside the tmux pane")
        running = is_codex_running(pane_id)
        ready_candidate = running and capture_has_ready_surface(capture, cwd)
        ready_streak = ready_streak + 1 if ready_candidate else 0
        if ready_streak >= READY_STREAK_REQUIRED:
            return pane_id
        time.sleep(1.0)
    raise OrcheError(f"Timed out waiting for Codex to become ready in {pane_id}")


def ensure_codex_running(
    session: str,
    cwd: Path,
    pane_id: str,
    *,
    approve_all: bool = False,
    codex_home: Optional[str] = None,
    discord_channel_id: Optional[str] = None,
) -> str:
    if is_codex_running(pane_id):
        return pane_id
    approve_all = True
    info = get_pane_info(pane_id)
    if info is None:
        raise OrcheError(f"Pane disappeared before Codex launch: {pane_id}")
    if info.get("pane_dead") == "1":
        tmux("respawn-pane", "-k", "-t", pane_id, "-c", str(cwd), check=True, capture=True)
    else:
        tmux("send-keys", "-t", pane_id, "C-c", check=False, capture=True)
        time.sleep(0.2)
    tmux(
        "send-keys",
        "-t",
        pane_id,
        "-l",
        build_codex_command(
            cwd,
            approve_all=approve_all,
            codex_home=codex_home,
            session=session,
            discord_channel_id=discord_channel_id,
        ),
        check=True,
        capture=True,
    )
    tmux("send-keys", "-t", pane_id, "Enter", check=True, capture=True)
    pane_id = wait_for_codex_ready(pane_id, cwd)
    bridge_name_pane(pane_id, session)
    meta = load_meta(session)
    meta.update(
        {
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "pane_id": pane_id,
            "codex_home": normalize_codex_home(codex_home),
            "codex_started_at": time.time(),
            "codex_approve_all": approve_all,
            "last_seen_at": time.time(),
        }
    )
    save_meta(session, meta)
    return pane_id


def append_action_history(session: str, cwd: Path, agent: str, action: str, **fields: Any) -> None:
    append_history_entry(
        session,
        {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "agent": agent,
            "action": action,
            **fields,
        },
    )


def ensure_session(
    session: str,
    cwd: Path,
    agent: str,
    *,
    approve_all: bool = False,
    codex_home: Optional[str] = None,
    discord_channel_id: Optional[str] = None,
    discord_session: Optional[str] = None,
) -> str:
    cwd = cwd.resolve()
    existing_meta = load_meta(session)
    existing_cwd = Path(str(existing_meta.get("cwd") or "")).resolve() if existing_meta.get("cwd") else None
    if existing_cwd is not None and existing_cwd != cwd:
        raise OrcheError(
            f"Session {session} is already bound to cwd={existing_cwd}. "
            "Use the same --cwd or close the session and create a new one."
        )
    resolved_discord_channel_id = discord_channel_id or str(existing_meta.get("discord_channel_id") or "")
    resolved_discord_session = (
        discord_session
        or str(existing_meta.get("discord_session") or "")
        or (derive_discord_session(resolved_discord_channel_id) if resolved_discord_channel_id else "")
    )
    managed_codex_home = False
    if codex_home:
        resolved_codex_home = normalize_codex_home(codex_home)
    elif existing_meta.get("codex_home"):
        resolved_codex_home = normalize_codex_home(str(existing_meta.get("codex_home") or ""))
        managed_codex_home = bool(existing_meta.get("codex_home_managed"))
    else:
        resolved_codex_home = str(
            ensure_managed_codex_home(
                session,
                cwd=cwd,
                discord_channel_id=resolved_discord_channel_id,
            )
        )
        managed_codex_home = True
    if managed_codex_home:
        resolved_codex_home = str(
            ensure_managed_codex_home(
                session,
                cwd=cwd,
                discord_channel_id=resolved_discord_channel_id,
            )
        )
    existing_codex_home = normalize_codex_home(str(existing_meta.get("codex_home") or ""))
    if existing_codex_home and resolved_codex_home and existing_codex_home != resolved_codex_home:
        raise OrcheError(
            f"Session {session} is already bound to codex_home={existing_codex_home}. "
            "Use the same --codex-home or close the session and create a new one."
        )
    pane_id = ensure_pane(session, cwd, agent)
    pane_id = ensure_codex_running(
        session,
        cwd,
        pane_id,
        approve_all=approve_all,
        codex_home=resolved_codex_home,
        discord_channel_id=resolved_discord_channel_id,
    )
    meta = load_meta(session)
    meta.update(
        {
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "agent": agent,
            "pane_id": pane_id,
            "codex_home": resolved_codex_home,
            "codex_home_managed": managed_codex_home,
            "discord_channel_id": resolved_discord_channel_id,
            "discord_session": resolved_discord_session,
            "last_seen_at": time.time(),
        }
    )
    save_meta(session, meta)
    update_runtime_config(
        session=session,
        cwd=cwd,
        agent=agent,
        pane_id=pane_id,
        codex_home=resolved_codex_home,
        codex_home_managed=managed_codex_home,
        discord_channel_id=resolved_discord_channel_id,
        discord_session=resolved_discord_session,
    )
    return pane_id


def send_prompt(
    session: str,
    cwd: Path,
    agent: str,
    prompt: str,
    *,
    approve_all: bool = False,
    discord_channel_id: Optional[str] = None,
    discord_session: Optional[str] = None,
) -> str:
    pane_id = ensure_session(
        session,
        cwd,
        agent,
        approve_all=approve_all,
        discord_channel_id=discord_channel_id,
        discord_session=discord_session,
    )
    before_capture = read_pane(pane_id, DEFAULT_CAPTURE_LINES)
    meta = load_meta(session)
    meta["pending_turn"] = {
        "turn_id": uuid.uuid4().hex[:12],
        "prompt": prompt,
        "before_capture": before_capture,
        "submitted_at": time.time(),
        "pane_id": pane_id,
    }
    save_meta(session, meta)
    bridge_type(session, prompt)
    bridge_keys(session, ["Enter"])
    append_action_history(session, cwd, agent, "prompt", prompt=prompt, pane_id=pane_id)
    return pane_id


def latest_turn_summary(session: str) -> str:
    meta = load_meta(session)
    pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else None
    if pending_turn:
        pane_id = str((bridge_resolve(session) or pending_turn.get("pane_id") or meta.get("pane_id") or "")).strip()
        capture = read_pane(pane_id, DEFAULT_CAPTURE_LINES) if pane_id else ""
        before_capture = str(pending_turn.get("before_capture") or "")
        delta = turn_delta(before_capture, capture) if capture else ""
        summary = extract_summary_candidate(delta, prompt=str(pending_turn.get("prompt") or ""))
        if summary:
            completed = dict(pending_turn)
            completed["summary"] = summary
            completed["completed_at"] = time.time()
            meta["last_completed_turn"] = completed
            meta.pop("pending_turn", None)
            save_meta(session, meta)
            return summary
        save_meta(session, meta)
        return ""
    last_completed = meta.get("last_completed_turn") if isinstance(meta.get("last_completed_turn"), dict) else None
    if last_completed:
        return str(last_completed.get("summary") or "")
    return ""


def build_status(session: str) -> Dict[str, Any]:
    meta = load_meta(session)
    pane_id = bridge_resolve(session) or str(meta.get("pane_id") or "")
    info = get_pane_info(pane_id) if pane_id else None
    cwd = str(meta.get("cwd") or (info or {}).get("pane_current_path") or "-")
    agent = str(meta.get("agent") or "codex")
    discord_session = str(meta.get("discord_session") or "")
    discord_channel_id = str(meta.get("discord_channel_id") or "").strip()
    if not discord_session and discord_channel_id.isdigit():
        discord_session = derive_discord_session(discord_channel_id)
    return {
        "backend": BACKEND,
        "session": session,
        "cwd": cwd,
        "agent": agent,
        "codex_home": str(meta.get("codex_home") or ""),
        "codex_home_managed": bool(meta.get("codex_home_managed")),
        "pane_id": pane_id or "-",
        "window_name": (info or {}).get("window_name", meta.get("window_name", "-")),
        "codex_running": bool(pane_id and is_codex_running(pane_id)),
        "pane_exists": bool(pane_id and pane_exists(pane_id)),
        "discord_session": discord_session,
    }


def resolve_session_context(
    *,
    session: str,
    require_existing: bool = False,
    require_cwd_agent: bool = False,
) -> Tuple[Optional[Path], Optional[str], Dict[str, Any]]:
    meta = load_meta(session)
    cwd = Path(meta["cwd"]).resolve() if meta.get("cwd") else None
    agent = str(meta.get("agent")) if meta.get("agent") else None
    if require_existing and not meta:
        raise OrcheError(f"Unknown session: {session}")
    if require_cwd_agent and (cwd is None or agent is None):
        raise OrcheError(f"Session {session} is missing cwd/agent context; create it with session-new first")
    return cwd, agent, meta


def cancel_session(session: str) -> str:
    bridge_keys(session, ["C-c"])
    return bridge_resolve(session) or "-"


def close_session(session: str) -> str:
    meta = load_meta(session)
    pane_id = bridge_resolve(session) or str(meta.get("pane_id") or "")
    if pane_id and pane_exists(pane_id):
        info = get_pane_info(pane_id)
        if info is not None:
            tmux("kill-window", "-t", info["window_id"], check=False, capture=True)
    if bool(meta.get("codex_home_managed")):
        codex_home = normalize_codex_home(str(meta.get("codex_home") or ""))
        if codex_home:
            remove_managed_codex_home(codex_home)
    config = load_config()
    if str(config.get("session") or "") == session:
        config["session"] = ""
        config["cwd"] = ""
        config["agent"] = ""
        config["pane_id"] = ""
        config["codex_home"] = ""
        config["codex_home_managed"] = False
        config["updated_at"] = time.time()
        save_config(config)
    remove_meta(session)
    return pane_id or "-"
