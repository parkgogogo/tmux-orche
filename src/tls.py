from __future__ import annotations

import os
import ssl
import urllib.request
from pathlib import Path

try:
    import certifi
except ImportError:  # pragma: no cover - fallback for source trees without optional deps installed
    certifi = None


def bundled_ca_file() -> str:
    if str(os.environ.get("SSL_CERT_FILE") or "").strip():
        return ""
    if str(os.environ.get("SSL_CERT_DIR") or "").strip():
        return ""
    if certifi is None:
        return ""
    candidate = Path(str(certifi.where()) or "").expanduser()
    if not candidate.exists():
        return ""
    return str(candidate)


def default_ssl_context() -> ssl.SSLContext | None:
    cafile = bundled_ca_file()
    if not cafile:
        return None
    return ssl.create_default_context(cafile=cafile)


def configure_tls_runtime() -> str:
    cafile = bundled_ca_file()
    if cafile:
        os.environ.setdefault("SSL_CERT_FILE", cafile)
    return cafile


def urlopen(request: urllib.request.Request, *, timeout: float):
    context = default_ssl_context()
    if context is None:
        return urllib.request.urlopen(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout, context=context)
