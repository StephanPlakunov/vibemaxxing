"""`vibe run` hands vibe-bridge to a Claude run whose account lacks what the bridge offers.

A run on an account vibe owns starts claude with a token override and without CLAUDE_CONFIG_DIR, so
a bridge registered in ~/.claude-extra is never read (a run in a config dir of its own may read it).
Fakes only: a fake claude and cursor-agent record their argv; nothing reaches a vendor or the
Keychain.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from tests.fake_upstream import FakeSecrets
from tests.test_bridge import ATLASSIAN, NOW, SLACK, CliRig, seed, upstream_with
from tests.test_run_cursor import World, auto_world
from vibemaxxing import bridge, mcp_oauth


def log_in(world: World, *upstreams: mcp_oauth.Upstream) -> None:
    """A bridge login on record (the description file); the tokens would be in the Keychain."""
    for upstream in upstreams:
        fake = upstream_with(upstream, [])
        seed(world.root, FakeSecrets(), fake)


@pytest.fixture
def world(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    world.cursor_usage()
    return world


def config_flag(argv: list[object]) -> str | None:
    return next((str(a) for a in argv if str(a).startswith("--mcp-config")), None)


# --- when it is added ---------------------------------------------------------------------------


def test_a_run_on_extra_gets_the_bridge_as_a_vibe_owned_mcp_config(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.claude_account("extra", session=5, weekly=5)
    log_in(world, ATLASSIAN)

    assert world.main("run", "extra", "--", "claude", "-p", "hello") == 0

    (call,) = world.claude_calls()
    argv = call["argv"]
    assert isinstance(argv, list)
    path = world.root / "bridge" / "mcp-config.json"
    assert (
        argv[0] == f"--mcp-config={path}"
    )  # the one-token form, first: a prompt is never swallowed
    assert argv[1:] == ["-p", "hello"]
    assert "--strict-mcp-config" not in argv  # the account's own servers stay
    err = capsys.readouterr().err
    assert err.count("added vibe-bridge (") == 1
    assert "added vibe-bridge (atlassian) to this run: --no-bridge turns it off" in err


def test_the_config_holds_only_vibe_bridge_and_nothing_secret(world: World) -> None:
    world.claude_account("extra", session=5, weekly=5)
    log_in(world, ATLASSIAN)

    world.main("run", "extra", "--", "claude", "-p", "hello")

    path = world.root / "bridge" / "mcp-config.json"
    document = json.loads(path.read_text())
    assert document == {
        "mcpServers": {
            "vibe-bridge": {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "vibemaxxing", "bridge", "serve"],
            }
        }
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    first = path.stat().st_mtime_ns
    world.main("run", "extra", "--", "claude", "-p", "again")
    assert path.stat().st_mtime_ns == first  # unchanged content is not rewritten


def test_which_upstreams_are_named_follows_what_the_account_lacks_and_the_bridge_has(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.claude_account("extra", session=5, weekly=5)
    log_in(world, SLACK, ATLASSIAN)
    (world.root / "settings.json").write_text(
        json.dumps({"schema": 1, "routing": {"connectors": {"extra": ["slack"]}}})
    )

    world.main("run", "extra", "--", "claude", "-p", "x")

    assert "vibe-bridge (atlassian)" in capsys.readouterr().err  # slack it has itself


def test_an_account_that_has_every_connector_the_bridge_offers_gets_nothing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.claude_account("main", session=1, weekly=1, active=True)  # main: slack, atlassian, m365
    log_in(world, SLACK, ATLASSIAN)

    world.main("run", "main", "--", "claude", "-p", "x")

    (call,) = world.claude_calls()
    assert config_flag(call["argv"]) is None  # type: ignore[arg-type]
    assert "vibe-bridge" not in capsys.readouterr().err


def test_without_a_bridge_login_nothing_is_added(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.claude_account("extra", session=5, weekly=5)

    world.main("run", "extra", "--", "claude", "-p", "x")

    (call,) = world.claude_calls()
    assert config_flag(call["argv"]) is None  # type: ignore[arg-type]
    assert not (world.root / "bridge" / "mcp-config.json").exists()
    assert "vibe-bridge" not in capsys.readouterr().err


def test_no_bridge_turns_it_off(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.claude_account("extra", session=5, weekly=5)
    log_in(world, ATLASSIAN)

    assert world.main("run", "extra", "--no-bridge", "--", "claude", "-p", "x") == 0

    (call,) = world.claude_calls()
    assert config_flag(call["argv"]) is None  # type: ignore[arg-type]
    assert "vibe-bridge" not in capsys.readouterr().err
    assert not (world.root / "bridge" / "mcp-config.json").exists()


def test_run_auto_that_picks_extra_adds_it_and_one_that_picks_main_does_not(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=10)
    log_in(world, ATLASSIAN)

    world.main("run", "auto", "--no-needs", "--", "claude", "-p", "x")  # extra first
    assert "auto picked extra" in capsys.readouterr().err
    assert config_flag(world.claude_calls()[-1]["argv"]) is not None  # type: ignore[arg-type]

    world.main("run", "auto", "--needs", "slack", "--", "claude", "-p", "y")  # main has them
    err = capsys.readouterr().err
    assert "auto picked main" in err and "vibe-bridge" not in err
    assert config_flag(world.claude_calls()[-1]["argv"]) is None  # type: ignore[arg-type]


def test_a_cursor_run_never_gets_the_bridge_explicit_or_through_auto(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    log_in(world, SLACK, ATLASSIAN)

    world.main("run", "cursor", "--", "claude", "-p", "x", "--model", "haiku")
    auto_world(world, main_weekly=90)
    world.main("run", "auto", "--needs", "slack", "--", "claude", "-p", "y", "--model", "haiku")

    err = capsys.readouterr().err
    assert "vibe-bridge" not in err
    assert len(world.fake.calls()) == 2
    for argv in world.fake.calls():
        assert not any("mcp-config" in a for a in argv)
    assert not (world.root / "bridge" / "mcp-config.json").exists()


def test_claudes_own_commands_and_other_programs_are_left_alone(world: World) -> None:
    world.claude_account("extra", session=5, weekly=5)
    log_in(world, ATLASSIAN)

    world.main("run", "extra", "--", "claude", "--version")
    world.main("run", "extra", "--", "claude", "mcp", "list")
    world.main("run", "extra", "--", sys.executable, "-c", "print(1)")

    for call in world.claude_calls():
        assert config_flag(call["argv"]) is None  # type: ignore[arg-type]
    assert not (world.root / "bridge" / "mcp-config.json").exists()


def test_the_users_own_mcp_config_and_a_prompt_that_follows_survive(world: World) -> None:
    world.claude_account("extra", session=5, weekly=5)
    log_in(world, ATLASSIAN)

    world.main(
        "run", "extra", "--", "claude", "--mcp-config", "mine.json", "--strict-mcp-config",
        "-p", "the prompt", "--allowedTools", "mcp__vibe-bridge__atlassian_atlassianUserInfo",
    )  # fmt: skip

    (call,) = world.claude_calls()
    argv = call["argv"]
    assert isinstance(argv, list)
    assert argv[0].startswith("--mcp-config=")
    assert argv[1:] == [
        "--mcp-config", "mine.json", "--strict-mcp-config", "-p", "the prompt",
        "--allowedTools", "mcp__vibe-bridge__atlassian_atlassianUserInfo",
    ]  # fmt: skip


def test_a_wrong_routing_does_not_stop_the_session_from_starting(world: World) -> None:
    world.claude_account("extra", session=5, weekly=5)
    log_in(world, ATLASSIAN)
    (world.root / "settings.json").write_text(json.dumps({"schema": 1, "routing": {"surprise": 1}}))

    assert world.main("run", "extra", "--", "claude", "-p", "x") == 0

    assert config_flag(world.claude_calls()[-1]["argv"]) is not None  # type: ignore[arg-type]


def test_dry_run_shows_the_flag(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.claude_account("extra", session=5, weekly=5)
    log_in(world, ATLASSIAN)

    world.main("run", "--json", "extra", "--dry-run", "--", "claude", "-p", "x")

    payload = json.loads(capsys.readouterr().out)
    assert payload["argv"][1].startswith("--mcp-config=") and world.claude_calls() == []


def test_add_command_says_the_bridge_is_added_automatically(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    CliRig(tmp_path).main("bridge", "add-command")

    out = capsys.readouterr().out
    assert "claude mcp add --scope user vibe-bridge -- vibe bridge serve" in out
    assert "now add the bridge by themselves" in out and "--no-bridge" in out
    assert "only for a session you start without `vibe run`" in out


def test_write_mcp_config_is_idempotent_and_names_the_interpreter(tmp_path: Path) -> None:
    root = tmp_path / ".vibemaxxing"

    first = bridge.write_mcp_config(root, python="/opt/py/bin/python")
    again = bridge.write_mcp_config(root, python="/opt/py/bin/python")
    changed = bridge.write_mcp_config(root, python="/other/python")

    assert first == again == changed == bridge.mcp_config_path(root)
    assert json.loads(first.read_text())["mcpServers"]["vibe-bridge"]["command"] == "/other/python"
    assert NOW  # the shared constant is importable here
