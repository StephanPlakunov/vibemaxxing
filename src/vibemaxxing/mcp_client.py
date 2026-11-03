"""A minimal MCP client over Streamable HTTP: initialize, list the tools, call one tool.

It exists so the bridge can forward one allowlisted call straight to a vendor's own MCP server
without a model anywhere in the path. It speaks JSON-RPC 2.0 over HTTP POST (the answer is JSON or
a short server-sent-event stream), keeps the session id the server hands out, and sends the user's
access token as a bearer header and nowhere else.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, cast

from vibemaxxing.errors import NeedsLoginError, NetworkError, VibeError
from vibemaxxing.httpclient import HttpClient, HTTPError, HttpResponse
from vibemaxxing.redact import Secret

PROTOCOL_VERSION: Final = "2025-06-18"
CLIENT_INFO: Final = {"name": "vibemaxxing-bridge", "version": "1"}
MAX_PAGES: Final = 5
# A token provider: called with True after a 401, when the stored token should be refreshed first.
TokenProvider = Callable[[bool], Secret]


class McpError(VibeError):
    """The upstream answered with a JSON-RPC error, or with something that is not MCP."""

    code = "mcp"


@dataclass(frozen=True)
class ToolResult:
    text: str  # the text content, joined; other content types are replaced by a note
    is_error: bool
    bytes: int  # size of ``text`` in bytes


def _sse_messages(body: str) -> list[object]:
    messages: list[object] = []
    data: list[str] = []
    for line in [*body.splitlines(), ""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line.strip() and data:
            with contextlib.suppress(ValueError):
                messages.append(json.loads("\n".join(data)))
            data = []
    return messages


def _message_for(response: HttpResponse, ident: int) -> Mapping[str, object]:
    """The JSON-RPC response with id ``ident`` from a JSON or event-stream body."""
    text = response.body.decode("utf-8", errors="replace")
    kind = response.headers.get("content-type", "")
    candidates: list[object]
    if "text/event-stream" in kind:
        candidates = _sse_messages(text)
    else:
        try:
            candidates = [json.loads(text)]
        except ValueError:
            raise McpError(
                "the upstream sent a body that is not JSON", "vibe bridge status"
            ) from None
        if len(candidates) == 1 and isinstance(candidates[0], list):
            candidates = cast("list[object]", candidates[0])
    for item in candidates:
        if isinstance(item, dict) and item.get("id") == ident:
            return {str(key): value for key, value in item.items()}
    raise McpError("the upstream's answer holds no response to the request", "vibe bridge status")


class McpClient:
    def __init__(
        self,
        client: HttpClient,
        url: str,
        token: TokenProvider,
        *,
        timeout_s: float,
        max_bytes: int,
    ) -> None:
        self._client, self._url, self._token = client, url, token
        self._timeout_s, self._max_bytes = timeout_s, max_bytes
        self._next_id = 0

    def _post(
        self, payload: Mapping[str, object], session: str | None, *, force_token: bool = False
    ) -> HttpResponse:
        headers = {
            # A permitted reveal() site (CONTRACT section 5): the bearer header, nowhere else.
            "Authorization": f"Bearer {self._token(force_token).reveal()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if session is not None:
            headers["Mcp-Session-Id"] = session
        return self._client.request(
            "POST",
            self._url,
            headers=headers,
            body=json.dumps(payload).encode(),
            timeout_s=self._timeout_s,
            max_bytes=self._max_bytes,
        )

    def _request(
        self, method: str, params: Mapping[str, object], session: str | None
    ) -> tuple[Mapping[str, object], HttpResponse]:
        self._next_id += 1
        ident = self._next_id
        payload = {"jsonrpc": "2.0", "id": ident, "method": method, "params": dict(params)}
        try:
            response = self._post(payload, session)
        except HTTPError as exc:
            if exc.status != 401:
                raise self._http_error(exc) from None
            # The token may have been revoked or may just be old: refresh once, then try again.
            try:
                response = self._post(payload, session, force_token=True)
            except HTTPError as again:
                if again.status == 401:
                    raise NeedsLoginError(
                        "the upstream does not accept the stored login",
                        "vibe bridge login <upstream>",
                    ) from None
                raise self._http_error(again) from None
        message = _message_for(response, ident)
        error = message.get("error")
        if isinstance(error, dict):
            text = error.get("message")
            raise McpError(
                f"the upstream answered an error ({error.get('code')}): "
                f"{text if isinstance(text, str) else 'no message'}"[:300],
                "vibe bridge status",
            )
        return message, response

    @staticmethod
    def _http_error(exc: HTTPError) -> VibeError:
        if exc.status in (403,):
            return NeedsLoginError(
                f"the upstream refused this login (HTTP {exc.status}): it may lack the scope or "
                "the app may not be approved",
                "vibe bridge login <upstream>",
            )
        return NetworkError(f"the upstream answered HTTP {exc.status}", "vibe bridge status")

    def _open(self) -> str | None:
        _, response = self._request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            None,
        )
        session = response.headers.get("mcp-session-id")
        # A notification; a server that dislikes it will say so on the next request.
        with contextlib.suppress(HTTPError):
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        return session

    def _close(self, session: str | None) -> None:
        if session is None:
            return
        # Best effort: the server drops an idle session by itself.
        with contextlib.suppress(HTTPError, VibeError):
            self._client.request(
                "DELETE",
                self._url,
                headers={
                    "Authorization": f"Bearer {self._token(False).reveal()}",
                    "Mcp-Session-Id": session,
                },
                timeout_s=5.0,
                max_bytes=4096,
            )

    def list_tools(self) -> list[dict[str, object]]:
        session = self._open()
        tools: list[dict[str, object]] = []
        try:
            cursor: str | None = None
            for _ in range(MAX_PAGES):
                params: dict[str, object] = {} if cursor is None else {"cursor": cursor}
                message, _ = self._request("tools/list", params, session)
                result = message.get("result")
                page = result.get("tools") if isinstance(result, dict) else None
                for item in page if isinstance(page, list) else []:
                    if isinstance(item, dict):
                        tools.append({str(k): v for k, v in item.items()})
                next_cursor = result.get("nextCursor") if isinstance(result, dict) else None
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                cursor = next_cursor
        finally:
            self._close(session)
        return tools

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> ToolResult:
        session = self._open()
        try:
            message, _ = self._request(
                "tools/call", {"name": name, "arguments": dict(arguments)}, session
            )
        finally:
            self._close(session)
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpError("the upstream's tool answer has no result", "vibe bridge status")
        parts: list[str] = []
        omitted = False
        content = result.get("content")
        for item in content if isinstance(content, list) else []:
            if (
                isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ):
                parts.append(cast("str", item["text"]))
            else:
                omitted = True
        if omitted:
            parts.append("[non-text content omitted by the bridge]")
        text = "\n".join(parts)
        return ToolResult(text, result.get("isError") is True, len(text.encode("utf-8")))
