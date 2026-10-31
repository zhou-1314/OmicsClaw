# SPDX-License-Identifier: Apache-2.0
"""Shared plumbing for the built-in life-science connectors.

Everything here is the standard library: a connector process must start
in any environment that can run OmicsClaw, so it cannot assume ``httpx``
or ``requests``.

* :class:`HttpClient` — GET/POST JSON or text with a timeout, a polite
  per-host request spacing, a bounded retry on throttling and transient
  server errors, and an identifying ``User-Agent``.
* :class:`Tool` — one tool: its name, description, JSON Schema for the
  arguments, and the function that answers it.
* :class:`ConnectorError` — a failure the model should read as the tool's
  answer (bad argument, upstream refused, nothing found) rather than a
  crash.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from omicsclaw.version import __version__

__all__ = [
    "ConnectorError",
    "DEFAULT_TIMEOUT_S",
    "HttpClient",
    "Tool",
    "USER_AGENT",
    "bounded_int",
    "clip",
    "require_str",
]

DEFAULT_TIMEOUT_S: Final = 30.0
"""Socket timeout of one upstream request."""

USER_AGENT: Final = (
    f"OmicsClaw-connectors/{__version__} (+https://github.com/zhou-1314/OmicsClaw)"
)

_RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS: Final = 3
_MAX_BODY_BYTES: Final = 16 * 1024 * 1024
"""Largest upstream response read; a longer one is an error, not a hang."""


class ConnectorError(Exception):
    """A tool failure reported to the model as ``isError`` text."""


@dataclass(frozen=True, slots=True)
class Tool:
    """One tool a connector offers."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Callable[[Mapping[str, Any]], Any]

    def listing(self) -> dict[str, Any]:
        """The entry ``tools/list`` returns for this tool."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
        }


@dataclass
class HttpClient:
    """A small, polite HTTP client for public scientific APIs.

    *min_interval_s* maps a host to the least time between two requests
    to it — NCBI, for one, asks for no more than three requests a second
    without an API key. Calls from several threads are spaced, not just
    calls from one.
    """

    timeout_s: float = DEFAULT_TIMEOUT_S
    min_interval_s: Mapping[str, float] = field(default_factory=dict)
    user_agent: str = USER_AGENT
    _last: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def get_json(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET *url* with *params* and decode the JSON body."""
        return _decode_json(self.request("GET", url, params=params), url)

    def get_text(self, url: str, params: Mapping[str, Any] | None = None) -> str:
        """GET *url* with *params* and return the body as text."""
        return self.request("GET", url, params=params).decode("utf-8", "replace")

    def post_json(self, url: str, document: Any) -> Any:
        """POST *document* as JSON and decode the JSON answer."""
        body = json.dumps(document).encode("utf-8")
        raw = self.request(
            "POST", url, body=body, headers={"Content-Type": "application/json"}
        )
        return _decode_json(raw, url)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        """Send one request, retrying throttling and transient failures.

        :raises ConnectorError: the upstream answered with an error status
            that is not worth retrying, kept failing, or was unreachable.
        """
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None and v != ""},
                doseq=True,
            )
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        sent_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain;q=0.8, */*;q=0.5",
            **(headers or {}),
        }
        host = urllib.parse.urlsplit(url).hostname or ""
        last_error = ""
        for attempt in range(_MAX_ATTEMPTS):
            self._space(host)
            req = urllib.request.Request(url, data=body, headers=sent_headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as response:
                    data = response.read(_MAX_BODY_BYTES + 1)
                if len(data) > _MAX_BODY_BYTES:
                    raise ConnectorError(f"{host} returned more than {_MAX_BODY_BYTES} bytes")
                return data
            except urllib.error.HTTPError as exc:
                detail = _error_detail(exc)
                last_error = f"{host} answered HTTP {exc.code}{detail}"
                if exc.code not in _RETRY_STATUSES:
                    raise ConnectorError(last_error) from None
                delay = _retry_after(exc) or 1.0 * (attempt + 1)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = f"could not reach {host}: {reason}"
                delay = 1.0 * (attempt + 1)
            if attempt + 1 < _MAX_ATTEMPTS:
                time.sleep(min(delay, 10.0))
        raise ConnectorError(last_error or f"request to {host} failed")

    def _space(self, host: str) -> None:
        interval = self.min_interval_s.get(host, 0.0)
        if interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._last.get(host, 0.0) + interval - now
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()


def require_str(arguments: Mapping[str, Any], key: str) -> str:
    """The non-empty string argument *key*.

    :raises ConnectorError: it is missing, not a string, or blank.
    """
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConnectorError(f"'{key}' must be a non-empty string")
    return value.strip()


def bounded_int(
    arguments: Mapping[str, Any], key: str, default: int, low: int, high: int
) -> int:
    """The integer argument *key*, defaulted and clamped to ``[low, high]``.

    :raises ConnectorError: it is present and not an integer.
    """
    value = arguments.get(key, default)
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConnectorError(f"'{key}' must be an integer")
    return max(low, min(high, int(value)))


def clip(text: Any, limit: int = 600) -> str:
    """*text* as a string cut to *limit* characters with an ellipsis."""
    value = "" if text is None else str(text)
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def env_value(name: str) -> str:
    """An environment variable, stripped; ``""`` when unset."""
    return os.environ.get(name, "").strip()


def _decode_json(raw: bytes, url: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        host = urllib.parse.urlsplit(url).hostname or url
        raise ConnectorError(f"{host} did not return JSON: {exc}") from None


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        text = exc.read(400).decode("utf-8", "replace")
    except Exception:
        return ""
    text = clip(text, 200)
    return f": {text}" if text else ""


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return float(value) if value else None
    except ValueError:
        return None
