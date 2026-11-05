"""One shared login (`routing.shared_login`), the short start lines, and the session's status line.

With a shared login every Claude run vibe routes goes on Claude Code's own login, which the
background tick moves between the accounts; the session follows it, and its status line says
which account it is on. Fakes only: a fake claude records argv and environment; no Keychain,
no request.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_bridge import ATLASSIAN
from tests.test_bridge_inject import config_flag, log_in
from tests.test_run_cursor import NOW_S, World, auto_world
from vibemaxxing import route, statusline
from vibemaxxing.errors import UsageError


@pytest.fixture
def world(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    world.cursor_usage()
    return world


def shared(world: World, **routing: object) -> None:
    (world.root / "settings.json").write_text(
        json.dumps({"schema": 1, "routing": {"shared_login": True, **routing}})
    )


def only_call(world: World) -> dict[str, object]:
    (call,) = world.claude_calls(full=True)
    return call


# --- the setting ------------------------------------------------------------------------------


def test_shared_login_is_off_unless_set_and_must_be_a_boolean(tmp_path: Path) -> None:
    assert route.parse_routing({}, tmp_path).shared_login is False
    assert route.parse_routing({"shared_login": True}, tmp_path).shared_login is True
    with pytest.raises(UsageError, match="shared_login must be true or false"):
        route.parse_routing({"shared_login": "yes"}, tmp_path)


# --- vibe run auto on the shared login --------------------------------------------------------


def test_a_connector_run_goes_on_the_shared_login_and_says_one_line(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=10)  # Tuesday: budget 40%
    shared(world)

    assert world.main("run", "auto", "--", "claude", "-p", "hi") == 0

    call = only_call(world)
    assert call["has_token"] is False and call["has_config_dir"] is False  # Claude Code's own login
    env = call["env"]
    assert isinstance(env, dict) and env["VIBE_SESSION"] == "live"
    assert capsys.readouterr().err == "vibemaxxing: auto picked main: 10% < 40%\n"


def test_a_solo_run_also_follows_the_shared_login_not_the_no_needs_order(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=10)  # extra has room, but main is the shared login now
    shared(world)

    assert world.main("run", "auto", "--no-needs", "--", "claude", "-p", "hi") == 0

    call = only_call(world)
    assert call["has_token"] is False and call["has_config_dir"] is False
    assert capsys.readouterr().err == "vibemaxxing: auto picked main: 10% < 40%\n"


def test_on_extra_the_line_says_why_main_is_not_the_shared_login(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.claude_account("main", session=5, weekly=60)
    world.claude_account("extra", session=5, weekly=5, active=True)  # the tick moved it
    shared(world)

    assert world.main("run", "auto", "--no-needs", "--", "claude", "-p", "hi") == 0

    assert only_call(world)["has_token"] is False
    assert capsys.readouterr().err == "vibemaxxing: auto picked extra: main 60% > 40%\n"


def test_the_bridge_comes_along_when_any_account_the_session_may_move_to_lacks_it(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=10)
    log_in(world, ATLASSIAN)
    shared(world)

    assert world.main("run", "auto", "--", "claude", "-p", "hi") == 0

    argv = only_call(world)["argv"]
    assert isinstance(argv, list) and config_flag(argv) is not None  # main has it; extra does not
    assert capsys.readouterr().err == (
        "vibemaxxing: auto picked main: 10% < 40%\n"
        "vibemaxxing: added vibe-bridge (atlassian) to this run: --no-bridge turns it off\n"
    )


def test_a_connector_run_on_extra_warns_once_about_what_it_will_not_have(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.claude_account("main", session=5, weekly=60)
    world.claude_account("extra", session=5, weekly=5, active=True)
    log_in(world, ATLASSIAN)
    shared(world, order={"needs": [{"account": "extra", "degraded": True}]})

    assert world.main("run", "auto", "--needs", "slack,atlassian", "--", "claude", "-p", "x") == 0

    err = capsys.readouterr().err
    assert "warning: no slack on extra" in err  # atlassian comes through the bridge


def test_a_live_login_in_its_own_config_dir_is_refused_with_release(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=10)
    shared(world)
    path = world.root / "accounts" / "main.json"
    raw = json.loads(path.read_text())
    raw["config_dir"] = str(world.home / ".claude-main")
    path.write_text(json.dumps(raw))

    assert world.main("run", "auto", "--", "claude", "-p", "hi") != 0

    assert world.claude_calls() == []
    assert "vibe release main" in capsys.readouterr().err


def test_without_the_setting_auto_keeps_routing_per_session(world: World) -> None:
    auto_world(world, main_weekly=10)

    assert world.main("run", "auto", "--no-needs", "--", "claude", "-p", "hi") == 0

    assert only_call(world)["has_token"] is True  # extra, on its own token


# --- a named account ---------------------------------------------------------------------------


def test_a_named_account_that_is_the_shared_login_runs_as_it_and_says_nothing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=10)
    shared(world)

    assert world.main("run", "main", "--", "claude", "-p", "hi") == 0

    call = only_call(world)
    assert call["has_token"] is False
    env = call["env"]
    assert isinstance(env, dict) and env["VIBE_SESSION"] == "live"
    assert capsys.readouterr().err == ""


def test_a_named_account_that_is_not_the_shared_login_is_fixed_to_it(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=10)
    shared(world)

    assert world.main("run", "extra", "--", "claude", "-p", "hi") == 0

    call = only_call(world)
    assert call["has_token"] is True
    env = call["env"]
    assert isinstance(env, dict) and env["VIBE_SESSION"] == "extra"
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
                "alias": "extra",
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
        ({"VIBE_SESSION": "extra"}, ("extra", True)),
        ({"VIBE_SESSION": "cursor"}, ("cursor", True)),
        ({"CLAUDE_CODE_OAUTH_TOKEN": "x"}, ("?", True)),
        ({"CLAUDE_CONFIG_DIR": "{home}/.claude"}, (None, False)),
        ({"CLAUDE_CONFIG_DIR": "{home}/.claude-extra"}, ("extra", True)),
        ({"CLAUDE_CONFIG_DIR": "{home}/.claude-other"}, ("?", True)),
    ],
)
def test_the_session_account_comes_from_the_sessions_own_environment(
    tmp_path: Path, environ: dict[str, str], expected: tuple[str | None, bool]
) -> None:
    root = tmp_path / ".vibemaxxing"
    (root / "accounts").mkdir(parents=True)
    (root / "accounts" / "extra.json").write_text(
        json.dumps({"alias": "extra", "config_dir": str(tmp_path / ".claude-extra")})
    )
    given = {key: value.format(home=tmp_path) for key, value in environ.items()}

    assert statusline.session_account(given, root=root, home=tmp_path) == expected


def test_a_session_on_the_shared_login_shows_the_account_the_login_is_now() -> None:
    line = statusline.render(status(live="extra"), now_s=NOW_S, session=(None, False))
    assert line.startswith("▶ extra · ")


def test_a_fixed_session_shows_its_own_account_whatever_is_live() -> None:
    line = statusline.render(status(live="main"), now_s=NOW_S, session=("extra", True))
    assert line.startswith("▶ extra (fixed) · main: ")


def test_a_cursor_session_shows_cursor_without_fixed() -> None:
    line = statusline.render(status(), now_s=NOW_S, session=("cursor", True))
    assert line.startswith("▶ cursor · main: ")


def test_the_session_flag_is_read_in_both_spellings() -> None:
    assert statusline.parse_session_flag(["--session", "cursor"]) == "cursor"
    assert statusline.parse_session_flag(["--session=cursor"]) == "cursor"
    assert statusline.parse_session_flag([]) is None


def test_the_cli_statusline_takes_the_session_flag(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    statusline.write(world.root, status())

    assert world.main("statusline", "--session", "cursor") == 0

    assert capsys.readouterr().out.startswith("▶ cursor · ")


# --- `vibe run <account>` with no command --------------------------------------------------------


def test_a_run_without_a_command_starts_a_session(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert world.main("run", "cursor") == 0
    assert world.fake.calls()[-1] == []  # Cursor's own TUI, on the model Cursor picks

    auto_world(world, main_weekly=10)
    shared(world)
    assert world.main("run", "auto") == 0
    assert only_call(world)["argv"] == []  # plain `claude` on the shared login


def test_at_exactly_the_budget_main_is_out_for_routing_as_for_the_tick(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=40)  # Tuesday: budget 40%

    args = ("--", "claude", "-p", "hi", "--model", "sonnet")
    assert world.main("run", "auto", "--needs", "slack", *args) == 0

    assert "auto picked cursor: main 40% = 40%" in capsys.readouterr().err
