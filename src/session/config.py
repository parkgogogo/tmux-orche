from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from agents.common import normalize_runtime_home, validate_discord_channel_id as common_validate_discord_channel_id, write_text_atomically
from json_utils import JSONInputTooLargeError, read_json_file
from paths import config_path, ensure_directories

from .meta import DEFAULT_MANAGED_SESSION_TTL_SECONDS, DEFAULT_MAX_INLINE_SESSIONS


CONFIG_COMMENT = "orche runtime config. session is the active orche agent session label; discord_session is the Discord/OpenClaw session key used for notify routing."
SUPPORTED_NOTIFY_PROVIDERS = ("discord", "telegram", "tmux-bridge")
CONFIG_KEY_MAP = {
    "claude.command": "claude_command",
    "claude.home-path": "claude_home_path",
    "claude.config-path": "claude_config_path",
    "discord.bot-token": "discord_bot_token",
    "discord.mention-user-id": "notify_mention_user_id",
    "discord.webhook-url": "discord_webhook_url",
    "inline.max-sessions": "max_inline_sessions",
    "managed.ttl-seconds": "managed_session_ttl_seconds",
    "notify.enabled": "notify_enabled",
    "telegram.bot-token": "telegram_bot_token",
}
DEFAULT_CLAUDE_COMMAND = "claude"


def validate_discord_channel_id(value: str, *, option_name: str = "--channel-id") -> str:
    try:
        return common_validate_discord_channel_id(value)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("--discord-channel-id"):
            message = message.replace("--discord-channel-id", option_name, 1)
        raise RuntimeError(message) from exc


def validate_notify_provider(value: str, *, option_name: str = "--notify-to") -> str:
    provider = str(value or "").strip()
    if not provider:
        raise RuntimeError(f"{option_name} is required")
    if provider not in SUPPORTED_NOTIFY_PROVIDERS:
        raise RuntimeError(f"{option_name} must be one of: {', '.join(SUPPORTED_NOTIFY_PROVIDERS)}")
    return provider


def derive_discord_session(channel_id: str) -> str:
    return f"agent:main:discord:channel:{channel_id}"


def _read_notify_binding(payload: Mapping[str, Any]) -> Dict[str, str]:
    binding = payload.get("notify_binding")
    if isinstance(binding, Mapping):
        provider = str(binding.get("provider") or "").strip()
        target = str(binding.get("target") or "").strip()
        if provider == "discord" and target.isdigit():
            return {"provider": "discord", "target": target, "session": str(binding.get("session") or derive_discord_session(target)).strip()}
        if provider in {"tmux-bridge", "telegram"} and target:
            return {"provider": provider, "target": target}
    legacy_routes = payload.get("notify_routes")
    if isinstance(legacy_routes, Mapping):
        discord_route = legacy_routes.get("discord")
        if isinstance(discord_route, Mapping):
            target = str(discord_route.get("channel_id") or "").strip()
            if target.isdigit():
                return {"provider": "discord", "target": target, "session": str(discord_route.get("session") or derive_discord_session(target)).strip()}
        for provider, key in (("tmux-bridge", "target_session"), ("telegram", "chat_id")):
            route = legacy_routes.get(provider)
            if isinstance(route, Mapping):
                target = str(route.get(key) or route.get("target") or "").strip()
                if target:
                    return {"provider": provider, "target": target}
    discord_channel_id = str(payload.get("discord_channel_id") or "").strip()
    if discord_channel_id.isdigit():
        return {"provider": "discord", "target": discord_channel_id, "session": str(payload.get("discord_session") or derive_discord_session(discord_channel_id)).strip()}
    return {}


def build_notify_binding(provider: str, target: str) -> Dict[str, str]:
    normalized_provider = validate_notify_provider(provider)
    normalized_target = str(target or "").strip()
    if normalized_provider == "discord":
        channel_id = validate_discord_channel_id(normalized_target, option_name="--notify-target")
        return {"provider": "discord", "target": channel_id, "session": derive_discord_session(channel_id)}
    if not normalized_target:
        raise RuntimeError(f"--notify-target is required for --notify-to {normalized_provider}")
    return {"provider": normalized_provider, "target": normalized_target}


def default_config_values() -> Dict[str, Any]:
    return {"_comment": CONFIG_COMMENT, "claude_command": "", "claude_home_path": "", "claude_config_path": "", "codex_turn_complete_channel_id": "", "discord_bot_token": "", "discord_channel_id": "", "discord_webhook_url": "", "telegram_bot_token": "", "max_inline_sessions": DEFAULT_MAX_INLINE_SESSIONS, "notify_enabled": True, "managed_session_ttl_seconds": DEFAULT_MANAGED_SESSION_TTL_SECONDS, "session": "", "discord_session": "", "runtime_home": "", "runtime_home_managed": False, "runtime_label": "", "codex_home": "", "codex_home_managed": False, "tmux_session": ""}


