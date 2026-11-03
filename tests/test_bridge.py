# ruff: noqa: E501
"""vibe-bridge v2: no model in the path. The allowlist, the forwarding, the OAuth login, the token.

Everything runs against `FakeUpstream` (an HttpClient) and `FakeSecrets`: no socket to a vendor, no
Keychain, no login. The one real socket is the loopback listener for the browser's redirect.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.fake_upstream import FakeReceiver, FakeSecrets, FakeUpstream, text_result
from vibemaxxing import bridge, cli, mcp_oauth, route
from vibemaxxing.bridge import ATLASSIAN, BY_NAME, SLACK, TOOLS, Bridge, BridgeConfig, parse_config
from vibemaxxing.errors import NeedsLoginError, UsageError
from vibemaxxing.httpclient import HTTPError, HttpResponse, UrllibClient
from vibemaxxing.mcp_client import McpClient, McpError
from vibemaxxing.mcp_oauth import Tokens
from vibemaxxing.redact import Secret

REPO = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000.0
EMPTY_SCHEMA: dict[str, object] = {"type": "object", "properties": {}}


def echo(arguments: Mapping[str, object]) -> dict[str, object]:
    return text_result(json.dumps({"ok": True, "got": dict(arguments)}))


def upstream_with(upstream: mcp_oauth.Upstream, names: list[str], **kw: object) -> FakeUpstream:
    fake = FakeUpstream(upstream, **kw)  # type: ignore[arg-type]
    for name in names:
        fake.tools[name] = (
            f"{name} does a thing",
            {"type": "object", "properties": {"q": {"type": "string"}}},
            echo,
        )
    return fake


def seed(
    root: Path, secrets: FakeSecrets, fake: FakeUpstream, *, expires_at: float | None = NOW + 3600
) -> None:
    mcp_oauth.save(
        root,
        secrets,
        fake.upstream,
        Tokens(
            access=Secret(fake.access),
            refresh=Secret(fake.refresh),
            expires_at=expires_at,
            client_id="client-1",
            client_secret=None,
            scope="granted",
            token_endpoint=f"{fake.issuer_url}/token",
            resource=fake.upstream.mcp_url,
        ),
    )
    secrets.log.clear()


class Rig:
    """A bridge wired to fakes: one clock, one secret store, one or two fake upstreams."""

    def __init__(
        self, tmp_path: Path, *fakes: FakeUpstream, config: BridgeConfig | None = None
    ) -> None:
        self.root = tmp_path / ".vibemaxxing"
        self.secrets = FakeSecrets()
        self.now = NOW
        self.fakes = {fake.upstream.id: fake for fake in fakes}
        self.config = config or BridgeConfig(timeout_s=5.0)
        self.entries: list[Mapping[str, object]] = []
        for fake in fakes:
            seed(self.root, self.secrets, fake)

    def client(self) -> _Router:
        return _Router(self.fakes)

    def upstreams(self) -> bridge.Upstreams:
        return bridge.Upstreams(
            client=self.client(),
            secrets=self.secrets,
            root=self.root,
            config=self.config,
            clock=lambda: self.now,
        )

    def bridge(self) -> Bridge:
        ups = self.upstreams()
        return Bridge(
            self.config, ups.forward, ups.catalog, self.entries.append, clock=lambda: self.now
        )


class _Router:
    """One HttpClient in front of several fake upstreams, by host."""

    def __init__(self, fakes: Mapping[str, FakeUpstream]) -> None:
        self.fakes = fakes

    def request(self, method: str, url: str, **kw: object) -> HttpResponse:
        host = urllib.parse.urlsplit(url).hostname
        for fake in self.fakes.values():
            if host in fake.upstream.hosts:
                return fake.request(method, url, **kw)  # type: ignore[arg-type]
        raise AssertionError(f"a request went to a host nobody fakes: {url}")


@pytest.fixture
def slack() -> FakeUpstream:
    return upstream_with(
        SLACK,
        ["slack_read_user_profile", "slack_read_channel", "slack_search_public", "slack_send_message_draft",
         "slack_send_message", "slack_schedule_message", "slack_add_reaction", "slack_create_canvas"],
    )  # fmt: skip


@pytest.fixture
def atlassian() -> FakeUpstream:
    return upstream_with(
        ATLASSIAN,
        [
            "getJiraIssue",
            "searchJiraIssuesUsingJql",
            "createJiraIssue",
            "executeWrite",
            "deleteJiraIssue",
        ],
    )


def call(rig: Rig, name: str, **arguments: object) -> tuple[str, bool]:
    return rig.bridge().call(name, arguments)


# --- the allowlist ---------------------------------------------------------------------------------

READ_NAME = re.compile(
    r"^(slack_(read|search|list|get)_|get[A-Z]|list[A-Z]|search([A-Z]|$)|lookup[A-Z]|atlassianUserInfo$)"
)
WRITE_WORDS = re.compile(
    r"(send|schedule|post|create|update|delete|remove|add[A-Z_]|edit|upload|write|manage|execute|"
    r"move|archive|share|copy|merge|complete|destructive)",
    re.IGNORECASE,
)


def test_the_allowlist_is_reads_and_one_draft_and_nothing_that_writes() -> None:
    names = [tool.name for tool in TOOLS]
    assert len(names) == len(set(names))
    drafts = [tool for tool in TOOLS if tool.draft]
    assert [d.upstream_tool for d in drafts] == ["slack_send_message_draft"]
    for tool in TOOLS:
        if tool.draft:
            continue
        assert READ_NAME.match(tool.upstream_tool), tool.upstream_tool  # a read verb first
        assert not WRITE_WORDS.search(tool.upstream_tool), tool.upstream_tool
        assert not WRITE_WORDS.search(tool.name), tool.name
    assert {tool.upstream for tool in TOOLS} == {"slack", "atlassian"}  # no Microsoft 365


@pytest.mark.parametrize(
    "name",
    [
        "slack_send_message", "slack_schedule_message", "slack_add_reaction", "slack_create_canvas",
        "slack_update_canvas", "slack_create_conversation", "atlassian_createJiraIssue",
        "createJiraIssue", "atlassian_executeWrite", "atlassian_deleteJiraIssue",
        "outlook_send_mail", "m365_search_mail", "", "../x", "slack_read_channel ",
    ],
)  # fmt: skip
def test_everything_not_on_the_allowlist_is_refused_before_any_network(
    tmp_path: Path, slack: FakeUpstream, atlassian: FakeUpstream, name: str
) -> None:
    rig = Rig(tmp_path, slack, atlassian)

    text, failed = call(rig, name, channel="C1")

    assert failed and "refused" in text and "ask the lead" in text
    assert slack.requests == [] and atlassian.requests == []  # not even a token refresh
    assert rig.entries[-1]["outcome"] == "refused"


def test_a_tool_the_upstream_offers_is_still_unreachable_if_it_is_not_on_the_list(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    """The upstream offers slack_send_message. No allowlisted name maps to it, so nothing can call it."""
    rig = Rig(tmp_path, slack)
    assert "slack_send_message" in slack.tools
    reachable = {tool.upstream_tool for tool in TOOLS if tool.upstream == "slack"}

    assert "slack_send_message" not in reachable
    for name in list(BY_NAME):
        call(rig, name, q="x")
    called = {str(c["name"]) for c in slack.calls()}
    assert called <= reachable and called  # only allowlisted upstream tools were ever called
    assert not called & {"slack_send_message", "slack_schedule_message", "slack_add_reaction"}


def test_an_allowlisted_name_forwards_exactly_one_upstream_call_with_the_arguments(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    rig = Rig(tmp_path, slack)

    text, failed = call(rig, "slack_read_user_profile", user="U0BQSQ9BGTD", extra={"a": 1})

    assert not failed and json.loads(text) == {
        "ok": True,
        "got": {"user": "U0BQSQ9BGTD", "extra": {"a": 1}},
    }
    assert slack.calls() == [
        {"name": "slack_read_user_profile", "arguments": {"user": "U0BQSQ9BGTD", "extra": {"a": 1}}}
    ]
    assert slack.mcp_methods() == ["initialize", "notifications/initialized", "tools/call"]


def test_the_atlassian_names_carry_a_prefix_and_reach_the_vendors_own_name(
    tmp_path: Path, atlassian: FakeUpstream
) -> None:
    rig = Rig(tmp_path, atlassian)

    call(rig, "atlassian_getJiraIssue", issueIdOrKey="ZAL-901")

    assert atlassian.calls() == [{"name": "getJiraIssue", "arguments": {"issueIdOrKey": "ZAL-901"}}]


def test_the_draft_tool_is_the_upstreams_own_draft_tool(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    rig = Rig(tmp_path, slack)

    _, failed = call(rig, "slack_send_message_draft", channel_id="C1", message="hi")

    assert not failed
    assert [c["name"] for c in slack.calls()] == ["slack_send_message_draft"]


# --- what the session is shown ---------------------------------------------------------------------


def test_the_listing_is_the_allowlist_intersected_with_what_the_upstream_offers(
    tmp_path: Path, slack: FakeUpstream, atlassian: FakeUpstream
) -> None:
    rig = Rig(tmp_path, slack, atlassian)

    listing = rig.bridge().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert listing is not None
    tools = {t["name"]: t for t in listing["result"]["tools"]}  # type: ignore[index]
    # offered by the fake AND allowlisted
    assert set(tools) == {
        "slack_read_user_profile", "slack_read_channel", "slack_search_public",
        "slack_send_message_draft", "atlassian_getJiraIssue", "atlassian_searchJiraIssuesUsingJql",
    }  # fmt: skip
    for write in ("slack_send_message", "slack_add_reaction", "atlassian_createJiraIssue"):
        assert write not in tools  # offered upstream, never listed
    assert tools["slack_read_channel"]["inputSchema"]["properties"] == {"q": {"type": "string"}}
    assert "read only" in tools["slack_read_channel"]["description"]
    assert "DRAFT only" in tools["slack_send_message_draft"]["description"]


def test_an_upstream_you_are_not_logged_in_to_is_not_listed(
    tmp_path: Path, slack: FakeUpstream, atlassian: FakeUpstream
) -> None:
    rig = Rig(tmp_path, slack)  # atlassian is faked but never logged in
    rig.fakes["atlassian"] = atlassian

    names = {t["name"] for t in rig.bridge().listing()}

    assert names and all(not n.startswith("atlassian_") for n in names)
    assert atlassian.requests == []


def test_an_unreadable_catalog_lists_the_allowlist_with_a_permissive_schema(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    slack.always_401 = True
    rig = Rig(tmp_path, slack)

    listing = rig.bridge().listing()

    assert {t["name"] for t in listing} == {t.name for t in TOOLS if t.upstream == "slack"}
    assert all(
        t["inputSchema"] == {"type": "object", "additionalProperties": True} for t in listing
    )


def test_the_catalog_is_cached_and_a_failure_is_retried_sooner(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    rig = Rig(tmp_path, slack)
    server = rig.bridge()
    server.listing()
    first = len(slack.requests)

    server.listing()
    assert len(slack.requests) == first  # cached

    rig.now += bridge.CATALOG_TTL_S + 1
    server.listing()
    assert len(slack.requests) > first


# --- results ---------------------------------------------------------------------------------------


def test_text_is_returned_as_data_and_other_content_is_noted_not_passed(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    slack.tools["slack_read_channel"] = (
        "d", EMPTY_SCHEMA,
        lambda a: {"content": [{"type": "text", "text": "line one"}, {"type": "image", "data": "AAAA"},
                               {"type": "text", "text": "line two"}], "isError": False},
    )  # fmt: skip
    rig = Rig(tmp_path, slack)

    text, failed = call(rig, "slack_read_channel", channel_id="C1")

    assert not failed and text == "line one\nline two\n[non-text content omitted by the bridge]"


def test_an_upstream_error_result_is_an_error_result(tmp_path: Path, slack: FakeUpstream) -> None:
    slack.tools["slack_read_channel"] = (
        "d",
        EMPTY_SCHEMA,
        lambda a: text_result("channel_not_found", error=True),
    )
    rig = Rig(tmp_path, slack)

    text, failed = call(rig, "slack_read_channel", channel_id="C1")

    assert failed and text == "channel_not_found" and rig.entries[-1]["outcome"] == "upstream-error"


def test_a_result_over_the_size_cap_is_refused_not_cut(tmp_path: Path, slack: FakeUpstream) -> None:
    slack.tools["slack_read_channel"] = ("d", EMPTY_SCHEMA, lambda a: text_result("x" * 5000))
    rig = Rig(tmp_path, slack, config=BridgeConfig(timeout_s=5.0, max_result_bytes=1000))

    text, failed = call(rig, "slack_read_channel", channel_id="C1")

    assert failed and "result too large: 5000 bytes is over the 1000 byte cap" in text
    assert "xxxxx" not in text  # no content in the refusal
    assert rig.entries[-1]["outcome"] == "too-large" and rig.entries[-1]["bytes"] == 5000


def test_oversized_arguments_are_refused_before_the_network(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    rig = Rig(tmp_path, slack)

    text, failed = call(
        rig, "slack_send_message_draft", message="y" * (bridge.MAX_ARGUMENT_BYTES + 1)
    )

    assert failed and "arguments are larger" in text and slack.requests == []


def test_an_event_stream_answer_and_a_paged_listing_work(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    slack.sse = True
    slack.page_size = 3
    rig = Rig(tmp_path, slack)

    text, failed = call(rig, "slack_read_channel", q="x")
    listing = rig.bridge().listing()

    assert not failed and json.loads(text)["ok"] is True
    assert len(listing) == 4  # all four allowlisted tools the fake offers, across pages


def test_the_session_id_is_carried_and_the_session_is_closed(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    rig = Rig(tmp_path, slack)

    call(rig, "slack_read_channel", q="x")

    posts = [s for s in slack.requests if s.method == "POST" and s.url == SLACK.mcp_url]
    assert "Mcp-Session-Id" not in posts[0].headers
    assert all(p.headers["Mcp-Session-Id"] == "sess-1" for p in posts[1:])
    assert slack.requests[-1].method == "DELETE"


def test_a_jsonrpc_error_and_a_garbled_answer_are_failures_with_a_clear_message(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    rig = Rig(tmp_path, slack)
    del slack.tools["slack_read_channel"]  # the upstream no longer offers it

    text, failed = call(rig, "slack_read_channel", q="x")

    assert failed and "the upstream answered an error (-32602)" in text


# --- the token --------------------------------------------------------------------------------------


def test_the_bearer_header_is_the_stored_access_token_and_never_anything_else(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    rig = Rig(tmp_path, slack)

    call(rig, "slack_read_channel", q="x")

    posts = [s for s in slack.requests if s.url == SLACK.mcp_url and s.method == "POST"]
    assert {p.headers["Authorization"] for p in posts} == {f"Bearer {slack.access}"}
    bodies = b"".join((s.body or b"") for s in slack.requests)
    assert slack.access.encode() not in bodies  # the token is a header, never a body


def test_no_token_reaches_the_log_an_error_or_the_result(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    slack.tools["slack_read_channel"] = (
        "d", EMPTY_SCHEMA, lambda a: text_result("fine"),
    )  # fmt: skip
    rig = Rig(tmp_path, slack)
    out, _ = call(
        rig, "slack_read_channel", q=f"{slack.access}"
    )  # an argument that holds the token
    slack.always_401 = True
    err_text, failed = call(rig, "slack_read_channel", q="x")
    bridge.append_log(rig.root, rig.entries[0])

    assert failed and slack.access not in err_text and slack.refresh not in err_text
    assert slack.access not in out and slack.refresh not in out
    log = bridge.log_path(rig.root).read_text() if bridge.log_path(rig.root).exists() else ""
    assert slack.access not in log and slack.refresh not in log
    for entry in rig.entries:
        assert slack.access not in json.dumps(entry)
    # and the non-secret description on disk holds no token either
    meta = mcp_oauth.meta_path(rig.root, SLACK).read_text()
    assert slack.access not in meta and slack.refresh not in meta


def test_an_expired_token_is_refreshed_and_the_new_one_is_stored_before_it_is_used(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    rig = Rig(tmp_path, slack)
    seed(rig.root, rig.secrets, slack, expires_at=NOW - 10)
    old_access, old_refresh = slack.access, slack.refresh

    _, failed = call(rig, "slack_read_channel", q="x")

    assert not failed
    assert slack.access != old_access and slack.refresh != old_refresh  # the fake rotated both
    assert rig.secrets.items[mcp_oauth.service(SLACK, "access")] == slack.access
    assert rig.secrets.items[mcp_oauth.service(SLACK, "refresh")] == slack.refresh
    token_post = next(s for s in slack.requests if s.url.endswith("/token"))
    assert token_post.form()["refresh_token"] == old_refresh
    first_mcp = next(i for i, s in enumerate(slack.requests) if s.url == SLACK.mcp_url)
    assert slack.requests.index(token_post) < first_mcp
    # the rotated pair was written (to the Keychain) before the MCP request that used it
    assert ("write", mcp_oauth.service(SLACK, "refresh")) in rig.secrets.log


def test_a_token_about_to_expire_is_refreshed_too(tmp_path: Path, slack: FakeUpstream) -> None:
    rig = Rig(tmp_path, slack)
    seed(rig.root, rig.secrets, slack, expires_at=NOW + 30)  # inside the 60 s skew

    call(rig, "slack_read_channel", q="x")

    assert any(s.url.endswith("/token") for s in slack.requests)


def test_a_401_refreshes_once_and_retries(tmp_path: Path, slack: FakeUpstream) -> None:
    rig = Rig(tmp_path, slack)
    slack.reject_next_mcp = 1

    _, failed = call(rig, "slack_read_channel", q="x")

    assert not failed
    assert sum(1 for s in slack.requests if s.url.endswith("/token")) == 1


def test_a_login_the_upstream_keeps_refusing_says_to_log_in_again(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    rig = Rig(tmp_path, slack)
    slack.reject_next_mcp = 99

    text, failed = call(rig, "slack_read_channel", q="x")

    assert failed and "does not accept the stored login" in text and "vibe bridge login" in text


def test_a_dead_refresh_token_says_to_log_in_again_and_echoes_nothing(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    rig = Rig(tmp_path, slack)
    seed(rig.root, rig.secrets, slack, expires_at=NOW - 10)
    slack.refresh_fails = True

    text, failed = call(rig, "slack_read_channel", q="x")

    assert failed and "refused the token request (HTTP 400, invalid_grant)" in text
    assert "vibe bridge login slack" in text
    assert "SECRETECHO" not in text and slack.refresh not in text  # the body is never repeated


def test_not_logged_in_is_a_clear_refusal_not_a_crash(tmp_path: Path, slack: FakeUpstream) -> None:
    rig = Rig(tmp_path)  # nothing seeded
    rig.fakes["slack"] = slack

    text, failed = call(rig, "slack_read_channel", q="x")

    assert failed and "not logged in to Slack" in text and "vibe bridge login slack" in text
    assert slack.requests == []


def test_another_process_that_refreshed_while_we_waited_is_not_refreshed_over(
    tmp_path: Path, slack: FakeUpstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spending a rotated refresh token twice would log the user out: re-read under the lock."""
    rig = Rig(tmp_path, slack)
    seed(rig.root, rig.secrets, slack, expires_at=NOW - 10)

    import contextlib

    @contextlib.contextmanager
    def other_process_refreshed(root: Path, upstream: mcp_oauth.Upstream):  # type: ignore[no-untyped-def]
        slack.access = "ACCESSFROMOTHERPROCESS1"
        slack.refresh = "REFRESHFROMOTHERPROC1"
        seed(rig.root, rig.secrets, slack, expires_at=NOW + 3600)
        yield

    monkeypatch.setattr(mcp_oauth, "_refresh_lock", other_process_refreshed)

    _, failed = call(rig, "slack_read_channel", q="x")

    assert not failed
    assert not any(s.url.endswith("/token") for s in slack.requests)  # no second refresh


