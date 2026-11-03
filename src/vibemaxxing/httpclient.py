"""The one injectable HTTP client. Every outbound request in the package uses it."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol

from vibemaxxing import __version__
from vibemaxxing.errors import NetworkError

ALLOWED_HOSTS: Final = frozenset({"claude.ai", "platform.claude.com", "api.anthropic.com"})
# The Cursor seat's usage, and nothing else. A second client with its own list, so the
# Claude client can never be pointed at it and a Cursor token can never reach a Claude host.
CURSOR_HOSTS: Final = frozenset({"api2.cursor.sh"})
# The bridge's upstreams (Slack's and Atlassian's own MCP servers) and their OAuth endpoints: one
# more client with its own list, so the bridge's tokens can reach no other host.
BRIDGE_HOSTS: Final = frozenset(
    {"mcp.slack.com", "slack.com", "mcp.atlassian.com", "auth.atlassian.com"}
)
# What a response may weigh before the client stops reading it.
DEFAULT_MAX_BYTES: Final = 8 * 1024 * 1024
DEFAULT_TIMEOUT_S: Final = 10.0
# Cloudflare fronts all three hosts and answers urllib's default
# "Python-urllib/3.x" with 403 "error code: 1010" before the request ever
# reaches the API, so every login died at the token exchange. Any named
# agent gets through; this one says who we actually are.
USER_AGENT: Final = f"vibemaxxing/{__version__}"


class HTTPError(Exception):
    """A 4xx/5xx from an allowed host. ``str`` never includes the body."""

    def __init__(
        self,
        status: int,
        body: bytes,
        retry_after_s: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body
        self.retry_after_s = retry_after_s
        # Lower-cased names; a 401's WWW-Authenticate is the one that matters.
        self.headers: Mapping[str, str] = headers or {}


def _headers(raw: object) -> dict[str, str]:
    items = getattr(raw, "items", None)
    if not callable(items):
        return {}
    return {str(name).lower(): str(value) for name, value in items()}


def _retry_after_s(raw: str | None) -> float | None:
    # ponytail: delta-seconds only. RFC 9110 also allows an HTTP-date, which the
    # usage endpoint has not been observed to send; parse it when one shows up.
    try:
        seconds = float(raw) if raw is not None else None
    except ValueError:
        return None
    # float() takes "inf" and "nan"; neither is a wait anyone can schedule.
    return seconds if seconds is not None and math.isfinite(seconds) and seconds >= 0 else None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)  # lower-cased names

    def json(self) -> dict[str, object]:
        parsed: object
        try:
            parsed = json.loads(self.body)
        except ValueError as exc:
            raise NetworkError(
                "the server sent a body that is not JSON",
                "vibe usage --once",
            ) from exc
        if not isinstance(parsed, dict):
            raise NetworkError(
                "the server sent JSON that is not an object",
                "vibe usage --once",
            )
        return {str(key): value for key, value in parsed.items()}


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every 3xx.

    urllib's default opener follows a redirect and re-sends the Authorization
    header to whatever host the upstream names -- verified on loopback: a 302
    delivered `Bearer <token>` to a second server that was never compared
    against ALLOWED_HOSTS, and the caller still saw a clean 200. The allowlist
    is worthless if it only covers the first hop, and none of the three
    endpoints we speak to has any reason to redirect.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


_OPENER: Final = urllib.request.build_opener(_NoRedirect)


class UrllibClient:
    def __init__(self, hosts: frozenset[str] | None = None) -> None:
        # None is the module's ALLOWED_HOSTS, read at each request.
        self._hosts = hosts

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> HttpResponse:
        host = urllib.parse.urlsplit(url).hostname
        allowed = ALLOWED_HOSTS if self._hosts is None else self._hosts
        if host not in allowed:
            raise NetworkError(
                f"refusing to contact {host!r}: vibemaxxing only talks to "
                + ", ".join(sorted(allowed)),
                "vibe list",
            )
        sent = {"User-Agent": USER_AGENT, **headers}
        request = urllib.request.Request(url, data=body, headers=sent, method=method)
        try:
            with _OPENER.open(request, timeout=timeout_s) as response:
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise NetworkError(
                        f"the answer from {host} is larger than {max_bytes} bytes", "vibe list"
                    )
                return HttpResponse(int(response.status), data, _headers(response.headers))
        except urllib.error.HTTPError as exc:
            # Closed here: it holds the response body open, and Python 3.14 warns when
            # the collector has to do it, which the suite turns into a failure.
            with exc:
                # `from None`: the suppressed context holds the request object, whose
                # headers carry the bearer token.
                raise HTTPError(
                    int(exc.code),
                    exc.read(max_bytes + 1)[:max_bytes],
                    _retry_after_s(exc.headers.get("Retry-After")),
                    _headers(exc.headers),
                ) from None
        except OSError as exc:
            raise NetworkError(
                f"could not reach {host}: {exc.__class__.__name__}",
                "vibe usage --once",
            ) from None
