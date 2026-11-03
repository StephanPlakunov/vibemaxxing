"""vibe-bridge: a local stdio MCP server that forwards one allowlisted call to a vendor's own MCP.

A session on the extra account has no Slack or Atlassian connector. It starts this server
(`vibe bridge serve`) and calls its tools. Each call is forwarded by vibe itself, as an MCP client,
straight to the vendor's remote MCP server, as exactly one allowlisted upstream tool with the
arguments given. **No model is anywhere in the path**, so there is no tool list for a prompt
injection to widen and nothing to fence with a prompt, a deny list or an init check.

The safety is the code:

* the allowlist below is the only thing that can be called. A name not on it is refused before any
  network is touched, whatever the upstream offers. Reads only: no draft tool either.
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

from vibemaxxing import jsonutil, mcp_oauth
from vibemaxxing.errors import DeadlineError, UsageError, VibeError
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
REFUSAL: Final = (
    "refused: {name} is not on the bridge allowlist. The bridge reads only; sending, drafting, "
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
    description: str  # our own words: nothing the vendor wrote is ever shown to the model
    # Our own argument list (name, JSON type). The vendor validates the values; any other argument
    # is still passed through, so a wrong guess here costs a hint, not a refusal.
    args: tuple[tuple[str, str], ...] = ()


_S, _I = "string", "integer"


def _t(
    upstream: str, prefix: str, tool: str, description: str, *args: tuple[str, str]
) -> BridgeTool:
    return BridgeTool(f"{prefix}{tool}", upstream, tool, description, args)


# The allowlist: READS ONLY. There is no draft tool and no write tool of any kind; a token that can
# write is never stored (mcp_oauth refuses a grant with a write scope). Tool names are the vendors'
# own (Slack's from a connector listing, Atlassian's from its v2 documentation); the argument names
# are the best known and are NOT verified: `vibe bridge tools <upstream>` compares them with the
# live server (names only).
TOOLS: Final = (
    _t("slack", "", "slack_read_channel", "Read recent messages of a channel.",
       ("channel_id", _S), ("limit", _I), ("cursor", _S)),
    _t("slack", "", "slack_read_thread", "Read one thread.",
       ("channel_id", _S), ("message_ts", _S)),
    _t("slack", "", "slack_read_user_profile", "Read a user's profile.", ("user_id", _S)),
    _t("slack", "", "slack_search_public", "Search public channels' messages.",
       ("query", _S), ("limit", _I), ("cursor", _S)),
    _t("slack", "", "slack_search_public_and_private",
       "Search messages in every channel the user can see.",
       ("query", _S), ("limit", _I), ("cursor", _S)),
    _t("slack", "", "slack_search_channels", "Search channels by name.", ("query", _S)),
    _t("slack", "", "slack_search_users", "Search users by name.", ("query", _S)),
    _t("slack", "", "slack_list_channel_members", "List the members of a channel.",
       ("channel_id", _S), ("limit", _I), ("cursor", _S)),
    _t("slack", "", "slack_list_user_channels", "List the channels a user is in.",
       ("user_id", _S), ("limit", _I), ("cursor", _S)),
    _t("slack", "", "slack_get_reactions", "Read the reactions on a message.",
       ("channel_id", _S), ("message_ts", _S)),
    _t("slack", "", "slack_read_canvas", "Read a canvas.", ("canvas_id", _S)),
    _t("slack", "", "slack_read_file", "Read a file's content.", ("file_id", _S)),
    _t("slack", "", "slack_read_list", "Read a list.", ("list_id", _S)),
    _t("atlassian", "atlassian_", "atlassianUserInfo", "Who the logged-in user is."),
    _t("atlassian", "atlassian_", "getAccessibleAtlassianResources",
       "The sites (cloud ids) the user can reach."),
    _t("atlassian", "atlassian_", "getJiraIssue", "Read one Jira issue.",
       ("cloudId", _S), ("issueIdOrKey", _S)),
    _t("atlassian", "atlassian_", "searchJiraIssuesUsingJql", "Search Jira with JQL.",
       ("cloudId", _S), ("jql", _S), ("maxResults", _I)),
    _t("atlassian", "atlassian_", "listJiraProjects", "List Jira projects.", ("cloudId", _S)),
    _t("atlassian", "atlassian_", "listJiraIssueTransitions",
       "List the transitions an issue could take (reads; moves nothing).",
       ("cloudId", _S), ("issueIdOrKey", _S)),
    _t("atlassian", "atlassian_", "listJiraIssueComments", "List an issue's comments.",
       ("cloudId", _S), ("issueIdOrKey", _S)),
    _t("atlassian", "atlassian_", "lookupJiraAccountId", "Find a Jira account id by name.",
       ("cloudId", _S), ("searchString", _S)),
    _t("atlassian", "atlassian_", "getConfluenceContent", "Read a Confluence page.",
       ("cloudId", _S), ("contentId", _S)),
    _t("atlassian", "atlassian_", "searchConfluence", "Search Confluence.",
       ("cloudId", _S), ("cql", _S)),
    _t("atlassian", "atlassian_", "listConfluenceContent", "List Confluence content.",
       ("cloudId", _S), ("spaceId", _S)),
    _t("atlassian", "atlassian_", "listConfluenceSpaces", "List Confluence spaces.",
       ("cloudId", _S)),
    _t("atlassian", "atlassian_", "getConfluenceSpace", "Read one Confluence space.",
       ("cloudId", _S), ("spaceId", _S)),
    _t("atlassian", "atlassian_", "listConfluenceComments", "List a page's comments.",
       ("cloudId", _S), ("contentId", _S)),
    _t("atlassian", "atlassian_", "search", "Search Atlassian products.", ("query", _S)),
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


# --- the upstreams ------------------------------------------------------------------------


class BridgeError(Exception):
    """A call that failed; the message is safe to show (no content, no secret)."""


class Upstreams:
    """The logged-in upstreams: forward one allowlisted call."""

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

    def logged_in(self, upstream_id: str) -> bool:
        """From the login's description on disk: no Keychain read, no request."""
        return mcp_oauth.has_login(self._root, UPSTREAMS[upstream_id])

    def _mcp(self, upstream: Upstream) -> McpClient:
        def token(force: bool, deadline: float | None) -> Secret:
            return mcp_oauth.refresh_tokens(
                upstream,
                client=self._client,
                secrets=self._secrets,
                root=self._root,
                now_s=self._clock(),
                force=force,
                deadline=deadline,
            ).access

        return McpClient(
            self._client,
            upstream.mcp_url,
            token,
            timeout_s=self._config.timeout_s,
            max_bytes=2 * self._config.max_result_bytes + 65_536,
        )

    def forward(
        self, tool: BridgeTool, arguments: Mapping[str, object], deadline: float
    ) -> ToolResult:
        """Exactly one upstream tool, the one this allowlisted name stands for."""
        return self._mcp(UPSTREAMS[tool.upstream]).call_tool(
            tool.upstream_tool, arguments, deadline=deadline
        )

    def upstream_tools(self, upstream: Upstream) -> list[dict[str, object]]:
        """What the live upstream offers. For `vibe bridge tools` only: never shown to a model."""
        deadline = time.monotonic() + self._config.timeout_s
        return self._mcp(upstream).list_tools(deadline=deadline)


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