def test_the_keychain_tool_gets_the_value_on_stdin_and_never_in_argv() -> None:
    calls: list[tuple[list[str], str | None]] = []
    stored: dict[str, str] = {}

    def runner(args, *, input, capture_output, text, encoding, timeout, check):  # type: ignore[no-untyped-def]
        calls.append((list(args), input))
        if args[1] == "-i":
            command = input or ""
            service = command.split(" -s ")[1].split(" ")[0]
            stored[service] = command.split(" -w ")[1].strip()
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "find-generic-password":
            service = args[args.index("-s") + 1]
            return subprocess.CompletedProcess(
                args, 0 if service in stored else 44, stored.get(service, "") + "\n", ""
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    store = mcp_oauth.MacSecretStore(runner)
    token = "eyJhbGciOiJIUzI1NiJ9.payloadpayload.signaturesignature"

    store.write("vibemaxxing-bridge-slack-access", token)
    assert store.read("vibemaxxing-bridge-slack-access") == token
    store.delete("vibemaxxing-bridge-slack-access")

    for argv, _stdin in calls:
        assert token not in " ".join(argv)  # never on a command line
    write_argv, write_stdin = calls[0]
    assert (
        write_argv == ["/usr/bin/security", "-i"]
        and write_stdin is not None
        and token in write_stdin
    )
    assert all("-w" not in argv[2:] or argv[1] == "find-generic-password" for argv, _ in calls[1:])


def test_the_keychain_store_refuses_awkward_values_and_a_truncated_readback() -> None:
    def runner(args, *, input, capture_output, text, encoding, timeout, check):  # type: ignore[no-untyped-def]
        if args[1] == "-i":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "truncated\n", "")  # what `security -i` can do

    store = mcp_oauth.MacSecretStore(runner)

    for bad in (
        "has space in it",
        'quote"d-token-value',
        "short",
        "x" * 4000,
        "new\nline-token-value",
    ):
        with pytest.raises(Exception) as caught:
            store.write("svc", bad)
        assert bad not in str(caught.value)
    with pytest.raises(Exception, match="did not keep 'svc' intact") as caught:
        store.write("svc", "validtokenvalue123")
    assert "validtokenvalue123" not in str(caught.value)


