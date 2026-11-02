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
from vibemaxxing.errors import UsageError

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
        "slack_create_draft", "atlassian_get_issue", "atlassian_search_jql",
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
         "cursor_effort": "medium", "cursor_read_mode": None,
         "tools": {"slack_search": "slack_search_public"},
         "order": [{"account": "cursor"}, {"account": "main", "last_resort": True}]}
    )  # fmt: skip
    assert (config.timeout_s, config.max_calls_per_hour, config.claude_model) == (30.0, 5, "sonnet")
    assert config.cursor_read_mode is None
    assert bridge.upstream_name(config, bridge.BY_NAME["slack_search"]) == "slack_search_public"
    assert config.order[0] == route.Step("cursor")
    for bad in (
        "x", {"surprise": 1}, {"timeout_s": 0}, {"timeout_s": True}, {"max_calls_per_hour": 0},
        {"claude_model": ""}, {"cursor_effort": "ludicrous"}, {"cursor_read_mode": "agent"},
        {"tools": {"slack_send_message": "x"}}, {"tools": []},
    ):  # fmt: skip
        with pytest.raises(UsageError, match=r"routing\.bridge"):
            parse_config(bad)


def test_the_add_command_is_printed_not_run(
    capsys: pytest.CaptureFixture[str], tmp_home: Path
) -> None:
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
    assert call["has_config_dir"] is False and call["has_token"] is False


def test_a_main_that_is_not_the_live_login_is_lent_its_token_for_the_call(world: World) -> None:
    fresh_claude_account(world, "main", weekly=1)
    fresh_claude_account(world, "extra", weekly=1, active=True)  # extra is the live login

    bridge_for(world).call("atlassian_get_issue", {"issue": "ZAL-1"})

    (call,) = world.claude_calls(full=True)
    assert call["has_token"] is True


def test_over_budget_the_call_goes_to_cursor_with_haiku_low_and_read_only_mode(
    world: World,
) -> None:
    fresh_claude_account(world, "main", weekly=100, active=True)  # over every day's budget
    world.cursor_usage()  # included usage left

    text, failed = bridge_for(world).call("slack_search", {"query": "release"})

    assert (text, failed) == ('{"from": "cursor"}', False)
    (argv,) = world.fake.calls()
    assert argv[:5] == ["-p", "--output-format", "json", "--model", "claude-haiku-5-5-low"]
    assert "--approve-mcps" in argv
    assert argv[argv.index("--mode") + 1] == "ask"
    assert world.claude_calls() == []
    entries = [json.loads(x) for x in bridge.log_path(world.root).read_text().splitlines()]
    assert entries[0]["account"] == "cursor" and entries[0]["kind"] == "cursor"


def test_a_draft_on_cursor_is_not_put_in_read_only_mode(world: World) -> None:
    fresh_claude_account(world, "main", weekly=100, active=True)
    world.cursor_usage()

    bridge_for(world).call("slack_create_draft", {"channel": "C1", "text": "hi"})

    (argv,) = world.fake.calls()
    assert "--mode" not in argv


def test_m365_only_ever_goes_to_main_even_over_budget(world: World) -> None:
    fresh_claude_account(world, "main", weekly=100, active=True)
    world.cursor_usage()

    _, failed = bridge_for(world).call("m365_search_mail", {"query": "invoice"})

    assert failed is False
    assert world.fake.calls() == []  # cursor has no m365
    (call,) = world.claude_calls(full=True)
    assert call["argv"][call["argv"].index("--allowedTools") + 1] == (  # type: ignore[index]
        "mcp__claude_ai_Microsoft_365__outlook_email_search"
    )


def test_the_bridge_never_uses_extra(world: World) -> None:
    fresh_claude_account(world, "main", weekly=100, active=True)
    fresh_claude_account(world, "extra", weekly=1)  # plenty of room, but no connectors
    world.cursor_client.queue_json(200, {"shape": "changed"})  # cursor unknown
    world.cursor_client.queue_json(200, {})

    bridge_for(world).call("slack_search", {"query": "x"})

    (call,) = world.claude_calls(full=True)
    assert call["has_token"] is False  # ran on main (the live login), not on extra's token


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
        "mcp__claude_ai_Atlassian__getJiraIssue"
    )
    log = [json.loads(x) for x in bridge.log_path(world.root).read_text().splitlines()]
    assert [(e["tool"], e["outcome"]) for e in log] == [
        ("atlassian_get_issue", "ok"),
        ("slack_send_message", "refused"),
    ]
    assert "ZAL-901" not in bridge.log_path(world.root).read_text()
    assert StubToken().value not in done.stdout + done.stderr
