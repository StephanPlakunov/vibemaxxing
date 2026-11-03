"""vibe-bridge: the allowlist, the refusals, the routing of a call, the cost guards, the log.

Fakes only: a fake `claude` and `cursor-agent` on a private PATH, a scratch HOME, no real account.
The last test runs the server as a real process over stdio, the way Claude Code on extra starts it.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.cursor_fakes import StubToken
from tests.test_run_cursor import NOW_S, World
from vibemaxxing import bridge, cli, launch, route
from vibemaxxing.bridge import Bridge, BridgeConfig, BridgeError, parse_config
from vibemaxxing.errors import NetworkError, UsageError

REPO = Path(__file__).resolve().parent.parent


def tool_call(name: str, ident: int = 7, **arguments: object) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": ident,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


class Recorder:
    def __init__(self) -> None:
        self.picked: list[str] = []
        self.executed: list[tuple[str, str]] = []
        self.entries: list[Mapping[str, object]] = []
        self.now = 1_000_000.0

    def bridge(self, *, config: BridgeConfig | None = None, account: str = "main") -> Bridge:
        def pick(connector: str) -> route.Route:
            self.picked.append(connector)
            return route.Route(account, "claude", "test")

        def execute(
            chosen: route.Route, tool: bridge.BridgeTool, args: Mapping[str, object]
        ) -> str:
            self.executed.append((chosen.alias, tool.name))
            return '{"ok": true}'

        return Bridge(
            config or BridgeConfig(), pick, execute, self.entries.append, clock=lambda: self.now
        )


# --- the protocol --------------------------------------------------------------------------


def test_initialize_list_and_ping() -> None:
    rec = Recorder().bridge()

    init = rec.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-03-26"}})  # fmt: skip
    assert init is not None
    result = init["result"]
    assert isinstance(result, dict)
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"] == {"name": "vibe-bridge", "version": "0.1"}
    assert rec.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert rec.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"}) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {},
    }
    error = rec.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert error is not None and error["error"]["code"] == -32601  # type: ignore[index]
    bad = rec.handle([1])
    assert bad is not None and bad["error"]["code"] == -32600  # type: ignore[index]


def test_the_listed_tools_are_reads_and_drafts_only() -> None:
    listing = Recorder().bridge().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listing is not None
    tools = listing["result"]["tools"]  # type: ignore[index]
    names = {tool["name"] for tool in tools}

    assert names == {
        "slack_read_channel", "slack_read_thread", "slack_search", "slack_read_profile",
        "atlassian_get_issue", "atlassian_search_jql",
        "atlassian_get_page", "m365_search_mail", "m365_read_mail", "m365_search_chat",
        "m365_read_chat", "outlook_create_draft",
    }  # fmt: skip
    for name in names:
        assert not any(word in name for word in ("send", "post", "delete", "react", "transition"))
    for tool in tools:
        assert tool["inputSchema"]["additionalProperties"] is False
        assert ("Draft only" in tool["description"]) == tool["name"].endswith("_draft")


@pytest.mark.parametrize(
    "name",
    [
        "slack_send_message",
        "slack_post_message",
        "slack_add_reaction",
        "slack_delete_message",
        "atlassian_create_issue",
        "atlassian_transition_issue",
        "atlassian_add_comment",
        "outlook_send_mail",
        "m365_delete_mail",
        "",
        "../../etc/passwd",
    ],
)
def test_everything_else_is_refused_with_ask_the_lead(name: str) -> None:
    rec = Recorder()
    server = rec.bridge()

    reply = server.handle(tool_call(name))

    assert reply is not None
    result = reply["result"]
    assert isinstance(result, dict) and result["isError"] is True
    text = result["content"][0]["text"]  # type: ignore[index]
    assert "refused" in text and "ask the lead" in text
    assert rec.picked == [] and rec.executed == []  # no account was even chosen
    assert rec.entries[-1]["outcome"] == "refused"


def test_arguments_are_checked_before_anything_is_spent() -> None:
    rec = Recorder()
    server = rec.bridge()

    for call, message in (
        (tool_call("slack_read_channel"), "missing argument channel"),
        (tool_call("slack_read_channel", channel="C1", extra="x"), "unknown argument extra"),
        (tool_call("slack_read_channel", channel=5), "argument channel must be a string"),
        (tool_call("slack_read_channel", channel="C1", limit="9"), "limit must be a integer"),
        (tool_call("slack_read_channel", channel="C1", limit=True), "limit must be a integer"),
    ):
        reply = server.handle(call)
        assert reply is not None
        assert message in reply["result"]["content"][0]["text"]  # type: ignore[index]
    assert rec.picked == []


def test_a_good_call_picks_by_connector_and_logs_without_content() -> None:
    rec = Recorder()
    server = rec.bridge()

    reply = server.handle(tool_call("atlassian_get_issue", issue="ZAL-901"))

    assert reply is not None
    assert reply["result"] == {
        "content": [{"type": "text", "text": '{"ok": true}'}],
        "isError": False,
    }
    assert rec.picked == ["atlassian"] and rec.executed == [("main", "atlassian_get_issue")]
    (entry,) = rec.entries
    assert set(entry) == {"at", "account", "kind", "tool", "ms", "outcome"}
    assert entry["account"] == "main" and entry["tool"] == "atlassian_get_issue"
    assert "ZAL-901" not in json.dumps(entry)


# --- the cost guards -------------------------------------------------------------------------


def test_calls_per_hour_are_capped_and_the_window_slides() -> None:
    rec = Recorder()
    server = rec.bridge(config=BridgeConfig(max_calls_per_hour=2))

    for _ in range(2):
        assert server.call("slack_search", {"query": "x"})[1] is False
    text, failed = server.call("slack_search", {"query": "x"})
    assert failed and "at most 2 bridge calls per hour" in text
    assert len(rec.executed) == 2 and rec.entries[-1]["outcome"] == "limit"

    rec.now += 3601
    assert server.call("slack_search", {"query": "x"})[1] is False


def test_a_failed_execution_is_an_error_result_not_a_crash() -> None:
    rec = Recorder()

    def boom(chosen: route.Route, tool: bridge.BridgeTool, args: Mapping[str, object]) -> str:
        raise BridgeError("claude exited with 1")

    server = Bridge(
        BridgeConfig(), lambda c: route.Route("main", "claude", "t"), boom, rec.entries.append
    )

    assert server.call("slack_search", {"query": "x"}) == ("claude exited with 1", True)
    assert rec.entries[-1]["outcome"] == "failed"


def test_no_account_available_is_an_error_result() -> None:
    def nobody(connector: str) -> route.Route:
        raise UsageError("no account can take this run (main: spent)", "vibe list")

    entries: list[Mapping[str, object]] = []
    server = Bridge(BridgeConfig(), nobody, lambda *a: "", entries.append)

    text, failed = server.call("slack_search", {"query": "x"})

    assert failed and "no account can take this call" in text
    assert entries[-1]["outcome"] == "no-account"


def test_a_call_that_takes_too_long_is_killed(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_home / "slow"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(BridgeError, match="timed out after 1s"):
        bridge.run_headless([str(script)], None, 1.0)
    with pytest.raises(BridgeError, match="exited with 3"):
        bridge.run_headless(["/bin/sh", "-c", "exit 3"], None, 5.0)
    with pytest.raises(BridgeError, match="returned nothing"):
        bridge.run_headless(["/bin/sh", "-c", "true"], None, 5.0)
    with pytest.raises(BridgeError, match="could not start"):
        bridge.run_headless([str(tmp_home / "missing")], None, 5.0)


def test_the_log_is_owner_only_and_best_effort(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"

    bridge.append_log(root, {"tool": "x", "outcome": "ok"})
    bridge.append_log(root, {"tool": "y", "outcome": "ok"})

    path = bridge.log_path(root)
    assert [json.loads(line)["tool"] for line in path.read_text().splitlines()] == ["x", "y"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    bridge.append_log(path / "not-a-dir", {"tool": "z"})  # cannot be written: no exception


def test_config_defaults_and_overrides_and_refusals() -> None:
    assert parse_config(None) == BridgeConfig()
    config = parse_config(
        {"timeout_s": 30, "max_calls_per_hour": 5, "claude_model": "sonnet",
         "tools": {"slack_search": "slack_search_public"}}
    )  # fmt: skip
    assert (config.timeout_s, config.max_calls_per_hour, config.claude_model) == (30.0, 5, "sonnet")
    assert bridge.upstream_name(config, bridge.BY_NAME["slack_search"]) == "slack_search_public"
    for bad in (
        "x", {"surprise": 1}, {"timeout_s": 0}, {"timeout_s": True}, {"max_calls_per_hour": 0},
        {"claude_model": ""}, {"tools": {"slack_send_message": "x"}}, {"tools": []},
        # nothing in the config can send a bridge call to Cursor, or change who it runs on
        {"order": [{"account": "cursor"}]}, {"cursor_effort": "low"}, {"cursor_read_mode": "ask"},
    ):  # fmt: skip
        with pytest.raises(UsageError, match=r"routing\.bridge"):
            parse_config(bad)


def test_the_add_command_is_printed_not_run(
    capsys: pytest.CaptureFixture[str], tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "ENABLED", True)
    ctx = cli.Context(
        root=tmp_home / ".vibemaxxing",
        client=None,  # type: ignore[arg-type]
        port=None,  # type: ignore[arg-type]
        now_s=NOW_S,
    )

    assert cli.main(["bridge", "add-command"], context=ctx) == 0

    out = capsys.readouterr().out
    assert "CLAUDE_CONFIG_DIR=~/.claude-extra claude mcp add --scope user vibe-bridge" in out
    assert "-- vibe bridge serve" in out and "vibe does not" in out


# --- the real runs, on fakes -----------------------------------------------------------------


@pytest.fixture
def world(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    return world


def bridge_for(world: World, config: BridgeConfig | None = None) -> Bridge:
    cfg = config or BridgeConfig()
    ctx = world.ctx()
    svc = ctx.services()
    routing = route.Routing()
    return Bridge(
        cfg,
        launch.bridge_pick(svc, routing, cfg),
        launch.bridge_execute(svc, cfg, lambda alias: {"CLAUDE_CODE_OAUTH_TOKEN": f"{alias}-lent"}),
        lambda entry: bridge.append_log(world.root, entry),
    )


def fresh_claude_account(world: World, alias: str, *, weekly: int, active: bool = False) -> None:
    """Like World.claude_account, but with numbers as young as the real clock the bridge reads."""
    import time

    world.claude_account(alias, session=5, weekly=weekly, active=active)
    cache = json.loads((world.root / "usage" / f"{alias}.json").read_text())
    cache["fetched_at"] = time.time() - 10
    (world.root / "usage" / f"{alias}.json").write_text(json.dumps(cache))


def test_a_read_runs_on_main_within_budget_with_only_that_tool_allowed(world: World) -> None:
    fresh_claude_account(world, "main", weekly=1, active=True)  # 1% is within any day's budget
    fresh_claude_account(world, "extra", weekly=1)
    server = bridge_for(world)

    text, failed = server.call("slack_read_channel", {"channel": "C123", "limit": 5})

    assert (text, failed) == ('{"from": "claude"}', False)
    (call,) = world.claude_calls(full=True)
    argv = call["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "-p" and "Call the Slack tool `slack_read_channel` exactly once" in argv[1]
    assert argv[argv.index("--model") + 1] == "haiku"  # the cheap model
    assert argv[argv.index("--tools") + 1] == ""  # no built-in tools
    assert argv[argv.index("--allowedTools") + 1] == "mcp__claude_ai_Slack__slack_read_channel"
    assert "--no-session-persistence" in argv
    assert call["has_token"] is False  # main is the live login: no override
    assert call["has_config_dir"] is False
    assert world.fake.calls() == []
    (entry,) = [json.loads(x) for x in bridge.log_path(world.root).read_text().splitlines()]
    assert entry["account"] == "main" and entry["tool"] == "slack_read_channel"
    assert "C123" not in json.dumps(entry)


def test_the_callers_config_dir_and_token_do_not_leak_into_the_nested_run(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh_claude_account(world, "main", weekly=1, active=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(world.home / ".claude-extra"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "EXTRATOKEN00000000")

    bridge_for(world).call("slack_search", {"query": "x"})

    (call,) = world.claude_calls(full=True)
    # never the caller's (extra's) token or config dir: main's own token, lent explicitly
    env = call["env"]
    assert isinstance(env, dict)
    assert call["has_config_dir"] is False
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "main-lent" and "EXTRATOKEN" not in json.dumps(env)


def test_a_main_that_is_not_the_live_login_is_lent_its_token_for_the_call(world: World) -> None:
    fresh_claude_account(world, "main", weekly=1)
    fresh_claude_account(world, "extra", weekly=1, active=True)  # extra is the live login

    bridge_for(world).call("atlassian_get_issue", {"issue": "ZAL-1"})

    (call,) = world.claude_calls(full=True)
    assert call["has_token"] is True


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("slack_search", {"query": "release"}),
        ("slack_read_profile", {"user": "U1"}),
        ("outlook_create_draft", {"to": "a@b.c", "subject": "s", "body": "b"}),
        ("atlassian_get_issue", {"issue": "ZAL-1"}),
    ],
)
def test_a_call_main_cannot_take_is_refused_and_never_goes_to_cursor(
    world: World, tool: str, arguments: dict[str, object]
) -> None:
    fresh_claude_account(world, "main", weekly=100, active=True)  # spent
    fresh_claude_account(world, "extra", weekly=1)
    world.cursor_usage()  # Cursor has plenty of room and a working login

    text, failed = bridge_for(world).call(tool, arguments)

    assert failed
    assert "the bridge only runs on main" in text and "Cursor cannot be limited to one tool" in text
    assert world.fake.calls() == []  # no cursor-agent run: no read, no draft
    assert world.claude_calls() == []  # and nothing on extra either
    assert world.cursor_client.requests == []  # Cursor was not even asked for its usage
    entries = [json.loads(x) for x in bridge.log_path(world.root).read_text().splitlines()]
    assert entries[-1]["outcome"] == "no-account"


def test_main_with_unknown_usage_is_refused_too(world: World) -> None:
    # No usage cache and nothing queued for the endpoint: unknown, so unavailable.
    world.claude_account("main", session=1, weekly=1, active=True)
    cache = world.root / "usage" / "main.json"
    cache.unlink()
    world.client.queue(NetworkError("offline", "x"))

    text, failed = bridge_for(world).call("slack_search", {"query": "x"})

    assert failed and "usage unknown" in text
    assert world.claude_calls() == [] and world.fake.calls() == []


def test_no_config_key_and_no_route_can_start_a_cursor_run_from_the_bridge(world: World) -> None:
    ctx = world.ctx()
    execute = launch.bridge_execute(ctx.services(), BridgeConfig(), lambda alias: {})

    with pytest.raises(BridgeError, match="never runs on Cursor"):
        execute(
            route.Route("cursor", "cursor", "x"), bridge.BY_NAME["slack_search"], {"query": "x"}
        )

    assert world.fake.calls() == []
    # the order the bridge decides by is fixed in code: main only, whatever settings.json says
    (world.root / "settings.json").write_text(
        json.dumps({"schema": 1, "routing": {"order": {"needs": [{"account": "cursor"}]}}})
    )
    fresh_claude_account(world, "main", weekly=100, active=True)
    world.cursor_usage()
    routing = route.read_routing(world.root)
    pick = launch.bridge_pick(ctx.services(), routing, BridgeConfig())
    with pytest.raises(UsageError, match="only runs on main"):
        pick("slack")
    assert world.fake.calls() == []


def test_m365_runs_on_main_with_its_own_tool_allowed(world: World) -> None:
    fresh_claude_account(world, "main", weekly=1, active=True)

    _, failed = bridge_for(world).call("m365_search_mail", {"query": "invoice"})

    assert failed is False
    (call,) = world.claude_calls(full=True)
    assert call["argv"][call["argv"].index("--allowedTools") + 1] == (  # type: ignore[index]
        "mcp__claude_ai_Microsoft_365__outlook_email_search"
    )


def test_the_nested_run_has_three_fences_and_an_empty_workspace(world: World) -> None:
    fresh_claude_account(world, "main", weekly=1, active=True)

    bridge_for(world).call("slack_read_profile", {"user": "U1"})

    (call,) = world.claude_calls(full=True)
    argv = call["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--permission-prompts") + 1] == "none"  # nobody answers: deny, not ask
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert "mcp__claude_ai_Slack__slack_create_conversation" in denied
    assert "mcp__claude_ai_Slack__slack_update_list" in denied
    assert argv[argv.index("--allowedTools") + 1] not in denied
    assert Path(str(call["cwd"])).resolve() == (world.root / "bridge-workspace").resolve()


def test_every_allowlisted_tool_is_allowed_alone_and_every_write_of_its_server_is_denied() -> None:
    config = BridgeConfig()
    for tool in bridge.TOOLS:
        argv = bridge.claude_argv(config, tool, "p")
        allowed = argv[argv.index("--allowedTools") + 1]
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        assert allowed not in denied
        writes = bridge.WRITE_TOOLS[tool.server]
        expected = {
            config.claude_tool_pattern.format(server=tool.server.replace(" ", "_"), tool=w)
            for w in writes
            if w != tool.upstream
        }
        assert set(denied) == expected
        assert "--permission-prompts" in argv and "--tools" in argv
    draft = bridge.claude_argv(config, bridge.BY_NAME["outlook_create_draft"], "p")
    deny = draft[draft.index("--disallowedTools") + 1].split(",")
    # a draft tool is itself on the write list, and is the one thing its own call allows
    assert "mcp__claude_ai_Microsoft_365__outlook_create_draft" not in deny
    assert "mcp__claude_ai_Microsoft_365__outlook_send_mail" in deny
    assert "mcp__claude_ai_Microsoft_365__outlook_send_draft" in deny
    assert draft[draft.index("--allowedTools") + 1] == (
        "mcp__claude_ai_Microsoft_365__outlook_create_draft"
    )


def test_the_bridge_never_uses_extra(world: World) -> None:
    fresh_claude_account(world, "main", weekly=100, active=True)
    fresh_claude_account(world, "extra", weekly=1)  # plenty of room, but no connectors

    _, failed = bridge_for(world).call("slack_search", {"query": "x"})

    assert failed and world.claude_calls() == []


def test_a_failing_nested_run_is_reported_without_content(world: World) -> None:
    fresh_claude_account(world, "main", weekly=1, active=True)
    os.environ["FAKE_CLAUDE_EXIT"] = "4"
    try:
        text, failed = bridge_for(world).call("slack_search", {"query": "secret-topic"})
    finally:
        del os.environ["FAKE_CLAUDE_EXIT"]

    assert failed and text == "claude exited with 4"
    assert "secret-topic" not in text


# --- a session on extra, end to end -------------------------------------------------------------

_LAUNCHER = """
import sys
from tests.fakes import FakeHttpClient, FakeKeychain
from vibemaxxing import cli
from tests.cursor_fakes import StubToken
import os, time
ctx = cli.Context(root=__import__("pathlib").Path(os.environ["VIBE_ROOT"]), client=FakeHttpClient(),
                  port=FakeKeychain(blob=os.environ["VIBE_BLOB"]), now_s=time.time(),
                  prompt=lambda _: "", browser=lambda _: True,
                  cursor_client=FakeHttpClient(), cursor_token=StubToken())