Forward = Callable[[BridgeTool, Mapping[str, object], float], ToolResult]
LoggedIn = Callable[[str], bool]
REFUSED_LOGGED_PER_PROCESS: Final = 50
# The worker is asked to stop at its own deadline (every request, the refresh lock and the body
# read check it); this is only how long past that the caller waits before giving up on it.
WORKER_GRACE_S: Final = 3.0


def with_deadline(work: Callable[[float], ToolResult], timeout_s: float) -> ToolResult:
    """``work(deadline)``, abandoned if it has not returned a little after ``timeout_s``.

    ``deadline`` is a ``time.monotonic()`` moment the work itself is bound by, so a call that times
    out is not left running in the background: its request is cut off, and it never starts the
    refresh or the request it was waiting for. The thread is a daemon; it never keeps the bridge up.
    """
    deadline = time.monotonic() + timeout_s
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = work(deadline)
        except BaseException as exc:  # carried to the caller, never swallowed
            box["error"] = exc

    thread = threading.Thread(target=run, name="vibe-bridge-call", daemon=True)
    thread.start()
    thread.join(timeout_s + WORKER_GRACE_S)
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
    logged_in: LoggedIn
    log: Callable[[Mapping[str, object]], None]
    clock: Callable[[], float] = time.time
    guard: Guard = field(init=False)
    refused_logged: int = field(init=False, default=0)

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

        Entirely ours: the name, the description and the input schema are written in this file, so
        nothing the vendor says reaches the model through the listing, and a vendor that changes a
        tool changes nothing here. No network.
        """
        tools: list[dict[str, object]] = []
        for tool in TOOLS:
            if not self.logged_in(tool.upstream):
                continue
            tools.append(
                {
                    "name": tool.name,
                    "description": (
                        f"[{UPSTREAMS[tool.upstream].name}, read only] {tool.description} "
                        "Arguments go to the upstream unchanged."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {name: {"type": kind} for name, kind in tool.args},
                        "additionalProperties": True,
                    },
                }
            )
        return tools

    # one call ---------------------------------------------------------------------

    def call(self, name: str, arguments: Mapping[str, object]) -> tuple[str, bool]:
        started = self.clock()
        tool = BY_NAME.get(name)
        if tool is None:
            self._log_refused(name, started)
            return REFUSAL.format(name=name[:64] or "(no name)"), True
        try:
            size = len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError, RecursionError):
            return f"{tool.name}: the arguments are not plain JSON", True
        if size > MAX_ARGUMENT_BYTES:
            return f"{tool.name}: the arguments are larger than {MAX_ARGUMENT_BYTES} bytes", True
        if not self.guard.allow():
            self._log(tool.upstream, tool.name, started, 0, "limit")
            return (
                f"rate limit: at most {self.config.max_calls_per_hour} bridge calls per hour "
                "per session; ask the lead",
                True,
            )
        try:
            result = with_deadline(
                lambda deadline: self.forward(tool, arguments, deadline), self.config.timeout_s
            )
        except (BridgeError, DeadlineError) as exc:
            self._log(tool.upstream, tool.name, started, 0, "timeout")
            return scrub(str(exc) if isinstance(exc, BridgeError) else exc.message), True
        except VibeError as exc:
            self._log(tool.upstream, tool.name, started, 0, "failed")
            suffix = f" (run: {exc.recovery})" if exc.recovery else ""
            return scrub(f"{exc.message}{suffix}"), True
        except Exception as exc:
            # Whatever an upstream's answer or a number in it can provoke ends in an error result,
            # never in a dead bridge.
            self._log(tool.upstream, tool.name, started, 0, "failed")
            return f"the call failed ({exc.__class__.__name__})", True
        if result.bytes > self.config.max_result_bytes:
            self._log(tool.upstream, tool.name, started, result.bytes, "too-large")
            return (
                f"result too large: {result.bytes} bytes is over the "
                f"{self.config.max_result_bytes} byte cap; narrow the request",
                True,
            )
        outcome = "upstream-error" if result.is_error else "ok"
        self._log(tool.upstream, tool.name, started, result.bytes, outcome)
        return result.text, result.is_error

    def _log_refused(self, name: str, started: float) -> None:
        """Log a refusal, but not a loop of them: the first 50, then one line saying so."""
        self.refused_logged += 1
        if self.refused_logged <= REFUSED_LOGGED_PER_PROCESS:
            self._log("-", scrub(name[:64]), started, 0, "refused")
        elif self.refused_logged == REFUSED_LOGGED_PER_PROCESS + 1:
            self._log("-", "-", started, 0, "refused-suppressed")

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


MAX_LINE_BYTES: Final = 2_000_000


def serve(bridge: Bridge, source: TextIO, sink: TextIO) -> None:
    """Newline-delimited JSON-RPC until the client closes its end. Nothing a line says, nothing an
    upstream answers and no exception ends it: a bad line is answered with an error and the next
    one is read."""
    for line in source:
        if not line.strip():
            continue
        reply: dict[str, object] | None
        try:
            if len(line) > MAX_LINE_BYTES:
                reply = _error(None, -32600, "request too large")
            else:
                try:
                    message: object = jsonutil.loads(line)
                except ValueError:
                    reply = _error(None, -32700, "parse error")
                else:
                    reply = bridge.handle(message)
        except Exception as exc:
            reply = _error(None, -32603, f"internal error ({exc.__class__.__name__})")
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
