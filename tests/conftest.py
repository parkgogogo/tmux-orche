from __future__ import annotations

import json
from pathlib import Path

import pytest

from notify.http import HTTPResponse


class FakeHTTPClient:
    def __init__(self, responses=None):
        self.requests = []
        self.responses = list(responses or [HTTPResponse(200, "ok")])

    def post(self, url, *, headers, json_body, timeout):
        self.requests.append(
            {
                "url": url,
                "headers": dict(headers),
                "json_body": dict(json_body),
                "timeout": timeout,
            }
        )
        if self.responses:
            return self.responses.pop(0)
        return HTTPResponse(200, "ok")


@pytest.fixture
def fake_http_client():
    return FakeHTTPClient()


@pytest.fixture
def xdg_runtime(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    home = tmp_path / "home"
    config_home.mkdir()
    data_home.mkdir()
    home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ORCHE_SESSION", raising=False)
    monkeypatch.delenv("ORCHE_DISCORD_CHANNEL_ID", raising=False)
    return {
        "config_home": config_home,
        "data_home": data_home,
        "home": home,
        "config_path": config_home / "orche" / "config.json",
    }


def write_runtime_config(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