def load_raw_config() -> Dict[str, Any]:
    ensure_directories()
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = read_json_file(path)
    except (json.JSONDecodeError, JSONInputTooLargeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config() -> Dict[str, Any]:
    merged = default_config_values()
    merged.update(load_raw_config())
    return merged


def save_config(config: Dict[str, Any]) -> None:
    ensure_directories()
    write_text_atomically(config_path(), json.dumps(config, indent=2, ensure_ascii=False) + "\n")


def managed_session_ttl_seconds(config: Optional[Mapping[str, Any]] = None) -> int:
    raw = dict(config or load_config()).get("managed_session_ttl_seconds", DEFAULT_MANAGED_SESSION_TTL_SECONDS)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MANAGED_SESSION_TTL_SECONDS


def max_inline_sessions(config: Optional[Mapping[str, Any]] = None) -> int:
    raw = dict(config or load_config()).get("max_inline_sessions", DEFAULT_MAX_INLINE_SESSIONS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_INLINE_SESSIONS
    if value < 1:
        return DEFAULT_MAX_INLINE_SESSIONS
    return min(value, DEFAULT_MAX_INLINE_SESSIONS)


def config_key_field(key: str) -> str:
    field = CONFIG_KEY_MAP.get(key)
    if field is None:
        raise RuntimeError(f"Unsupported config key: {key}. Supported keys: {', '.join(sorted(CONFIG_KEY_MAP))}")
    return field


def default_config_value(key: str) -> Any:
    config_key_field(key)
    defaults = {"claude.command": DEFAULT_CLAUDE_COMMAND, "claude.home-path": "~/.claude", "claude.config-path": "~/.claude.json", "discord.bot-token": "", "discord.mention-user-id": "", "discord.webhook-url": "", "inline.max-sessions": DEFAULT_MAX_INLINE_SESSIONS, "managed.ttl-seconds": DEFAULT_MANAGED_SESSION_TTL_SECONDS, "notify.enabled": True, "telegram.bot-token": ""}
    return defaults[key]


def get_config_value(key: str) -> str:
    field = config_key_field(key)
    raw_config = load_raw_config()
    value = raw_config[field] if field in raw_config else default_config_value(key)
    if key == "notify.enabled":
        return "true" if bool(value) else "false"
    return "" if value is None else str(value)


def set_config_value(key: str, value: str) -> Dict[str, Any]:
    config = load_raw_config()
    field = config_key_field(key)
    normalized: Any = value.strip()
    if key == "notify.enabled":
        lowered = normalized.lower()
        if lowered in {"1", "true", "yes", "on"}:
            normalized = True
        elif lowered in {"0", "false", "no", "off"}:
            normalized = False
        else:
            raise RuntimeError("notify.enabled must be one of: true, false, 1, 0, yes, no, on, off")
    elif key == "managed.ttl-seconds":
        try:
            normalized = int(normalized)
        except ValueError as exc:
            raise RuntimeError("managed.ttl-seconds must be an integer number of seconds") from exc
    elif key == "inline.max-sessions":
        try:
            normalized = int(normalized)
        except ValueError as exc:
            raise RuntimeError("inline.max-sessions must be an integer between 1 and 4") from exc
        if normalized < 1 or normalized > DEFAULT_MAX_INLINE_SESSIONS:
            raise RuntimeError("inline.max-sessions must be between 1 and 4")
    config[field] = normalized
    config["_comment"] = CONFIG_COMMENT
    save_config(config)
    return config


def reset_config_value(key: str) -> Dict[str, Any]:
    config = load_raw_config()
    config.pop(config_key_field(key), None)
    if config:
        config["_comment"] = CONFIG_COMMENT
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
    tmux_session: str = "",
    runtime_home: Optional[str] = None,
    runtime_home_managed: Optional[bool] = None,
    runtime_label: str = "",
) -> Dict[str, Any]:
    config = load_config()
    config["_comment"] = CONFIG_COMMENT
    config.pop("orch_session", None)
    config.pop("parent_session_key", None)
    config["session"] = session
    config["cwd"] = str(cwd)
    config["agent"] = agent
    config["pane_id"] = pane_id
    config["tmux_session"] = str(tmux_session or "").strip()
    normalized_runtime_home = normalize_runtime_home(runtime_home)
    config["runtime_home"] = normalized_runtime_home
    if runtime_home_managed is not None:
        config["runtime_home_managed"] = bool(runtime_home_managed)
    config["runtime_label"] = runtime_label
    if agent == "codex":
        config["codex_home"] = normalized_runtime_home
        config["codex_home_managed"] = bool(runtime_home_managed)
    else:
        config["codex_home"] = ""
        config["codex_home_managed"] = False
    config["updated_at"] = time.time()
    save_config(config)
    return config