# --- the login ----------------------------------------------------------------------------------------


def run_login(
    tmp_path: Path,
    fake: FakeUpstream,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    drafts: bool = False,
    receiver: FakeReceiver | None = None,
) -> tuple[Tokens, FakeSecrets, list[str], Path]:
    root = tmp_path / ".vibemaxxing"
    secrets = FakeSecrets()
    opened: list[str] = []
    said: list[str] = []
    recv = receiver or FakeReceiver()

    def browser(url: str) -> bool:
        opened.append(url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        fake.challenge = query["code_challenge"]  # what the server will check the verifier against
        return True

    tokens = mcp_oauth.login(
        fake.upstream,
        client=_Router({fake.upstream.id: fake}),
        secrets=secrets,
        root=root,
        receiver=recv,
        open_browser=browser,
        say=said.append,
        now_s=NOW,
        client_id=client_id,
        client_secret=Secret(client_secret) if client_secret else None,
        drafts=drafts,
    )
    return tokens, secrets, opened, root


def test_atlassian_login_registers_a_public_client_and_asks_for_read_scopes_only(
    tmp_path: Path, atlassian: FakeUpstream
) -> None:
    receiver = FakeReceiver(port=8765)

    tokens, secrets, opened, root = run_login(tmp_path, atlassian, receiver=receiver)

    (url,) = opened
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith("https://mcp.atlassian.com/authorize?")
    assert query["client_id"] == "dcr-client-1" and query["response_type"] == "code"
    assert query["redirect_uri"] == "http://127.0.0.1:8765/callback"
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) >= 43
    assert query["state"] == receiver.states[0]
    assert query["resource"] == ATLASSIAN.mcp_url  # RFC 8707
    scopes = query["scope"].split()
    assert scopes == list(ATLASSIAN.read_scopes)
    assert not any(s.startswith(("write:", "delete:", "manage:")) for s in scopes)
    registration = next(s for s in atlassian.requests if s.url.endswith("/register"))
    assert registration.rpc()["token_endpoint_auth_method"] == "none"
    assert registration.rpc()["redirect_uris"] == ["http://127.0.0.1:8765/callback"]
    # the token exchange: code + the verifier whose challenge was sent, no secret
    exchange = next(s for s in atlassian.requests if s.url.endswith("/token")).form()
    assert exchange["grant_type"] == "authorization_code" and exchange["code"] == "auth-code-1"
    assert "client_secret" not in exchange
    # the tokens are in the (fake) Keychain; the file on disk holds no token
    assert secrets.items[mcp_oauth.service(ATLASSIAN, "access")] == tokens.access.reveal()
    meta = mcp_oauth.meta_path(root, ATLASSIAN)
    assert stat.S_IMODE(meta.stat().st_mode) == 0o600
    assert tokens.access.reveal() not in meta.read_text()
    assert tokens.refresh is not None and tokens.refresh.reveal() not in meta.read_text()