from vibemaxxing import bridge
bridge.ENABLED = True  # the test enables it in its own process; no setting can
sys.exit(cli.main(["bridge", "serve"], context=ctx))
"""


def test_a_session_on_extra_calls_the_bridge_over_stdio_in_a_scratch_config(
    world: World, tmp_home: Path
) -> None:
    """What Claude Code on extra does: start `vibe bridge serve`, list, call, read the answer."""
    fresh_claude_account(world, "main", weekly=1, active=True)
    fresh_claude_account(world, "extra", weekly=1)
    assert world.port.blob is not None
    env = {
        **os.environ,
        "VIBE_ROOT": str(world.root),
        "VIBE_BLOB": world.port.blob,
        "PYTHONPATH": str(REPO),
        "HOME": str(tmp_home),
    }
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        tool_call("atlassian_get_issue", 3, issue="ZAL-901"),
        tool_call("slack_send_message", 4, channel="C1", text="hello"),
    ]

    done = subprocess.run(
        [sys.executable, "-c", _LAUNCHER],
        input="\n".join(json.dumps(r) for r in requests) + "\n",
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    answers = [json.loads(line) for line in done.stdout.splitlines()]
    assert [a["id"] for a in answers] == [1, 2, 3, 4]  # the notification got no answer
    listed = answers[1]["result"]["tools"]
    assert "slack_send_message" not in {tool["name"] for tool in listed}
    assert answers[2]["result"] == {
        "content": [{"type": "text", "text": '{"from": "claude"}'}],
        "isError": False,
    }
    refused = answers[3]["result"]
    assert refused["isError"] is True and "ask the lead" in refused["content"][0]["text"]
    (call,) = world.claude_calls(full=True)
    assert call["argv"][call["argv"].index("--allowedTools") + 1] == (  # type: ignore[index]
        "mcp__claude_ai_Atlassian_Rovo__getJiraIssue"
    )
    log = [json.loads(x) for x in bridge.log_path(world.root).read_text().splitlines()]
    assert [(e["tool"], e["outcome"]) for e in log] == [
        ("atlassian_get_issue", "ok"),
        ("slack_send_message", "refused"),
    ]
    assert "ZAL-901" not in bridge.log_path(world.root).read_text()
    assert StubToken().value not in done.stdout + done.stderr


def test_a_fenced_answer_is_handed_back_as_the_json_inside() -> None:
    fenced = json.dumps({"result": '```json\n{"a": 1}\n```'})

    assert bridge.extract_result(fenced) == '{"a": 1}'
    assert bridge.extract_result(json.dumps({"result": '{"a": 1}'})) == '{"a": 1}'
    assert bridge.extract_result("plain text") == "plain text"
    assert bridge.extract_result("```\nx\n```") == "x"


# --- round 3: fences that do not depend on the person's settings ---------------------------------


def test_the_fences_do_not_depend_on_the_users_claude_settings() -> None:
    config = BridgeConfig()

    argv = bridge.claude_argv(config, bridge.BY_NAME["slack_read_profile"], "p")

    # measured live: "project" alone hides the connectors; "user" reaches them and drops the rest
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert argv[argv.index("--settings") + 1] == '{"disableAllHooks": true}'
    assert argv[argv.index("--permission-mode") + 1] == "manual"  # explicit, not a settings default
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--allowedTools") + 1] == "mcp__claude_ai_Slack__slack_read_user_profile"
    assert (
        "mcp__claude_ai_Slack__slack_create_conversation"
        in argv[argv.index("--disallowedTools") + 1]
    )


def test_a_denied_call_is_a_failure_not_an_ok_result() -> None:
    denied = json.dumps({"result": "I could not", "permission_denials": [{"tool_name": "x"}]})
    errored = json.dumps({"result": "boom", "is_error": True})

    assert bridge.verdict(denied) == "1 tool call(s) denied by the permission fence"
    assert bridge.verdict(errored) == "the run reported an error"
    assert bridge.verdict(json.dumps({"result": "ok", "permission_denials": []})) is None
    assert bridge.verdict("not json") is None and bridge.verdict("[1]") is None


def test_run_headless_raises_on_a_denial_even_when_the_exit_code_is_zero(tmp_home: Path) -> None:
    script = tmp_home / "denier"
    script.write_text(
        '#!/bin/sh\necho \'{"result":"no","permission_denials":[{"tool_name":"t"}]}\'\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(BridgeError, match="denied by the permission fence"):
        bridge.run_headless([str(script)], None, 5.0)


def test_a_tools_override_cannot_map_a_tool_onto_a_write_tool() -> None:
    with pytest.raises(UsageError, match="write tool slack_create_conversation"):
        parse_config({"tools": {"slack_search": "slack_create_conversation"}})
    with pytest.raises(UsageError, match="write tool createJiraIssue"):
        parse_config({"tools": {"atlassian_get_issue": "createJiraIssue"}})
    # a draft tool keeps its own default, and a harmless remap is fine
    assert parse_config({"tools": {"outlook_create_draft": "outlook_create_draft"}})
    assert parse_config({"tools": {"slack_search": "slack_search_public"}})


def test_a_nested_run_inherits_none_of_the_callers_secrets(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh_claude_account(world, "main", weekly=1, active=True)
    monkeypatch.setenv("GH_TOKEN", "GHTOKEN0000000000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ANTHROPICKEY00000000")
    monkeypatch.setenv("HARMLESS", "kept")

    bridge_for(world).call("slack_search", {"query": "x"})

    (call,) = world.claude_calls(full=True)
    seen = call["env"]
    assert isinstance(seen, dict)
    assert "GH_TOKEN" not in seen and "ANTHROPIC_API_KEY" not in seen
    assert seen["HARMLESS"] == "kept"
    assert not [
        name for name in seen if name != "CLAUDE_CODE_OAUTH_TOKEN" and name.endswith("_TOKEN")
    ]


# --- the user settings preflight (connectors need the user source, so its rules still count) ---


def settings(tmp_home: Path, document: object) -> Path:
    config = tmp_home / ".claude"
    config.mkdir(exist_ok=True)
    (config / "settings.json").write_text(
        document if isinstance(document, str) else json.dumps(document)
    )
    return config


def test_settings_without_an_mcp_allow_rule_are_fine(tmp_home: Path) -> None:
    assert bridge.settings_problem(tmp_home / ".claude") is None  # no file at all
    ok = {"model": "sonnet", "permissions": {"allow": ["Bash(git status)", "Read"], "deny": ["x"]}}
    assert bridge.settings_problem(settings(tmp_home, ok)) is None
    assert bridge.settings_problem(settings(tmp_home, {"permissions": {}})) is None
    assert bridge.settings_problem(settings(tmp_home, {"hooks": {}})) is None


@pytest.mark.parametrize(
    "document",
    [
        {"permissions": {"allow": ["mcp__claude_ai_Slack__slack_send_message"]}},
        {"permissions": {"allow": ["Read", "mcp__claude_ai_Slack"]}},
        {"permissions": {"allow": ["MCP"]}},
        {"permissions": {"allow": "mcp__x"}},
        "{not json",
        "[1]",
    ],
)
def test_an_mcp_allow_rule_or_unreadable_settings_refuse_the_call(
    tmp_home: Path, document: object
) -> None:
    config = settings(tmp_home, document)

    if document == "[1]":
        assert bridge.settings_problem(config) is None  # a list has no permissions: nothing allowed
        return
    assert bridge.settings_problem(config) is not None


def test_a_call_with_such_a_settings_file_is_refused_before_anything_runs(
    world: World, tmp_home: Path
) -> None:
    fresh_claude_account(world, "main", weekly=1, active=True)
    settings(tmp_home, {"permissions": {"allow": ["mcp__claude_ai_Slack__slack_send_message"]}})

    text, failed = bridge_for(world).call("slack_search", {"query": "x"})

    assert failed and text.startswith("refused:") and "allow rule for an MCP tool" in text
    assert world.claude_calls() == []
    # and the unsafe file is the only reason: with it removed the same call runs
    (tmp_home / ".claude" / "settings.json").unlink()
    assert bridge_for(world).call("slack_search", {"query": "x"})[1] is False


def test_the_bridge_is_disabled_until_a_live_denial_has_been_shown(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = cli.Context(
        root=tmp_home / ".vibemaxxing",
        client=None,  # type: ignore[arg-type]
        port=None,  # type: ignore[arg-type]
        now_s=NOW_S,
    )

    assert bridge.ENABLED is False  # the shipped value
    assert cli.main(["bridge", "serve"], context=ctx) == 2
    assert cli.main(["bridge", "add-command"], context=ctx) == 2
    err = capsys.readouterr().err
    assert err.count("the bridge is disabled") == 2 and "non-allowed tool" in err
    assert "claude mcp add" not in err  # nothing to register


# --- the fence check: the model is offered exactly the one allowed tool ---------------------------

FIXTURE = json.loads(
    (Path(__file__).with_name("fixtures") / "init_tools_2026-11-03.json").read_text()
)["tools"]
ALLOWED = "mcp__claude_ai_Slack__slack_read_user_profile"


def init_text(tools: list[str], *, pending: bool = False, mode: str = "default") -> str:
    servers = sorted({t.split("__")[1] for t in FIXTURE})
    return json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "permissionMode": mode,
            "tools": tools,
            "mcp_servers": [
                {
                    "name": s,
                    "status": "pending" if pending and s == "claude_ai_Slack" else "connected",
                }
                for s in servers
            ],
        }
    )


class Probes:
    """A fake probe runner: offers the recorded connectors' tools minus --disallowedTools."""

    def __init__(self, *, offered: list[str] | None = None, pending: int = 0) -> None:
        self.offered = list(FIXTURE if offered is None else offered)
        self.pending = pending
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        assert "stream-json" in argv and "--verbose" in argv
        assert argv[argv.index("-p") + 1] == bridge.PROBE_PROMPT  # no real prompt in a probe
        denied = (
            set(argv[argv.index("--disallowedTools") + 1].split(","))
            if "--disallowedTools" in argv
            else set()
        )
        tools = [t for t in self.offered if t not in denied]
        if self.pending > 0:
            self.pending -= 1
            return init_text([t for t in tools if "claude_ai_Slack" not in t], pending=True)
        return init_text(tools)


