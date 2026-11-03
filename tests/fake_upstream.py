"""A fake upstream: discovery documents, dynamic registration, a token endpoint with PKCE and a
rotating refresh token, and an MCP endpoint (JSON or event-stream). It is an `HttpClient`, so no
socket is ever opened, and it records every request.

Everything it does is what the real servers' public documents say; nothing here contacts anyone.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from vibemaxxing.httpclient import HTTPError, HttpResponse
from vibemaxxing.mcp_oauth import Upstream

ToolHandler = Callable[[Mapping[str, object]], dict[str, object]]


@dataclass
class Seen:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None

    def form(self) -> dict[str, str]:
        return {k: v[0] for k, v in urllib.parse.parse_qs((self.body or b"").decode()).items()}

    def rpc(self) -> dict[str, object]:
        return json.loads(self.body or b"{}")


class FakeSecrets:
    """An in-memory SecretStore that remembers the order of its writes."""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}
        self.log: list[tuple[str, str]] = []  # (verb, service); never a value

    def read(self, service: str) -> str | None:
        return self.items.get(service)

    def write(self, service: str, value: str) -> None:
        self.log.append(("write", service))
        self.items[service] = value

    def delete(self, service: str) -> None:
        self.log.append(("delete", service))
        self.items.pop(service, None)


class FakeReceiver:
    """A CodeReceiver that hands back a code as if the browser had been redirected."""

    def __init__(self, code: str = "auth-code-1", *, port: int = 8765) -> None:
        self.redirect_uri = f"http://127.0.0.1:{port}/callback"
        self.code = code
        self.states: list[str] = []
        self.closed = False

    def wait(self, state: str, timeout_s: float) -> str:
        self.states.append(state)
        return self.code

    def close(self) -> None:
        self.closed = True


def text_result(text: str, *, error: bool = False) -> dict[str, object]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


@dataclass
class FakeUpstream:
    upstream: Upstream
    # What the MCP server offers: name -> (description, inputSchema, handler)
    tools: dict[str, tuple[str, dict[str, object], ToolHandler]] = field(default_factory=dict)
    access: str = "ACCESS0000000001"
    refresh: str = "REFRESH000000001"
    expires_in: int = 3600
    sse: bool = False
    dcr: bool = True
    issuer: str | None = None
    prm_authorization_server: str | None = None
    requests: list[Seen] = field(default_factory=list)
    counter: int = 1
    challenge: str | None = None
    code: str = "auth-code-1"
    reject_next_mcp: int = 0  # answer 401 to this many MCP posts (a revoked or expired token)
    always_401: bool = False
    refresh_fails: bool = False
    page_size: int = 0
    session: str = "sess-1"

    # --- the documents -----------------------------------------------------------------------

    def _origin(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def issuer_url(self) -> str:
        return self.issuer or self._origin(self.upstream.mcp_url)

    def prm(self) -> dict[str, object]:
        return {
            "resource": self.upstream.mcp_url,
            "authorization_servers": [self.prm_authorization_server or self.issuer_url],
            "scopes_supported": ["a", "b"],
        }

    def as_metadata(self) -> dict[str, object]:
        base = self.issuer_url
        meta: dict[str, object] = {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "code_challenge_methods_supported": ["S256"],
        }
        if self.dcr:
            meta["registration_endpoint"] = f"{base}/register"
        return meta

    # --- HttpClient ----------------------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_s: float = 10.0,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> HttpResponse:
        self.requests.append(Seen(method, url, dict(headers), body))
        path = urllib.parse.urlsplit(url).path
        if method == "GET" and url == self.upstream.prm_url:
            return self._json(self.prm())
        if method == "GET" and path.endswith("/.well-known/oauth-authorization-server"):
            return self._json(self.as_metadata())
        if method == "GET" and "/.well-known/oauth-authorization-server" in path:
            return self._json(self.as_metadata())
        if method == "GET":
            raise HTTPError(404, b"not found")
        if method == "POST" and path.endswith("/register"):
            return self._json({"client_id": "dcr-client-1"})
        if method == "POST" and path.endswith("/token"):
            return self._token(body or b"")
        if url == self.upstream.mcp_url:
            return self._mcp(method, headers, body)
        raise HTTPError(404, b"unknown")

    @staticmethod
    def _json(payload: object, headers: dict[str, str] | None = None) -> HttpResponse:
        return HttpResponse(
            200,
            json.dumps(payload).encode(),
            {"content-type": "application/json", **(headers or {})},
        )

    def _token(self, body: bytes) -> HttpResponse:
        form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()}
        if form.get("grant_type") == "authorization_code":
            verifier = form.get("code_verifier", "")
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            if form.get("code") != self.code or (
                self.challenge is not None and digest.decode().rstrip("=") != self.challenge
            ):
                raise HTTPError(400, b'{"error": "invalid_grant"}')
            return self._issue()
        if form.get("grant_type") == "refresh_token":
            if self.refresh_fails or form.get("refresh_token") != self.refresh:
                raise HTTPError(400, b'{"error": "invalid_grant", "echo": "SECRETECHO"}')
            return self._issue()
        raise HTTPError(400, b'{"error": "unsupported_grant_type"}')

    def _issue(self) -> HttpResponse:
        self.counter += 1
        self.access = f"ACCESS{self.counter:010d}"
        self.refresh = f"REFRESH{self.counter:09d}"
        return self._json(
            {
                "access_token": self.access,
                "refresh_token": self.refresh,
                "expires_in": self.expires_in,
                "scope": "granted",
                "token_type": "Bearer",
            }
        )

    # --- the MCP endpoint ---------------------------------------------------------------------

    def _mcp(self, method: str, headers: Mapping[str, str], body: bytes | None) -> HttpResponse:
        if method == "DELETE":
            return HttpResponse(200, b"", {})
        if self.always_401 or headers.get("Authorization") != f"Bearer {self.access}":
            raise HTTPError(401, b"{}", None, {"www-authenticate": "Bearer"})
        if self.reject_next_mcp > 0:
            self.reject_next_mcp -= 1
            raise HTTPError(401, b"{}", None, {"www-authenticate": "Bearer"})
        message = json.loads(body or b"{}")
        if "id" not in message:
            return HttpResponse(202, b"", {})
        ident, rpc_method = message["id"], message["method"]
        params = message.get("params") or {}
        if rpc_method == "initialize":
            result: dict[str, object] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
            }
            return self._reply(ident, result, {"mcp-session-id": self.session})
        if rpc_method == "tools/list":
            names = sorted(self.tools)
            start = int(params.get("cursor") or 0)
            page = names[start : start + self.page_size] if self.page_size else names
            listing = [
                {"name": n, "description": self.tools[n][0], "inputSchema": self.tools[n][1]}
                for n in page
            ]
            out: dict[str, object] = {"tools": listing}
            if self.page_size and start + self.page_size < len(names):
                out["nextCursor"] = str(start + self.page_size)
            return self._reply(ident, out)
        if rpc_method == "tools/call":
            name = params.get("name")
            if name not in self.tools:
                return self._reply(
                    ident, None, error={"code": -32602, "message": f"unknown tool {name}"}
                )
            return self._reply(ident, self.tools[str(name)][2](params.get("arguments") or {}))
        return self._reply(ident, None, error={"code": -32601, "message": "no such method"})

    def _reply(
        self,
        ident: object,
        result: object,
        headers: dict[str, str] | None = None,
        error: dict[str, object] | None = None,
    ) -> HttpResponse:
        message: dict[str, object] = {"jsonrpc": "2.0", "id": ident}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        if self.sse:
            text = f"event: message\ndata: {json.dumps(message)}\n\n"
            return HttpResponse(
                200, text.encode(), {"content-type": "text/event-stream", **(headers or {})}
            )
        return self._json(message, headers)

    # --- helpers for assertions -----------------------------------------------------------------

    def mcp_methods(self) -> list[str]:
        return [
            str(seen.rpc().get("method"))
            for seen in self.requests
            if seen.url == self.upstream.mcp_url and seen.method == "POST"
        ]

    def calls(self) -> list[dict[str, object]]:
        """The tools/call requests, as (name, arguments)."""
        return [
            dict(seen.rpc()["params"])  # type: ignore[call-overload]
            for seen in self.requests
            if seen.url == self.upstream.mcp_url
            and seen.method == "POST"
            and seen.rpc().get("method") == "tools/call"
        ]
