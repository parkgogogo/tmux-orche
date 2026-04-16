from __future__ import annotations

import pytest

from session.config import (
    _read_notify_binding,
    build_notify_binding,
    get_config_value,
    list_config_values,
    load_raw_config,
    managed_session_ttl_seconds,
    max_inline_sessions,
    reset_config_value,
    set_config_value,
)
from session.meta import (
    DEFAULT_MANAGED_SESSION_TTL_SECONDS,
    DEFAULT_MAX_INLINE_SESSIONS,
)

pytestmark = pytest.mark.unit


def test_build_notify_binding_normalizes_discord_target():
    binding = build_notify_binding("discord", " 123456789 ")

    assert binding == {
        "provider": "discord",
        "target": "123456789",
        "session": "agent:main:discord:channel:123456789",
    }


def test_read_notify_binding_supports_legacy_routes():
    binding = _read_notify_binding(
        {
            "notify_routes": {
                "discord": {
                    "channel_id": "123456789",
                }
            }
        }
    )

    assert binding == {
        "provider": "discord",
        "target": "123456789",
        "session": "agent:main:discord:channel:123456789",
    }


def test_set_config_value_roundtrips_normalized_values(xdg_runtime):
    set_config_value("notify.enabled", "off")
    set_config_value("managed.ttl-seconds", "120")
    set_config_value("inline.max-sessions", "2")

    assert get_config_value("notify.enabled") == "false"
    assert get_config_value("managed.ttl-seconds") == "120"
    assert list_config_values()["inline.max-sessions"] == "2"
    assert load_raw_config()["notify_enabled"] is False

    reset_config_value("notify.enabled")

    assert get_config_value("notify.enabled") == "true"


def test_set_config_value_rejects_out_of_range_inline_limit(xdg_runtime):
    with pytest.raises(RuntimeError, match="between 1 and 4"):
        set_config_value("inline.max-sessions", "9")


def test_numeric_config_helpers_clamp_and_fallback():
    assert (
        max_inline_sessions({"max_inline_sessions": "999"})
        == DEFAULT_MAX_INLINE_SESSIONS
    )
    assert (
        max_inline_sessions({"max_inline_sessions": "0"}) == DEFAULT_MAX_INLINE_SESSIONS
    )
    assert (
        managed_session_ttl_seconds({"managed_session_ttl_seconds": "oops"})
        == DEFAULT_MANAGED_SESSION_TTL_SECONDS
    )