def fence(**kw: object) -> tuple[bridge.Fence, list[float], list[float]]:
    now = [1_000.0]
    sleeps: list[float] = []
    f = bridge.Fence(clock=lambda: now[0], sleep=sleeps.append, **kw)  # type: ignore[arg-type]
    return f, now, sleeps


TOOL = bridge.BY_NAME["slack_read_profile"]


def test_the_recorded_listing_is_the_real_one() -> None:
    assert len(FIXTURE) == 172 and ALLOWED in FIXTURE
    assert any(t.startswith("mcp__chrome-devtools__") for t in FIXTURE)
    assert any("send_mail" in t for t in FIXTURE)  # write tools were offered


def test_the_init_event_is_parsed_and_judged() -> None:
    listing = bridge.parse_init("noise\n" + init_text([ALLOWED]) + "\n" + '{"type":"result"}')
    assert listing is not None and listing.tools == (ALLOWED,) and listing.settled
    assert bridge.fence_problem(listing, ALLOWED) is None
    assert bridge.parse_init("no events here") is None and bridge.parse_init("{broken") is None

    other = bridge.parse_init(
        init_text([ALLOWED, "mcp__x__a", "mcp__x__b", "mcp__x__c", "mcp__x__d"])
    )
    assert other is not None
    problem = bridge.fence_problem(other, ALLOWED)
    assert (
        problem is not None
        and "4 tool(s) other than the allowed one" in problem
        and "and 1 more" in problem
    )
    empty = bridge.parse_init(init_text([]))
    assert empty is not None and "allowed tool is not offered" in str(
        bridge.fence_problem(empty, ALLOWED)
    )
    odd = bridge.parse_init(init_text([ALLOWED], mode="bypassPermissions"))
    assert odd is not None and "bypassPermissions" in str(bridge.fence_problem(odd, ALLOWED))
    builtin = bridge.parse_init(init_text([ALLOWED, "Bash"]))
    assert builtin is not None and bridge.fence_problem(builtin, ALLOWED) is not None
    pending = bridge.parse_init(init_text([], pending=True))
    assert pending is not None and not pending.settled


