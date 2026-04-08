from __future__ import annotations

import io
import urllib.error

from notify.http import UrllibHTTPClient


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_urllib_http_client_handles_success(monkeypatch):
    client = UrllibHTTPClient()

    def fake_urlopen(request, timeout):
        assert timeout == 3
        assert request.full_url == "https://discord.test"
        return FakeResponse(204, "ok")

    monkeypatch.setattr("notify.http.urlopen", fake_urlopen)

    response = client.post(
        "https://discord.test",
        headers={"Content-Type": "application/json"},
        json_body={"content": "done"},
        timeout=3,
    )

    assert response.status_code == 204
    assert response.body == "ok"


def test_urllib_http_client_handles_http_error(monkeypatch):
    client = UrllibHTTPClient()

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "rate limited",
            hdrs=None,
            fp=io.BytesIO(b"too many requests"),
        )

    monkeypatch.setattr("notify.http.urlopen", fake_urlopen)

    response = client.post(
        "https://discord.test",
        headers={"Content-Type": "application/json"},
        json_body={"content": "done"},
        timeout=3,
    )

    assert response.status_code == 429
    assert response.body == "too many requests"
