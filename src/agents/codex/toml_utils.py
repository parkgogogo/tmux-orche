from __future__ import annotations

import json
import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None


TOML_TABLE_HEADER_RE = re.compile(r"^\s*\[\[?.*\]\]?\s*$")
TOML_NOTICE_HEADER_RE = re.compile(r"^\s*\[notice\]\s*$")
TOML_FEATURES_HEADER_RE = re.compile(r"^\s*\[features\]\s*$")
TOML_NOTIFY_KEY_RE = re.compile(r"^\s*notify\s*=")
TOML_UPDATE_CHECK_RE = re.compile(r"^\s*check_for_update_on_startup\s*=")
TOML_HIDE_RATE_LIMIT_MODEL_NUDGE_RE = re.compile(r"^\s*hide_rate_limit_model_nudge\s*=")
TOML_CODEX_HOOKS_RE = re.compile(r"^\s*codex_hooks\s*=")
TOML_PROJECT_HEADER_RE = re.compile(r"^\s*\[projects\.(.+)\]\s*$")
TOML_TRUST_LEVEL_RE = re.compile(r"^\s*trust_level\s*=")


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
    return updated + render_project_trust_block(cwd)


def upsert_top_level_setting(content: str, *, setting_line: str, matcher: re.Pattern[str]) -> str:
    lines = content.splitlines(keepends=True)
    first_table_index = next((index for index, line in enumerate(lines) if TOML_TABLE_HEADER_RE.match(line)), len(lines))
    prefix = [line for line in lines[:first_table_index] if not matcher.match(line)]
    suffix = lines[first_table_index:]
    while prefix and not prefix[-1].strip():
        prefix.pop()
    while suffix and not suffix[0].strip():
        suffix.pop(0)
    updated = list(prefix)
    if updated and not updated[-1].endswith("\n"):
        updated[-1] += "\n"
    if updated:
        updated.append("\n")
    updated.append(setting_line + "\n")
    if suffix:
        updated.append("\n")
        updated.extend(suffix)
    return "".join(updated)


def upsert_top_level_notify(content: str, notify_line: str) -> str:
    return upsert_top_level_setting(content, setting_line=notify_line, matcher=TOML_NOTIFY_KEY_RE)


def upsert_update_check_setting(content: str, *, enabled: bool) -> str:
    return upsert_top_level_setting(content, setting_line=render_update_check_setting(enabled), matcher=TOML_UPDATE_CHECK_RE)


def _upsert_table_setting(content: str, *, header_re: re.Pattern[str], header_name: str, matcher: re.Pattern[str], setting_line: str) -> str:
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not header_re.match(line):
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
    return updated + f"[{header_name}]\n{setting_line}\n"


def upsert_hide_rate_limit_model_nudge(content: str, *, enabled: bool) -> str:
    return _upsert_table_setting(content, header_re=TOML_NOTICE_HEADER_RE, header_name="notice", matcher=TOML_HIDE_RATE_LIMIT_MODEL_NUDGE_RE, setting_line=render_notice_boolean_setting("hide_rate_limit_model_nudge", enabled))


def upsert_codex_hooks_feature(content: str, *, enabled: bool) -> str:
    return _upsert_table_setting(content, header_re=TOML_FEATURES_HEADER_RE, header_name="features", matcher=TOML_CODEX_HOOKS_RE, setting_line=render_notice_boolean_setting("codex_hooks", enabled))


__all__ = [
    "TOML_CODEX_HOOKS_RE",
    "TOML_FEATURES_HEADER_RE",
    "TOML_HIDE_RATE_LIMIT_MODEL_NUDGE_RE",
    "TOML_NOTICE_HEADER_RE",
    "TOML_NOTIFY_KEY_RE",
    "TOML_PROJECT_HEADER_RE",
    "TOML_TABLE_HEADER_RE",
    "TOML_TRUST_LEVEL_RE",
    "TOML_UPDATE_CHECK_RE",
    "render_notice_boolean_setting",
    "render_project_trust_block",
    "render_update_check_setting",
    "strip_notify_assignments",
    "upsert_codex_hooks_feature",
    "upsert_hide_rate_limit_model_nudge",
    "upsert_project_trust",
    "upsert_top_level_notify",
    "upsert_top_level_setting",
    "upsert_update_check_setting",
    "validate_toml_document",
]