def test_discovery_then_a_full_deny_list_then_a_verification() -> None:
    probes = Probes()
    f, _, _ = fence()

    extra = f.deny_list(BridgeConfig(), TOOL, "real prompt", probes)

    assert sorted(extra) == sorted(t for t in FIXTURE if t != ALLOWED)  # all servers incl. chrome
    assert len(probes.calls) == 2
    discovery, verification = probes.calls
    assert "--disallowedTools" not in discovery  # discovery sees everything
    denied = verification[verification.index("--disallowedTools") + 1].split(",")
    assert ALLOWED not in denied and "mcp__chrome-devtools__click" in denied
    assert "mcp__claude_ai_Microsoft_365__outlook_send_mail" in denied
    for argv in probes.calls:  # the other fences are on in a probe too
        assert argv[argv.index("--permission-prompts") + 1] == "none"
        assert argv[argv.index("--tools") + 1] == "" and "--setting-sources" in argv


def test_the_result_is_cached_per_argv_and_rechecked_after_ten_minutes() -> None:
    probes = Probes()
    f, now, _ = fence()
    f.deny_list(BridgeConfig(), TOOL, "p", probes)

    f.deny_list(BridgeConfig(), TOOL, "another prompt", probes)  # same argv hash: no probe
    assert len(probes.calls) == 2

    other = bridge.BY_NAME["slack_search"]  # another tool: discovery reused, one verification
    f.deny_list(BridgeConfig(), other, "p", probes)
    assert len(probes.calls) == 3

    now[0] += bridge.RECHECK_S + 1  # the connector list may have moved: both again
    f.deny_list(BridgeConfig(), TOOL, "p", probes)
    assert len(probes.calls) == 5