def test_slack_login_needs_an_app_of_your_own_and_sends_its_secret_only_to_the_token_endpoint(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    slack.dcr = False
    with pytest.raises(UsageError, match="no dynamic client registration"):
        run_login(tmp_path, slack)  # no client id or secret

    _, secrets, opened, root = run_login(
        tmp_path, slack, client_id="app-client-1", client_secret="APPSECRET000000001"
    )

    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))
    assert query["client_id"] == "app-client-1" and "client_secret" not in query
    assert "resource" not in query  # Slack's server is not sent RFC 8707's parameter
    assert query["scope"].split() == list(SLACK.read_scopes)  # no chat:write without --drafts
    assert "chat:write" not in query["scope"]
    assert "APPSECRET000000001" not in opened[0]
    exchange = next(s for s in slack.requests if s.url.endswith("/token")).form()
    assert (
        exchange["client_secret"] == "APPSECRET000000001"
    )  # client_secret_post, to the token endpoint
    assert secrets.items[mcp_oauth.service(SLACK, "client-secret")] == "APPSECRET000000001"
    assert "APPSECRET000000001" not in mcp_oauth.meta_path(root, SLACK).read_text()


def test_drafts_adds_exactly_the_draft_scope(tmp_path: Path, slack: FakeUpstream) -> None:
    _, _, opened, _ = run_login(
        tmp_path, slack, client_id="app-client-1", client_secret="APPSECRET000000001", drafts=True
    )

    scopes = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))["scope"].split()
    assert scopes == [*SLACK.read_scopes, "chat:write"]


