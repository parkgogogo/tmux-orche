from __future__ import annotations

import io
from pathlib import Path

import tls


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b"{}"


def test_bundled_ca_file_prefers_certifi_when_env_not_set(monkeypatch, tmp_path):
    ca_file = tmp_path / "cacert.pem"
    ca_file.write_text("cert", encoding="utf-8")

    class FakeCertifi:
        @staticmethod
        def where() -> str:
            return str(ca_file)

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(tls, "certifi", FakeCertifi)

    assert tls.bundled_ca_file() == str(ca_file)


def test_bundled_ca_file_respects_existing_env(monkeypatch, tmp_path):
    custom_ca = tmp_path / "custom.pem"
    custom_ca.write_text("cert", encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(custom_ca))

    assert tls.bundled_ca_file() == ""


def test_configure_tls_runtime_sets_ssl_cert_file(monkeypatch, tmp_path):
    ca_file = tmp_path / "bundle.pem"
    ca_file.write_text("cert", encoding="utf-8")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(tls, "bundled_ca_file", lambda: str(ca_file))

    resolved = tls.configure_tls_runtime()

    assert resolved == str(ca_file)
    assert tls.os.environ["SSL_CERT_FILE"] == str(ca_file)


def test_urlopen_uses_default_ssl_context(monkeypatch):
    request = tls.urllib.request.Request("https://example.test")
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout, context=None):
        captured["request"] = req
        captured["timeout"] = timeout
        captured["context"] = context
        return FakeResponse()

    monkeypatch.setattr(tls, "default_ssl_context", lambda: "tls-context")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with tls.urlopen(request, timeout=5) as response:
        assert isinstance(response, FakeResponse)

    assert captured == {"request": request, "timeout": 5, "context": "tls-context"}