def test_a_connector_that_gains_a_tool_is_caught_and_denied_after_the_recheck() -> None:
    probes = Probes()
    f, now, _ = fence()
    f.deny_list(BridgeConfig(), TOOL, "p", probes)

    probes.offered.append("mcp__claude_ai_Slack__slack_new_writer")  # appears later
    now[0] += bridge.RECHECK_S + 1
    extra = f.deny_list(BridgeConfig(), TOOL, "p", probes)

    assert "mcp__claude_ai_Slack__slack_new_writer" in extra  # rediscovered, and denied


def test_something_that_cannot_be_denied_fails_the_check_and_forces_a_rediscovery() -> None:
    class Stubborn(Probes):
        def __call__(self, argv: list[str]) -> str:
            self.calls.append(argv)
            denied = (
                set(argv[argv.index("--disallowedTools") + 1].split(","))
                if "--disallowedTools" in argv
                else set()
            )
            return init_text([t for t in FIXTURE if t not in denied] and [ALLOWED, "Bash"])

    probes = Stubborn()
    f, _, _ = fence()

    with pytest.raises(BridgeError, match="fence check failed: 1 tool"):
        f.deny_list(BridgeConfig(), TOOL, "p", probes)
    before = len(probes.calls)
    with pytest.raises(BridgeError):
        f.deny_list(BridgeConfig(), TOOL, "p", probes)
    assert len(probes.calls) >= before + 2  # nothing was cached: discovery and verification again