def test_a_poisoned_discovery_document_cannot_send_anything_to_another_host(
    tmp_path: Path, atlassian: FakeUpstream
) -> None:
    atlassian.prm_authorization_server = "https://evil.example/as"
    router_calls: list[str] = []

    class Spy(_Router):
        def request(self, method: str, url: str, **kw: object) -> HttpResponse:
            router_calls.append(url)
            return super().request(method, url, **kw)

    with pytest.raises(UsageError, match="not on a host this bridge trusts"):
        mcp_oauth.discover(Spy({"atlassian": atlassian}), ATLASSIAN)

    assert all("evil.example" not in url for url in router_calls)
    atlassian.prm_authorization_server = None
    atlassian.issuer = "https://evil.example"  # the metadata names endpoints on another host
    with pytest.raises(UsageError, match="not on a host this bridge trusts"):
        mcp_oauth.discover(Spy({"atlassian": atlassian}), ATLASSIAN)


def test_a_wrong_code_verifier_is_refused_by_the_upstream_and_nothing_is_stored(
    tmp_path: Path, atlassian: FakeUpstream
) -> None:
    atlassian.code = "a-different-code"

    with pytest.raises(NeedsLoginError, match="refused the token request"):
        run_login(tmp_path, atlassian)

    assert not (tmp_path / ".vibemaxxing" / "bridge").exists()


