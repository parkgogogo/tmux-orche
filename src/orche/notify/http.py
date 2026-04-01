from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class HTTPResponse:
    status_code: int
    body: str = ""


class HTTPClient(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any],
        timeout: float,
    ) -> HTTPResponse:
        ...


class UrllibHTTPClient:
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any],
        timeout: float,
    ) -> HTTPResponse:
        payload = json.dumps(dict(json_body)).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                status = getattr(response, "status", 200)
                return HTTPResponse(status_code=status, body=body)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return HTTPResponse(status_code=exc.code, body=body)
