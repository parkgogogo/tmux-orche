from __future__ import annotations

import sys
import types
from contextlib import contextmanager

from . import agent as _agent
from . import runtime as _runtime
from .agent import (
    CODEX_SUBMIT_SECONDS_PER_CHAR,
    CODEX_SUBMIT_SETTLE_MAX_SECONDS,
    CODEX_SUBMIT_SETTLE_MIN_SECONDS,
    CodexAgent,
    PLUGINS,
    READY_SURFACE_HINTS,
    codex_submit_settle_seconds,
)
from .runtime import (
    DEFAULT_CODEX_SOURCE_HOME,
    DEFAULT_RUNTIME_HOME_ROOT,
    SOURCE_CONFIG_BACKUP_SUFFIX,
    SOURCE_CONFIG_LOCK_NAME,
    build_hooks_payload,
    cleanup_managed_codex_home,
    default_codex_home_path,
    default_hooks_path,
    default_notify_hook_path,
    materialize_managed_codex_home,
    read_text_or_empty,
    render_hook_command,
    render_notify_assignment,
    rewrite_codex_config,
    source_codex_config_backup_path,
    source_codex_config_path,
    source_config_lock,
    source_hooks_path,
    sync_trust_to_source_config,
)
from .toml_utils import (
    TOML_CODEX_HOOKS_RE,
    TOML_FEATURES_HEADER_RE,
    TOML_HIDE_RATE_LIMIT_MODEL_NUDGE_RE,
    TOML_NOTICE_HEADER_RE,
    TOML_NOTIFY_KEY_RE,
    TOML_PROJECT_HEADER_RE,
    TOML_TABLE_HEADER_RE,
    TOML_TRUST_LEVEL_RE,
    TOML_UPDATE_CHECK_RE,
    render_notice_boolean_setting,
    render_project_trust_block,
    render_update_check_setting,
    strip_notify_assignments,
    upsert_codex_hooks_feature,
    upsert_hide_rate_limit_model_nudge,
    upsert_project_trust,
    upsert_top_level_notify,
    upsert_top_level_setting,
    upsert_update_check_setting,
    validate_toml_document,
)


class _CodexModule(types.ModuleType):
    def __setattr__(self, name: str, value) -> None:
        if hasattr(_runtime, name):
            setattr(_runtime, name, value)
        if hasattr(_agent, name):
            setattr(_agent, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _CodexModule

__all__ = [
    "CODEX_SUBMIT_SECONDS_PER_CHAR",
    "CODEX_SUBMIT_SETTLE_MAX_SECONDS",
    "CODEX_SUBMIT_SETTLE_MIN_SECONDS",
    "CodexAgent",
    "DEFAULT_CODEX_SOURCE_HOME",
    "DEFAULT_RUNTIME_HOME_ROOT",
    "PLUGINS",
    "READY_SURFACE_HINTS",
    "SOURCE_CONFIG_BACKUP_SUFFIX",
    "SOURCE_CONFIG_LOCK_NAME",
    "TOML_CODEX_HOOKS_RE",
    "TOML_FEATURES_HEADER_RE",
    "TOML_HIDE_RATE_LIMIT_MODEL_NUDGE_RE",
    "TOML_NOTICE_HEADER_RE",
    "TOML_NOTIFY_KEY_RE",
    "TOML_PROJECT_HEADER_RE",
    "TOML_TABLE_HEADER_RE",
    "TOML_TRUST_LEVEL_RE",
    "TOML_UPDATE_CHECK_RE",
    "build_hooks_payload",
    "cleanup_managed_codex_home",
    "codex_submit_settle_seconds",
    "default_codex_home_path",
    "default_hooks_path",
    "default_notify_hook_path",
    "materialize_managed_codex_home",
    "read_text_or_empty",
    "render_hook_command",
    "render_notice_boolean_setting",
    "render_notify_assignment",
    "render_project_trust_block",
    "render_update_check_setting",
    "rewrite_codex_config",
    "source_codex_config_backup_path",
    "source_codex_config_path",
    "source_config_lock",
    "source_hooks_path",
    "strip_notify_assignments",
    "sync_trust_to_source_config",
    "upsert_codex_hooks_feature",
    "upsert_hide_rate_limit_model_nudge",
    "upsert_project_trust",
    "upsert_top_level_notify",
    "upsert_top_level_setting",
    "upsert_update_check_setting",
    "validate_toml_document",
]