def test_the_loopback_receiver_takes_the_redirect_and_checks_the_state() -> None:
    receiver = mcp_oauth.LoopbackReceiver(0)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        assert receiver.redirect_uri.startswith("http://127.0.0.1:")
        threading.Timer(
            0.2,
            lambda: opener.open(
                f"{receiver.redirect_uri}?code=the-code&state=good", timeout=5
            ).read(),
        ).start()
        assert receiver.wait("good", 5) == "the-code"
    finally:
        receiver.close()


def test_the_loopback_receiver_refuses_a_foreign_state_an_error_and_silence() -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for query, message in (
        ("code=c&state=other", "did not carry this login's state"),
        ("error=access_denied&state=good", "refused the login: access_denied"),
        ("state=good", "carried no code"),
    ):
        receiver = mcp_oauth.LoopbackReceiver(0)
        try:
            url = f"{receiver.redirect_uri}?{query}"
            threading.Timer(0.2, lambda url=url: opener.open(url, timeout=5).read()).start()
            with pytest.raises(UsageError, match=message):
                receiver.wait("good", 5)
        finally:
            receiver.close()
    quiet = mcp_oauth.LoopbackReceiver(0)
    try:
        with pytest.raises(UsageError, match="no answer from the browser"):
            quiet.wait("good", 0.2)
    finally:
        quiet.close()


# --- the cost guards and the log --------------------------------------------------------------------------


def test_a_call_that_takes_too_long_is_stopped(tmp_path: Path, slack: FakeUpstream) -> None:
    release = threading.Event()
    slack.tools["slack_read_channel"] = (
        "d",
        EMPTY_SCHEMA,
        lambda a: (release.wait(5), text_result("late"))[1],
    )
    rig = Rig(tmp_path, slack, config=BridgeConfig(timeout_s=0.3))
    started = time.monotonic()

    text, failed = call(rig, "slack_read_channel", q="x")
    release.set()

    assert failed and "timed out" in text
    assert time.monotonic() - started < 3
    assert rig.entries[-1]["outcome"] == "timeout"


def test_calls_per_hour_are_capped_and_the_window_slides(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    rig = Rig(tmp_path, slack, config=BridgeConfig(timeout_s=5.0, max_calls_per_hour=2))
    server = rig.bridge()

    assert server.call("slack_read_channel", {})[1] is False
    assert server.call("slack_read_channel", {})[1] is False
    text, failed = server.call("slack_read_channel", {})
    assert failed and "at most 2 bridge calls per hour" in text
    assert rig.entries[-1]["outcome"] == "limit"
    assert len(slack.calls()) == 2  # the third never reached the upstream

    rig.now += 3601
    assert server.call("slack_read_channel", {})[1] is False


def test_the_log_has_upstream_tool_time_and_size_but_no_content(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    slack.tools["slack_read_channel"] = (
        "d",
        EMPTY_SCHEMA,
        lambda a: text_result("SECRETMESSAGE body"),
    )
    rig = Rig(tmp_path, slack)

    call(rig, "slack_read_channel", channel_id="C0SECRETCHAN")
    for entry in rig.entries:
        bridge.append_log(rig.root, entry)

    path = bridge.log_path(rig.root)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    (line,) = path.read_text().splitlines()
    entry = json.loads(line)
    assert set(entry) == {"at", "upstream", "tool", "ms", "bytes", "outcome"}
    assert entry["upstream"] == "slack" and entry["tool"] == "slack_read_channel"
    assert entry["bytes"] == len("SECRETMESSAGE body") and entry["outcome"] == "ok"
    assert "SECRETMESSAGE" not in line and "C0SECRETCHAN" not in line
    bridge.append_log(path / "nope", {"x": 1})  # an unwritable log never fails a call


def test_the_config_has_no_key_that_adds_a_tool_or_an_upstream() -> None:
    assert parse_config(None) == BridgeConfig()
    assert parse_config({"timeout_s": 30, "max_calls_per_hour": 5, "max_result_bytes": 5000}) == (
        BridgeConfig(30.0, 5, 5000)
    )
    for bad in (
        "x", {"surprise": 1}, {"timeout_s": 0}, {"timeout_s": True}, {"max_calls_per_hour": 0},
        {"max_result_bytes": 10}, {"max_result_bytes": 10**9}, {"tools": {"x": "y"}},
        {"upstreams": ["m365"]}, {"claude_model": "haiku"}, {"order": []},
    ):  # fmt: skip
        with pytest.raises(UsageError, match=r"routing\.bridge"):
            parse_config(bad)


# --- the protocol ------------------------------------------------------------------------------------------


def test_initialize_ping_unknown_methods_and_notifications(
    tmp_path: Path, slack: FakeUpstream
) -> None:
    server = Rig(tmp_path, slack).bridge()

    init = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
    )
    assert init is not None and init["result"]["protocolVersion"] == "2025-03-26"  # type: ignore[index]
    assert init["result"]["serverInfo"] == {"name": "vibe-bridge", "version": "2"}  # type: ignore[index]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"}) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {},
    }
    error = server.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert error is not None and error["error"]["code"] == -32601  # type: ignore[index]
    bad = server.handle([1])
    assert bad is not None and bad["error"]["code"] == -32600  # type: ignore[index]


