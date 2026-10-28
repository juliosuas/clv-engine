"""Tiny stdlib HTTP client (GET only).

Adapted from juliosuas/polymarket-desk get_json/build_url (api/common.py, MIT),
but failures are logged instead of silently swallowed. This module only ever
issues GET requests: the engine never talks to order/trading endpoints.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

log = logging.getLogger(__name__)

USER_AGENT = "clv-engine/0.1 (paper-only, read-only)"


def build_url(base: str, params: dict[str, Any] | Iterable[tuple[str, Any]] | None = None) -> str:
    if not params:
        return base
    items = params.items() if isinstance(params, dict) else params
    clean = [(k, v) for k, v in items if v is not None]
    return f"{base}?{urllib.parse.urlencode(clean, doseq=True)}"


class Http:
    """GET-only JSON client with retry/backoff. Swap with a fake in tests."""

    def __init__(self, timeout: float = 10.0, retries: int = 2, min_interval: float = 0.0):
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self._last = 0.0
        self.calls = 0

    def get_json(self, url: str, params: Any = None, headers: dict[str, str] | None = None) -> Any:
        return self.get_json_meta(url, params, headers)[0]

    def get_json_meta(self, url: str, params: Any = None, headers: dict[str, str] | None = None) -> tuple[Any, dict[str, str]]:
        """GET -> (parsed JSON or None, lower-cased response headers). Headers are returned on HTTP errors too
        (e.g. a 429 from The Odds API still carries x-requests-remaining)."""
        full = build_url(url, params)
        req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
        resp_headers: dict[str, str] = {}
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    raw = resp.read()
                    return (json.loads(raw) if raw else None), resp_headers
            except urllib.error.HTTPError as exc:
                resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                log.warning("GET %s -> HTTP %s", _redact(full), exc.code)
                return None, resp_headers
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                if attempt < self.retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                log.warning("GET %s failed: %s", _redact(full), exc)
                return None, resp_headers
        return None, resp_headers


def _redact(url: str) -> str:
    """Never log API keys."""
    parts = urllib.parse.urlsplit(url)
    q = [(k, "***" if k.lower() in ("apikey", "api_key", "key", "token") else v) for k, v in urllib.parse.parse_qsl(parts.query)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))
