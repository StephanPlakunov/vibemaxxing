"""vibe-bridge: a local stdio MCP server that forwards one allowlisted call to a vendor's own MCP.

A session on the extra account has no Slack or Atlassian connector. It starts this server
(`vibe bridge serve`) and calls its tools. Each call is forwarded by vibe itself, as an MCP client,
straight to the vendor's remote MCP server, as exactly one allowlisted upstream tool with the
arguments given. **No model is anywhere in the path**, so there is no tool list for a prompt
injection to widen and nothing to fence with a prompt, a deny list or an init check.

The safety is the code:

* the allowlist below is the only thing that can be called. A name not on it is refused before any
  network is touched, whatever the upstream offers. Reads, and the upstream's own draft tool.
  Never send, schedule, post, react, create, update or delete;
* the user's own OAuth login per upstream (mcp_oauth.py), tokens in the Keychain;
* a timeout and a size cap per call, a calls-per-hour cap per bridge process, results returned as
  data, and a log line (upstream, tool, duration, bytes, outcome: never arguments or content).

Microsoft 365 is not an upstream: Microsoft's own remote MCP (Work IQ) needs a Global
Administrator's consent, which is not the user's own OAuth.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TextIO

from vibemaxxing import mcp_oauth
from vibemaxxing.errors import UsageError, VibeError
from vibemaxxing.fsutil import FILE_MODE
from vibemaxxing.httpclient import HttpClient
from vibemaxxing.mcp_client import McpClient, ToolResult
from vibemaxxing.mcp_oauth import SecretStore, Upstream
from vibemaxxing.redact import Secret, scrub

PROTOCOL: Final = "2024-11-05"
SERVER_NAME: Final = "vibe-bridge"
LOG_NAME: Final = "bridge.log"
DEFAULT_TIMEOUT_S: Final = 60.0
DEFAULT_MAX_CALLS_PER_HOUR: Final = 30
DEFAULT_MAX_RESULT_BYTES: Final = 100_000
HARD_MAX_RESULT_BYTES: Final = 1_000_000
MAX_ARGUMENT_BYTES: Final = 20_000
CATALOG_TTL_S: Final = 600.0
CATALOG_RETRY_S: Final = 60.0
REFUSAL: Final = (
    "refused: {name} is not on the bridge allowlist. The bridge reads and drafts only; sending, "
    "scheduling, posting, reacting, creating, updating and deleting are never allowed here: "
    "ask the lead"
)

SLACK: Final = Upstream(
    id="slack",
    name="Slack",
    mcp_url="https://mcp.slack.com/mcp",
    prm_url="https://mcp.slack.com/.well-known/oauth-protected-resource",
    hosts=frozenset({"mcp.slack.com", "slack.com"}),
    registration="preregistered",  # no dynamic client registration: an app of the user's own
    read_scopes=(
        "channels:history", "channels:read", "groups:history", "groups:read", "im:history",
        "im:read", "mpim:history", "mpim:read", "search:read.public", "search:read.private",
        "search:read.im", "search:read.mpim", "search:read.users", "users:read", "canvases:read",
        "files:read", "lists:read", "reactions:read",
    ),
    draft_scopes=("chat:write",),
    send_resource=False,
)  # fmt: skip
ATLASSIAN: Final = Upstream(
    id="atlassian",
    name="Atlassian",
    mcp_url="https://mcp.atlassian.com/v2/mcp",
    prm_url="https://mcp.atlassian.com/.well-known/oauth-protected-resource/v2/mcp",
    hosts=frozenset({"mcp.atlassian.com", "auth.atlassian.com"}),
    registration="dcr",
    read_scopes=(
        "read:me", "read:account", "offline_access", "read:jira:agent-interface",
        "search:jira:agent-interface", "read:confluence:agent-interface",
        "search:confluence:agent-interface",
    ),
)  # fmt: skip
UPSTREAMS: Final = {upstream.id: upstream for upstream in (SLACK, ATLASSIAN)}


@dataclass(frozen=True)
class BridgeTool:
    name: str  # what the extra session calls
    upstream: str  # an id in UPSTREAMS
    upstream_tool: str  # the vendor's own tool name
    description: str
    draft: bool = False


def _reads(upstream: str, prefix: str, *names: str) -> tuple[BridgeTool, ...]:
    return tuple(
        BridgeTool(
            f"{prefix}{name}", upstream, name, f"{UPSTREAMS[upstream].name} {name} (read only)"
        )
        for name in names
    )


# The allowlist. Names are the vendors' own (Slack's observed in a connector listing; Atlassian's
# from its v2 tool documentation). `vibe bridge tools <upstream>` checks them against the live
# server after a login. A name the server does not offer is simply not listed.
TOOLS: Final = (
    *_reads(
        "slack", "",
        "slack_read_channel", "slack_read_thread", "slack_read_user_profile",
        "slack_search_public", "slack_search_public_and_private", "slack_search_channels",
        "slack_search_users", "slack_list_channel_members", "slack_list_user_channels",
        "slack_get_reactions", "slack_read_canvas", "slack_read_file", "slack_read_list",
    ),
    BridgeTool(
        "slack_send_message_draft", "slack", "slack_send_message_draft",
        "Slack: create a DRAFT message for the user to review and send themselves (never sends).",
        draft=True,
    ),
    *_reads(
        "atlassian", "atlassian_",
        "atlassianUserInfo", "getAccessibleAtlassianResources", "getJiraIssue",
        "searchJiraIssuesUsingJql", "listJiraProjects", "listJiraIssueTransitions",
        "listJiraIssueComments", "lookupJiraAccountId", "getConfluenceContent",
        "searchConfluence", "listConfluenceContent", "listConfluenceSpaces", "getConfluenceSpace",
        "listConfluenceComments", "search",
    ),
)  # fmt: skip
BY_NAME: Final = {tool.name: tool for tool in TOOLS}


# --- config --------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeConfig:
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_calls_per_hour: int = DEFAULT_MAX_CALLS_PER_HOUR
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES


def _bad(why: str) -> UsageError:
    return UsageError(f"settings.json routing.bridge {why}", "edit ~/.vibemaxxing/settings.json")


def parse_config(raw: object) -> BridgeConfig:
    """`routing.bridge`. Nothing in it can add a tool or an upstream: both are code."""
    if raw is None:
        return BridgeConfig()
    if not isinstance(raw, dict):
        raise _bad("must be an object")
    known = {"timeout_s", "max_calls_per_hour", "max_result_bytes"}
    if set(raw) - known:
        raise _bad(f"has unknown members: {', '.join(sorted(set(raw) - known))}")
    config = BridgeConfig()
    timeout = raw.get("timeout_s", config.timeout_s)
    calls = raw.get("max_calls_per_hour", config.max_calls_per_hour)
    size = raw.get("max_result_bytes", config.max_result_bytes)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1 <= timeout <= 600
    ):
        raise _bad("timeout_s must be between 1 and 600")
    if isinstance(calls, bool) or not isinstance(calls, int) or not 1 <= calls <= 1000:
        raise _bad("max_calls_per_hour must be between 1 and 1000")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not 1000 <= size <= HARD_MAX_RESULT_BYTES
    ):
        raise _bad(f"max_result_bytes must be between 1000 and {HARD_MAX_RESULT_BYTES}")
    return BridgeConfig(float(timeout), calls, size)


# --- what the upstreams really offer ----------------------------------------------------------


class BridgeError(Exception):
    """A call that failed; the message is safe to show (no content, no secret)."""


Catalog = Mapping[str, "Mapping[str, Mapping[str, object]] | None"]


class Upstreams:
    """The logged-in upstreams: forward a call, and read what each one offers."""

    def __init__(
        self,
        *,
        client: HttpClient,
        secrets: SecretStore,
        root: Path,
        config: BridgeConfig,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client, self._secrets, self._root = client, secrets, root
        self._config, self._clock = config, clock
        self._catalog: tuple[float, dict[str, Mapping[str, Mapping[str, object]] | None]] | None = (
            None
        )

    def logged_in(self, upstream: Upstream) -> bool:
        return mcp_oauth.load(self._root, self._secrets, upstream) is not None

    def _mcp(self, upstream: Upstream) -> McpClient:
        def token(force: bool) -> Secret:
            return mcp_oauth.refresh_tokens(
                upstream,
                client=self._client,
                secrets=self._secrets,
                root=self._root,
                now_s=self._clock(),
                force=force,
            ).access

        return McpClient(
            self._client,
            upstream.mcp_url,
            token,
            timeout_s=self._config.timeout_s,
            max_bytes=2 * self._config.max_result_bytes + 65_536,
        )

    def forward(self, tool: BridgeTool, arguments: Mapping[str, object]) -> ToolResult:
        """Exactly one upstream tool, the one this allowlisted name stands for."""
        return self._mcp(UPSTREAMS[tool.upstream]).call_tool(tool.upstream_tool, arguments)

    def upstream_tools(self, upstream: Upstream) -> list[dict[str, object]]:
        return self._mcp(upstream).list_tools()

    def catalog(self) -> Catalog:
        """Per logged-in upstream, the tools it offers (None when that could not be read)."""
        now = self._clock()
        if self._catalog is not None and now < self._catalog[0]:
            return self._catalog[1]
        found: dict[str, Mapping[str, Mapping[str, object]] | None] = {}
        failed = False
        for upstream in UPSTREAMS.values():
            if not self.logged_in(upstream):
                continue
            try:
                found[upstream.id] = {
                    str(tool.get("name")): tool for tool in self.upstream_tools(upstream)
                }
            except (VibeError, OSError):
                found[upstream.id] = None
                failed = True
        self._catalog = (now + (CATALOG_RETRY_S if failed else CATALOG_TTL_S), found)
        return found


# --- the server ----------------------------------------------------------------------


class Guard:
    """Calls per hour, per bridge process: a sliding window."""

    def __init__(self, limit: int, clock: Callable[[], float]) -> None:
        self._limit, self._clock = limit, clock
        self._calls: deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        while self._calls and now - self._calls[0] >= 3600:
            self._calls.popleft()
        if len(self._calls) >= self._limit:
            return False
        self._calls.append(now)
        return True


Forward = Callable[[BridgeTool, Mapping[str, object]], ToolResult]


def with_deadline(work: Callable[[], ToolResult], timeout_s: float) -> ToolResult:
    """``work()``, or BridgeError once ``timeout_s`` has passed. The thread cannot be killed; it is
    a daemon, so it never keeps the bridge alive."""
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = work()
        except BaseException as exc:  # carried to the caller, never swallowed
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise BridgeError(f"timed out after {timeout_s:.0f}s")
    error = box.get("error")
    if isinstance(error, BaseException):
        raise error
    value = box.get("value")
    if not isinstance(value, ToolResult):
        raise BridgeError("the upstream call returned nothing")
    return value


@dataclass
class Bridge:
    config: BridgeConfig
    forward: Forward
    catalog: Callable[[], Catalog]
    log: Callable[[Mapping[str, object]], None]
    clock: Callable[[], float] = time.time
    guard: Guard = field(init=False)

    def __post_init__(self) -> None:
        self.guard = Guard(self.config.max_calls_per_hour, self.clock)

    # JSON-RPC ---------------------------------------------------------------------

    def handle(self, message: object) -> dict[str, object] | None:
        if not isinstance(message, dict):
            return _error(None, -32600, "not a JSON-RPC object")
        ident, method = message.get("id"), message.get("method")
        if "id" not in message:
            return None  # a notification (notifications/initialized, ...): no answer
        params = message.get("params")
        args = params if isinstance(params, dict) else {}
        if method == "initialize":
            version = args.get("protocolVersion")
            return _ok(
                ident,
                {
                    "protocolVersion": version if isinstance(version, str) else PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": "2"},
                },
            )
        if method == "ping":
            return _ok(ident, {})
        if method == "tools/list":
            return _ok(ident, {"tools": self.listing()})
        if method == "tools/call":
            name, arguments = args.get("name"), args.get("arguments")
            text, failed = self.call(
                name if isinstance(name, str) else "",
                arguments if isinstance(arguments, dict) else {},
            )
            return _ok(ident, {"content": [{"type": "text", "text": text}], "isError": failed})
        return _error(ident, -32601, f"method not found: {method}")

    def listing(self) -> list[dict[str, object]]:
        """The allowlisted tools of the upstreams the user is logged in to.

        With the upstream's own description and input schema when it could be read; a tool the
        upstream does not offer is left out; if the catalog is unreadable the allowlist is listed
        with a permissive schema (the upstream still validates every call).
        """
        found = self.catalog()
        tools: list[dict[str, object]] = []
        for tool in TOOLS:
            if tool.upstream not in found:
                continue  # not logged in to that upstream
            offered = found[tool.upstream]
            entry: Mapping[str, object] = {}
            if offered is not None:
                if tool.upstream_tool not in offered:
                    continue
                entry = offered[tool.upstream_tool]
            schema = entry.get("inputSchema")
            description = entry.get("description")
            tools.append(
                {
                    "name": tool.name,
                    "description": (
                        f"[{UPSTREAMS[tool.upstream].name}, "
                        f"{'DRAFT only' if tool.draft else 'read only'}] "
                        f"{description if isinstance(description, str) else tool.description}"
                    )[:1500],
                    "inputSchema": schema
                    if isinstance(schema, dict)
                    else {"type": "object", "additionalProperties": True},
                }
            )
        return tools

    # one call ---------------------------------------------------------------------

    def call(self, name: str, arguments: Mapping[str, object]) -> tuple[str, bool]:
        started = self.clock()
        tool = BY_NAME.get(name)
        if tool is None:
            self._log("-", name[:64], started, 0, "refused")
            return REFUSAL.format(name=name[:64] or "(no name)"), True
        if len(json.dumps(arguments).encode("utf-8")) > MAX_ARGUMENT_BYTES:
            return f"{tool.name}: the arguments are larger than {MAX_ARGUMENT_BYTES} bytes", True
        if not self.guard.allow():
            self._log(tool.upstream, tool.name, started, 0, "limit")
            return (
                f"rate limit: at most {self.config.max_calls_per_hour} bridge calls per hour "
                "per session; ask the lead",
                True,
            )
        try:
            result = with_deadline(lambda: self.forward(tool, arguments), self.config.timeout_s)
        except BridgeError as exc:
            self._log(tool.upstream, tool.name, started, 0, "timeout")
            return scrub(str(exc)), True
        except VibeError as exc:
            self._log(tool.upstream, tool.name, started, 0, "failed")
            return scrub(
                f"{exc.message}" + (f" (run: {exc.recovery})" if exc.recovery else "")
            ), True
        except OSError as exc:
            self._log(tool.upstream, tool.name, started, 0, "failed")
            return f"the call failed ({exc.__class__.__name__})", True
        if result.bytes > self.config.max_result_bytes:
            self._log(tool.upstream, tool.name, started, result.bytes, "too-large")
            return (
                f"result too large: {result.bytes} bytes is over the "
                f"{self.config.max_result_bytes} byte cap; narrow the request",
                True,
            )
        self._log(
            tool.upstream,
            tool.name,
            started,
            result.bytes,
            "upstream-error" if result.is_error else "ok",
        )
        return result.text, result.is_error

    def _log(self, upstream: str, tool: str, started: float, size: int, outcome: str) -> None:
        self.log(
            {
                "at": datetime.fromtimestamp(started, UTC).isoformat(timespec="seconds"),
                "upstream": upstream,
                "tool": tool,
                "ms": round((self.clock() - started) * 1000),
                "bytes": size,
                "outcome": outcome,
            }
        )


def _ok(ident: object, result: Mapping[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": ident, "result": dict(result)}


def _error(ident: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def serve(bridge: Bridge, source: TextIO, sink: TextIO) -> None:
    """Newline-delimited JSON-RPC until the client closes its end."""
    for line in source:
        if not line.strip():
            continue
        try:
            message: object = json.loads(line)
        except ValueError:
            reply: dict[str, object] | None = _error(None, -32700, "parse error")
        else:
            reply = bridge.handle(message)
        if reply is not None:
            sink.write(scrub(json.dumps(reply, separators=(",", ":"))) + "\n")
            sink.flush()


# --- the log ------------------------------------------------------------------------------


def log_path(root: Path) -> Path:
    return root / LOG_NAME


def append_log(root: Path, entry: Mapping[str, object]) -> None:
    """One line per call. Best effort: a log that cannot be written never fails a call."""
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(log_path(root), os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        return


def print_add_command(config_dir: str = "~/.claude-extra") -> str:
    """The command that registers the bridge with the extra account's config dir. Printed only."""
    return (
        f"CLAUDE_CONFIG_DIR={config_dir} claude mcp add --scope user {SERVER_NAME} "
        "-- vibe bridge serve"
    )