def test_serve_reads_lines_and_answers_in_order(tmp_path: Path, slack: FakeUpstream) -> None:
    server = Rig(tmp_path, slack).bridge()
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "slack_read_user_profile", "arguments": {"user": "U1"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "slack_send_message", "arguments": {}}},
    ]  # fmt: skip
    sink = io.StringIO()

    bridge.serve(
        server, io.StringIO("\n".join(json.dumps(x) for x in lines) + "\nnot json\n\n"), sink
    )

    answers = [json.loads(x) for x in sink.getvalue().splitlines()]
    assert [a.get("id") for a in answers] == [1, 2, 3, None]
    assert (
        answers[1]["result"]["isError"] is False
        and "U1" in answers[1]["result"]["content"][0]["text"]
    )
    assert (
        answers[2]["result"]["isError"] is True
        and "ask the lead" in answers[2]["result"]["content"][0]["text"]
    )
    assert answers[3]["error"]["code"] == -32700


# --- the CLI ------------------------------------------------------------------------------------------------

_LAUNCHER = """
import os, sys, time
from pathlib import Path
from tests import test_bridge as tb
from tests.fake_upstream import FakeSecrets
from vibemaxxing import cli, bridge

fake = tb.upstream_with(bridge.SLACK, ["slack_read_user_profile", "slack_send_message"])
secrets = FakeSecrets()
root = Path(os.environ["VIBE_ROOT"])
tb.seed(root, secrets, fake, expires_at=time.time() + 3600)
router = tb._Router({"slack": fake})
ctx = cli.Context(root=root, client=router, port=None, now_s=time.time(),
                  bridge_client=router, secrets=secrets)
sys.exit(cli.main(["bridge", "serve"], context=ctx))
"""


def test_a_scratch_mcp_client_reads_through_vibe_bridge_serve_as_a_real_process(
    tmp_path: Path,
) -> None:
    """What Claude Code on extra does: start `vibe bridge serve`, list, call, read, try a write."""
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "slack_read_user_profile", "arguments": {"user": "U0BQSQ9BGTD"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "slack_send_message", "arguments": {"channel_id": "C1", "message": "hi"}}},
    ]  # fmt: skip
    env = {
        **os.environ,
        "VIBE_ROOT": str(tmp_path / ".vibemaxxing"),
        "PYTHONPATH": str(REPO),
        "HOME": str(tmp_path),
    }

    done = subprocess.run(
        [sys.executable, "-c", _LAUNCHER],
        input="\n".join(json.dumps(r) for r in requests) + "\n",
        capture_output=True, text=True, env=env, cwd=REPO, timeout=60, check=False,
    )  # fmt: skip

    assert done.returncode == 0, done.stderr
    answers = [json.loads(line) for line in done.stdout.splitlines()]
    assert [a["id"] for a in answers] == [1, 2, 3, 4]
    assert {t["name"] for t in answers[1]["result"]["tools"]} == {
        "slack_read_user_profile"
    }  # not the writer
    read = answers[2]["result"]
    assert read["isError"] is False and "U0BQSQ9BGTD" in read["content"][0]["text"]
    refused = answers[3]["result"]
    assert refused["isError"] is True and "ask the lead" in refused["content"][0]["text"]
    log = [
        json.loads(x) for x in (tmp_path / ".vibemaxxing" / "bridge.log").read_text().splitlines()
    ]
    assert [(e["tool"], e["outcome"]) for e in log] == [
        ("slack_read_user_profile", "ok"),
        ("slack_send_message", "refused"),
    ]
    assert "U0BQSQ9BGTD" not in (tmp_path / ".vibemaxxing" / "bridge.log").read_text()


class CliRig:
    def __init__(self, tmp_path: Path, *fakes: FakeUpstream) -> None:
        self.root = tmp_path / ".vibemaxxing"
        self.secrets = FakeSecrets()
        self.fakes = {f.upstream.id: f for f in fakes}
        self.opened: list[str] = []
        self.receiver = FakeReceiver()
        self.asked: list[str] = []

    def ctx(self) -> cli.Context:
        router = _Router(self.fakes)

        def browser(url: str) -> bool:
            self.opened.append(url)
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            for fake in self.fakes.values():
                fake.challenge = query["code_challenge"]
            return True

        def prompt(text: str) -> str:
            self.asked.append("prompt")
            return "app-client-1"

        return cli.Context(
            root=self.root, client=router, port=None, now_s=NOW, clock=lambda: NOW,  # type: ignore[arg-type]
            prompt=prompt, browser=browser, bridge_client=router, secrets=self.secrets,
            secret_prompt=lambda _: (self.asked.append("secret"), "APPSECRET000000001")[1],
            receiver=lambda port: self.receiver,
        )  # fmt: skip

    def main(self, *argv: str) -> int:
        return cli.main(list(argv), context=self.ctx())