def test_the_allowed_tool_missing_from_the_connectors_is_refused() -> None:
    probes = Probes(offered=[t for t in FIXTURE if t != ALLOWED])
    f, _, _ = fence()

    with pytest.raises(BridgeError, match="allowed tool is not offered"):
        f.deny_list(BridgeConfig(), TOOL, "p", probes)


def test_a_pending_connector_is_waited_for_never_read_as_no_tools() -> None:
    probes = Probes(pending=2)
    f, _, sleeps = fence()

    extra = f.deny_list(BridgeConfig(), TOOL, "p", probes)

    assert ALLOWED not in extra and len(sleeps) == 2  # slept, retried, then found Slack connected
    stuck = Probes(pending=99)
    g, _, naps = fence()
    with pytest.raises(BridgeError, match="stayed pending"):
        g.deny_list(BridgeConfig(), TOOL, "p", stuck)
    assert len(stuck.calls) == bridge.PENDING_RETRIES and len(naps) == bridge.PENDING_RETRIES - 1


def test_a_probe_without_an_init_event_is_a_failure() -> None:
    f, _, _ = fence()

    with pytest.raises(BridgeError, match="printed no init event"):
        f.deny_list(BridgeConfig(), TOOL, "p", lambda argv: "nothing useful")


# --- through the bridge, on the fake claude ------------------------------------------------------


