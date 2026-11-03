"""An account whose login Claude Code keeps in its own config dir is never refreshed by vibe.

The incident (3 Nov): a long interactive session on extra stopped with "OAuth token revoked".
It had been started by `vibe run extra` with CLAUDE_CODE_OAUTH_TOKEN, an access token that cannot
renew itself, while vibe's own 5-minute tick refreshed the same lineage and rotated its refresh
token. A session must never lose its login because vibe started or polled something.

So an account imported with `vibe add --from-config-dir DIR` records DIR; `vibe run` starts Claude
Code with CLAUDE_CONFIG_DIR=DIR and no token, and nothing in vibe POSTs a refresh for it. Fakes
only: no socket, no Keychain, no real config dir.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.fakes import FakeKeychain, fixture_usage, make_credential, seed_account
from tests.test_bridge import ATLASSIAN
from tests.test_bridge_inject import log_in
from tests.test_run_cursor import NOW_S, World, auto_world
from vibemaxxing import envelope, oauth, store
from vibemaxxing.credentials import Identity, credential_to_blob
from vibemaxxing.errors import UsageError

NOW_MS = int(NOW_S * 1000)
FUTURE_MS = 4_102_444_800_000
EXTRA = Identity(None, "uextra", None, None, None, None, None)


def token_requests(world: World) -> list[str]:
    return [r.url for r in world.client.requests if r.url == oauth.TOKEN_URL]


def own_dir(
    world: World,
    *,
    login: bool = True,
    uuid: str = "uextra",
    dir_expires_ms: int = FUTURE_MS,
    cache: bool = True,
) -> Path:
    """extra, living in ~/.claude-extra: the stored copy expired long ago, the dir's login is Claude
    Code's own (what it last refreshed)."""
    directory = world.home / ".claude-extra"
    directory.mkdir(exist_ok=True)
    (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": uuid}}))
    if cache:
        world.claude_account("extra", session=5, weekly=5)  # a fresh usage cache, no active mark
        stored = store.read_account(world.root, "extra")
        store.write_account(
            world.root,
            replace(
                stored,
                credential=make_credential(
                    access="extra-stored-token", refresh="extra-stored-rt", expires_at_ms=1_000
                ),
                config_dir=str(directory),
            ),
        )
    else:
        seed_account(
            world.root,
            "extra",
            identity=EXTRA,
            credential=make_credential(
                access="extra-stored-token", refresh="extra-stored-rt", expires_at_ms=1_000
            ),
            config_dir=str(directory),
        )
    if login:
        world.dirs[str(directory)] = FakeKeychain(
            blob=credential_to_blob(
                make_credential(
                    access="extra-dir-token", refresh="extra-dir-rt", expires_at_ms=dir_expires_ms
                )
            )
        )
    return directory


@pytest.fixture
def world(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    world.cursor_usage()
    return world


# --- the root: nothing refreshes it ---------------------------------------------------------------


def test_refresh_refuses_an_owned_account_and_sends_nothing(world: World) -> None:
    own_dir(world)
    account = store.read_account(world.root, "extra")

    outcome = oauth.refresh(world.root, "extra", account.credential, world.client, now_ms=NOW_MS)

    assert outcome.credential is None and outcome.error == "owned"
    assert world.client.requests == []
    assert store.read_stash(world.root, "extra") is None
    assert store.read_account(world.root, "extra") == account  # not one byte of it changed


def test_the_same_account_without_a_config_dir_is_refreshed_so_the_guard_is_what_stops_it(
    world: World,
) -> None:
    own_dir(world)
    owned = store.read_account(world.root, "extra")
    store.write_account(world.root, replace(owned, config_dir=None))
    world.client.queue_json(
        200, {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 28800}
    )

    outcome = oauth.refresh(world.root, "extra", owned.credential, world.client, now_ms=NOW_MS)

    assert outcome.error is None and outcome.credential is not None
    assert len(token_requests(world)) == 1


def test_an_unreadable_account_file_is_not_refreshed_either(world: World) -> None:
    own_dir(world)
    credential = store.read_account(world.root, "extra").credential
    store.account_path(world.root, "extra").write_text("{ not json")

    outcome = oauth.refresh(world.root, "extra", credential, world.client, now_ms=NOW_MS)

    assert outcome.credential is None and outcome.error == "transient"
    assert world.client.requests == []


def test_the_incident_nothing_vibe_does_rotates_a_lineage_claude_code_holds(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every path that used to refresh an expired non-active account, one after another."""
    auto_world(world, main_weekly=10)  # main is the live login
    own_dir(world)
    before = store.account_path(world.root, "extra").read_bytes()
    for _ in range(2):  # the late collect below finds both usage caches stale: one fetch each
        world.client.queue_json(200, fixture_usage())

    assert world.main("list") == 0
    assert world.main("run", "extra", "--", "claude", "-p", "hi") == 0
    assert world.main("run", "auto", "--no-needs", "--", "claude", "-p", "hi") == 0
    assert world.main("run", "auto", "--claude-only", "--", "claude", "-p", "hi") == 0
    views = envelope.collect(
        world.root,
        client=world.client,
        port=world.port,
        now_s=NOW_S + 3600,  # an hour on, the numbers are stale again but still never a refresh
        port_for=world.port_for,
    )

    assert token_requests(world) == []
    assert store.account_path(world.root, "extra").read_bytes() == before
    assert store.read_stash(world.root, "extra") is None
    assert {v.alias for v in views} == {"main", "extra"}
    # extra's usage was asked for with the token in its dir: the stored copy expired long ago
    bearers = {r.headers["Authorization"] for r in world.client.requests}
    assert "Bearer extra-dir-token" in bearers and "Bearer extra-stored-token" not in bearers


# --- what `vibe list` and the tick show -----------------------------------------------------------


def test_usage_is_fetched_with_the_token_in_the_dir_not_the_stored_one(world: World) -> None:
    own_dir(world, cache=False)
    world.client.queue_json(200, fixture_usage())

    (view,) = [
        v
        for v in envelope.collect(
            world.root,
            client=world.client,
            port=world.port,
            now_s=NOW_S,
            port_for=world.port_for,
        )
        if v.alias == "extra"
    ]

    (request,) = world.client.requests
    assert request.headers["Authorization"] == "Bearer extra-dir-token"
    assert view.summary is not None and view.message is None


def test_with_every_token_expired_the_view_says_claude_code_refreshes_it_and_asks_nobody(
    world: World,
) -> None:
    own_dir(world, cache=False, dir_expires_ms=1_000)

    (view,) = envelope.collect(
        world.root,
        client=world.client,
        port=world.port,
        now_s=NOW_S,
        port_for=world.port_for,
    )

    assert world.client.requests == []
    assert view.message is not None and "Claude Code refreshes it in" in view.message
    assert str(world.home / ".claude-extra") in view.message


# --- a run ----------------------------------------------------------------------------------------


def test_a_run_uses_the_config_dir_and_no_token_and_scrubs_what_was_inherited(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "INHERITED-TOKEN-0000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "INHERITED-KEY-000000")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
    monkeypatch.setenv("GH_TOKEN", "the-persons-own")
    directory = own_dir(world)

    assert world.main("run", "extra", "--", "claude", "-p", "hi") == 0

    (call,) = world.claude_calls(full=True)
    seen = call["env"]
    assert isinstance(seen, dict)
    assert seen["CLAUDE_CONFIG_DIR"] == str(directory)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen and "ANTHROPIC_API_KEY" not in seen
    assert seen["GH_TOKEN"] == "the-persons-own"  # Claude Code keeps what it always got
    blob = json.dumps(seen)
    assert not any(t in blob for t in ("INHERITED", "extra-stored-token", "extra-dir-token"))
    assert token_requests(world) == [] and world.client.requests == []
    err = capsys.readouterr().err
    assert f"extra runs with CLAUDE_CONFIG_DIR={directory}" in err and "no token override" in err


def test_an_interactive_run_is_not_given_a_token_that_would_end_in_eight_hours(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    own_dir(world)

    assert world.main("run", "extra", "--", "claude") == 0

    (call,) = world.claude_calls(full=True)
    assert call["has_token"] is False and call["has_config_dir"] is True
    assert "cannot renew it" not in capsys.readouterr().err


def test_the_bridge_is_still_handed_to_a_run_in_a_config_dir(world: World) -> None:
    own_dir(world)
    log_in(world, ATLASSIAN)

    assert world.main("run", "extra", "--", "claude", "-p", "hi") == 0

    (call,) = world.claude_calls(full=True)
    argv = call["argv"]
    assert isinstance(argv, list)
    assert argv[0] == f"--mcp-config={world.root / 'bridge' / 'mcp-config.json'}"
    env = call["env"]
    assert isinstance(env, dict) and "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_a_dir_with_no_login_stops_the_run_and_says_how_to_log_in(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = own_dir(world, login=False)

    assert world.main("run", "extra", "--", "claude", "-p", "hi") != 0

    assert world.claude_calls() == []  # nothing started
    err = capsys.readouterr().err
    assert f"Claude Code has no login for {directory}" in err
    assert f"CLAUDE_CONFIG_DIR={directory} claude" in err


def test_a_dir_logged_in_as_somebody_else_stops_the_run(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    own_dir(world, uuid="usomeone-else")

    assert world.main("run", "extra", "--", "claude", "-p", "hi") != 0

    assert world.claude_calls() == []
    assert 'not as "extra"' in capsys.readouterr().err


def test_auto_that_picks_an_owned_account_uses_its_dir_never_the_live_login(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=10)
    directory = own_dir(world)

    assert world.main("run", "auto", "--no-needs", "--", "claude", "-p", "hi") == 0

    (call,) = world.claude_calls(full=True)
    env = call["env"]
    assert isinstance(env, dict)
    assert env["CLAUDE_CONFIG_DIR"] == str(directory) and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "auto picked extra" in capsys.readouterr().err


def test_a_dir_without_the_claude_md_of_the_person_says_so(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = own_dir(world)
    (world.home / ".claude").mkdir(exist_ok=True)
    (world.home / ".claude" / "CLAUDE.md").write_text("rules")
    (world.home / ".claude" / "settings.json").write_text("{}")
    (directory / "settings.json").symlink_to(world.home / ".claude" / "settings.json")

    assert world.main("run", "extra", "--", "claude", "-p", "hi") == 0

    err = capsys.readouterr().err
    assert f"{directory} has no CLAUDE.md of its own" in err
    assert "settings.json of its own" not in err and "settings.json," not in err
    assert f"ln -s ~/.claude/CLAUDE.md {directory}/" in err


# --- never copied into the default login ---------------------------------------------------------


def test_a_switch_to_an_owned_account_is_refused_and_the_port_is_untouched(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=10)
    own_dir(world)
    log = list(world.port.log)

    assert world.main("switch", "extra") != 0

    assert world.port.log == log
    assert store.read_active(world.root) == "main"
    assert "copying it into the default login would log one of the two out" in (
        capsys.readouterr().err
    )


def test_ensure_usable_refuses_an_owned_target_so_the_tick_stays_put(world: World) -> None:
    auto_world(world, main_weekly=10)
    own_dir(world)

    with pytest.raises(UsageError, match="login in"):
        oauth.ensure_usable(world.root, "extra", world.client, now_ms=NOW_MS)

    assert world.client.requests == []


# --- an account vibe owns -------------------------------------------------------------------------


def test_the_file_of_an_account_vibe_owns_is_what_it_always_was(world: World) -> None:
    seed_account(world.root, "plain", identity=EXTRA)
    raw = json.loads(store.account_path(world.root, "plain").read_text())

    assert "config_dir" not in raw
    assert store.read_account(world.root, "plain").config_dir is None


def test_the_dir_survives_a_round_trip(world: World) -> None:
    own_dir(world)

    raw = json.loads(store.account_path(world.root, "extra").read_text())

    assert raw["config_dir"] == str(world.home / ".claude-extra")
    assert store.read_account(world.root, "extra").config_dir == raw["config_dir"]


def test_an_interactive_run_on_a_token_override_says_when_the_token_ends(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.claude_account("extra", session=5, weekly=5)  # vibe's own: no config dir

    assert world.main("run", "extra", "--", "claude") == 0
    interactive = capsys.readouterr().err
    assert world.main("run", "extra", "--", "claude", "-p", "hi") == 0
    scripted = capsys.readouterr().err

    assert "carries a token that ends at" in interactive and "cannot renew it" in interactive
    assert "vibe add extra --from-config-dir DIR" in interactive
    assert "cannot renew it" not in scripted