def test_status_login_tools_logout_through_the_cli(
    tmp_path: Path, atlassian: FakeUpstream, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = CliRig(tmp_path, atlassian)

    assert rig.main("bridge", "status") == 0
    assert "atlassian not logged in  (vibe bridge login atlassian)" in capsys.readouterr().out

    assert rig.main("bridge", "login", "atlassian") == 0
    captured = capsys.readouterr()
    assert "logged in to Atlassian; the token is in the Keychain" in captured.out
    assert "redirect URL for the login: http://127.0.0.1:8765/callback" in captured.err
    assert (
        atlassian.access not in captured.out + captured.err
        and atlassian.refresh not in captured.out + captured.err
    )
    assert rig.receiver.closed
    assert rig.asked == []  # Atlassian registers itself: no client id, no secret asked

    assert rig.main("bridge", "status") == 0
    status = capsys.readouterr().out
    assert "atlassian logged in; access token 60 min left" in status
    assert atlassian.access not in status

    assert rig.main("bridge", "tools", "atlassian") == 0
    tools = capsys.readouterr().out
    assert "atlassian_getJiraIssue" in tools and "offered" in tools
    assert (
        "atlassian_listJiraProjects" in tools and "NOT offered" in tools
    )  # allowlisted, not on this fake
    assert "createJiraIssue" not in tools  # a writer is never even named

    assert rig.main("bridge", "logout", "atlassian") == 0
    assert "logged out of atlassian" in capsys.readouterr().out
    assert rig.secrets.items == {} and not mcp_oauth.meta_path(rig.root, ATLASSIAN).exists()
    assert rig.main("bridge", "logout", "all") == 0
    assert "there was no login to remove" in capsys.readouterr().out


def test_slack_login_asks_for_the_client_id_and_a_hidden_secret(
    tmp_path: Path, slack: FakeUpstream, capsys: pytest.CaptureFixture[str]
) -> None:
    slack.dcr = False
    rig = CliRig(tmp_path, slack)

    assert rig.main("bridge", "login", "slack", "--drafts") == 0

    assert rig.asked == ["prompt", "secret"]
    err = capsys.readouterr().err
    assert "APPSECRET000000001" not in err
    assert "chat:write" in urllib.parse.unquote(rig.opened[0])
    assert mcp_oauth.service(SLACK, "client-secret") in rig.secrets.items


def test_an_unknown_upstream_and_microsoft_365_are_not_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = CliRig(tmp_path)

    with pytest.raises(SystemExit):
        rig.main("bridge", "login", "m365")
    assert "invalid choice" in capsys.readouterr().err


def test_add_command_prints_the_line_for_extras_config_dir_and_runs_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = CliRig(tmp_path)

    assert rig.main("bridge", "add-command") == 0

    out = capsys.readouterr().out
    assert (
        "CLAUDE_CONFIG_DIR=~/.claude-extra claude mcp add --scope user vibe-bridge -- vibe bridge serve"
        in out
    )
    assert "vibe does not" in out
    assert rig.main("bridge", "add-command", "--config-dir", "~/.claude-work") == 0
    assert "CLAUDE_CONFIG_DIR=~/.claude-work claude mcp" in capsys.readouterr().out


def test_the_bridge_has_no_model_and_no_nested_claude_left() -> None:
    source = (REPO / "src" / "vibemaxxing" / "bridge.py").read_text()
    for gone in (
        "claude_argv",
        "run_headless",
        "Fence",
        "settings_problem",
        "--allowedTools",
        "subprocess",
    ):
        assert gone not in source, gone
    launch = (REPO / "src" / "vibemaxxing" / "launch.py").read_text()
    assert "bridge" not in launch


# --- the HTTP layer the bridge relies on ---------------------------------------------------------------------


def test_the_urllib_client_returns_headers_caps_the_body_and_keeps_a_401s_headers() -> None:
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/401":
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Bearer resource_metadata="x"')
                self.end_headers()
                return
            body = b"x" * (5000 if self.path == "/big" else 10)
            self.send_response(200)
            self.send_header("Mcp-Session-Id", "s-1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    client = UrllibClient(frozenset({"127.0.0.1"}))
    try:
        ok = client.request("GET", f"{base}/ok", headers={})
        assert ok.status == 200 and ok.headers["mcp-session-id"] == "s-1"
        with pytest.raises(Exception, match="larger than 1000 bytes"):
            client.request("GET", f"{base}/big", headers={}, max_bytes=1000)
        with pytest.raises(HTTPError) as caught:
            client.request("GET", f"{base}/401", headers={})
        assert (
            caught.value.status == 401
            and "resource_metadata" in caught.value.headers["www-authenticate"]
        )
    finally:
        server.shutdown()
        server.server_close()


def test_the_bridge_client_talks_to_the_upstreams_hosts_only() -> None:
    from vibemaxxing.httpclient import BRIDGE_HOSTS

    assert (
        frozenset({"mcp.slack.com", "slack.com", "mcp.atlassian.com", "auth.atlassian.com"})
        == BRIDGE_HOSTS
    )
    assert SLACK.hosts | ATLASSIAN.hosts == BRIDGE_HOSTS
    client = UrllibClient(BRIDGE_HOSTS)
    for url in ("https://claude.ai/x", "https://api2.cursor.sh/x", "https://evil.example/x"):
        with pytest.raises(Exception, match="refusing to contact"):
            client.request("GET", url, headers={})


def test_an_mcp_client_reads_a_json_array_batch_and_rejects_a_foreign_answer(
    slack: FakeUpstream,
) -> None:
    seed_client = McpClient(
        _Router({"slack": slack}),
        SLACK.mcp_url,
        lambda force: Secret(slack.access),
        timeout_s=5,
        max_bytes=100_000,
    )
    assert [t["name"] for t in seed_client.list_tools()][:1]

    class Odd(FakeUpstream):
        def _reply(self, ident, result, headers=None, error=None):  # type: ignore[no-untyped-def]
            return self._json({"jsonrpc": "2.0", "id": 999, "result": {}})

    odd = Odd(SLACK, tools=dict(slack.tools), access=slack.access)
    broken = McpClient(
        _Router({"slack": odd}),
        SLACK.mcp_url,
        lambda force: Secret(odd.access),
        timeout_s=5,
        max_bytes=100_000,
    )
    with pytest.raises(McpError, match="holds no response to the request"):
        broken.call_tool("slack_read_channel", {})


def test_routing_still_accepts_a_bridge_member(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"schema": 1, "routing": {"bridge": {"max_calls_per_hour": 7}}})
    )

    routing = route.read_routing(tmp_path)

    assert parse_config(routing.bridge).max_calls_per_hour == 7