def test_a_real_call_runs_the_probes_first_then_the_call_with_everything_else_denied(
    world: World,
) -> None:
    fresh_claude_account(world, "main", weekly=1, active=True)
    server = bridge_for(world)

    text, failed = server.call("slack_read_profile", {"user": "U1"})

    assert (text, failed) == ('{"from": "claude"}', False)
    assert len(world.claude_probes()) == 2
    (call,) = world.claude_calls(full=True)
    argv = call["argv"]
    assert isinstance(argv, list)
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert sorted(denied) == sorted(t for t in FIXTURE if t != ALLOWED)
    assert argv[argv.index("--allowedTools") + 1] == ALLOWED
    server.call("slack_read_profile", {"user": "U2"})
    assert len(world.claude_probes()) == 2  # cached: no new probe


def test_a_fence_that_does_not_hold_stops_the_call_before_it_runs(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh_claude_account(world, "main", weekly=1, active=True)
    monkeypatch.setenv(
        "FAKE_CLAUDE_EXTRA_TOOLS", "mcp__user_server__do_anything"
    )  # cannot be denied

    text, failed = bridge_for(world).call("slack_read_profile", {"user": "U1"})

    assert failed and "fence check failed" in text and "do_anything" in text
    assert world.claude_calls() == []  # the real call never started
    entries = [json.loads(x) for x in bridge.log_path(world.root).read_text().splitlines()]
    assert entries[-1]["outcome"] == "failed"


def test_an_unexpected_permission_mode_stops_the_call(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh_claude_account(world, "main", weekly=1, active=True)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "bypassPermissions")

    text, failed = bridge_for(world).call("slack_read_profile", {"user": "U1"})

    assert failed and "bypassPermissions" in text and world.claude_calls() == []


def test_a_connector_still_pending_is_retried_through_the_bridge(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh_claude_account(world, "main", weekly=1, active=True)
    monkeypatch.setenv("FAKE_CLAUDE_PENDING", "1")
    monkeypatch.setattr("time.sleep", lambda _: None)

    _, failed = bridge_for(world).call("slack_read_profile", {"user": "U1"})

    assert failed is False and len(world.claude_probes()) >= 3  # one was pending, then settled


def test_the_shipped_bridge_is_still_disabled_until_a_live_init_shows_the_allowed_tool_alone() -> (
    None
):
    assert bridge.ENABLED is False
