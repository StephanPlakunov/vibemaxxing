"""The account a session is on: what `vibe run` marks, and what its status line shows.

`vibe run <account>` fixes a session to the account the person named; vibe never moves it. Fakes
only: a fake claude records argv and environment; no Keychain, no request.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_run_cursor import NOW_S, World
from vibemaxxing import statusline


@pytest.fixture
def world(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    world.cursor_usage()
    world.claude_account("main", session=5, weekly=10, active=True)
    world.claude_account("work", session=5, weekly=5)
    return world


def only_call(world: World) -> dict[str, object]:
    (call,) = world.claude_calls(full=True)
    return call


# --- a named account ---------------------------------------------------------------------------


def test_a_named_account_is_fixed_even_when_it_is_claude_codes_own_login(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert world.main("run", "main", "--", "claude", "-p", "hi") == 0

    call = only_call(world)
    assert call["has_token"] is True  # its own token: nothing moves it
    env = call["env"]
    assert isinstance(env, dict) and env["VIBE_SESSION"] == "main"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "main-live-token"  # the live login's, not refreshed
    assert capsys.readouterr().err == ""  # headless: no line about when the token ends


def test_a_named_account_that_is_not_live_is_fixed_to_it(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert world.main("run", "work", "--", "claude", "-p", "hi") == 0

    call = only_call(world)
    assert call["has_token"] is True
    env = call["env"]
    assert isinstance(env, dict) and env["VIBE_SESSION"] == "work"
    assert capsys.readouterr().err == ""  # headless: no line about the token's end


def test_a_cursor_run_is_marked_cursor(world: World) -> None:
    world.main("run", "cursor", "--", "claude", "-p", "x", "--model", "haiku")

    assert world.fake.envs()[-1]["VIBE_SESSION"] == "cursor"


# --- the session's own status line -------------------------------------------------------------


def status(live: str = "main") -> dict[str, object]:
    return {
        "schema": 1,
        "live": live,
        "accounts": [
            {
                "alias": "main",
                "fetched_at": NOW_S - 30,
                "rows": [{"kind": "weekly_all", "percent": 75, "resets_at": NOW_S + 86_400}],
            },
            {
                "alias": "work",
                "fetched_at": NOW_S - 30,
                "rows": [{"kind": "session", "percent": 31, "resets_at": NOW_S + 3_600}],
            },
        ],
        "cursor": [{"alias": "cursor", "fetched_at": NOW_S - 30, "available": True}],
    }


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, (None, False)),
        ({"VIBE_SESSION": "live"}, (None, False)),
        ({"VIBE_SESSION": "work"}, ("work", True)),
        ({"VIBE_SESSION": "cursor"}, ("cursor", True)),
        ({"CLAUDE_CODE_OAUTH_TOKEN": "x"}, ("?", True)),
        ({"CLAUDE_CONFIG_DIR": "{home}/.claude"}, (None, False)),
        ({"CLAUDE_CONFIG_DIR": "{home}/.claude-work"}, ("work", True)),
        ({"CLAUDE_CONFIG_DIR": "{home}/.claude-other"}, ("?", True)),
    ],
)
def test_the_session_account_comes_from_the_sessions_own_environment(
    tmp_path: Path, environ: dict[str, str], expected: tuple[str | None, bool]
) -> None:
    root = tmp_path / ".vibemaxxing"
    (root / "accounts").mkdir(parents=True)
    (root / "accounts" / "work.json").write_text(
        json.dumps({"alias": "work", "config_dir": str(tmp_path / ".claude-work")})
    )
    given = {key: value.format(home=tmp_path) for key, value in environ.items()}

    assert statusline.session_account(given, root=root, home=tmp_path) == expected


def test_a_session_on_claude_codes_own_login_shows_the_account_the_login_is_now() -> None:
    line = statusline.render(status(live="work"), now_s=NOW_S, session=(None, False))
    assert line.startswith("▶ work · ")


def test_a_fixed_session_shows_its_own_account_whatever_is_live() -> None:
    line = statusline.render(status(live="main"), now_s=NOW_S, session=("work", True))
    assert line.startswith("▶ work (fixed) · main: ")


def test_a_cursor_session_shows_cursor_fixed() -> None:
    line = statusline.render(status(), now_s=NOW_S, session=("cursor", True))
    assert line.startswith("▶ cursor (fixed) · main: ")


def test_the_session_flag_is_read_in_both_spellings() -> None:
    assert statusline.parse_session_flag(["--session", "cursor"]) == "cursor"
    assert statusline.parse_session_flag(["--session=cursor"]) == "cursor"
    assert statusline.parse_session_flag([]) is None


def test_the_cli_statusline_takes_the_session_flag(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    statusline.write(world.root, status())

    assert world.main("statusline", "--session", "cursor") == 0

    assert capsys.readouterr().out.startswith("▶ cursor (fixed) · ")


# --- `vibe run <account>` with no command --------------------------------------------------------


def test_a_run_without_a_command_starts_a_session(world: World) -> None:
    assert world.main("run", "cursor") == 0
    assert world.fake.calls()[-1] == []  # Cursor's own TUI, on the model Cursor picks

    assert world.main("run", "work") == 0
    assert only_call(world)["argv"] == []  # plain `claude`, fixed to work


def test_vibes_own_flags_after_the_account_are_vibes_not_a_command() -> None:
    from vibemaxxing.cli import split_run_command

    assert split_run_command(["run", "cursor", "--dry-run"]) == (["run", "cursor", "--dry-run"], [])
    assert split_run_command(["run", "work", "--no-bridge", "--json"]) == (
        ["run", "work", "--no-bridge", "--json"],
        [],
    )
    assert split_run_command(["run", "work", "claude", "--dry-run"]) == (
        ["run", "work"],
        ["claude", "--dry-run"],
    )
