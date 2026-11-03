"""The environment of every child: built on purpose, never inherited whole (review 1, P1/P2)."""

from __future__ import annotations

import json
import sys

import pytest

from tests.test_run_cursor import World, auto_world
from vibemaxxing import childenv
from vibemaxxing.errors import UsageError

SECRETS = {
    "CLAUDE_CODE_OAUTH_TOKEN": "EXTRATOKEN0000000000",
    "ANTHROPIC_API_KEY": "ANTHROPICKEY00000000",
    "ANTHROPIC_AUTH_TOKEN": "ANTHROPICAUTH0000000",
    "CLAUDE_CONFIG_DIR": "/somewhere/.claude-extra",
    "GH_TOKEN": "GHTOKEN0000000000",
    "MY_SERVICE_SECRET": "SERVICESECRET00000",
    "VENDOR_API_KEY": "VENDORKEY000000000",
    "AWS_SESSION_TOKEN": "AWSSESSION00000000",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HARMLESS", "kept")
    return dict(SECRETS)


@pytest.fixture
def world(tmp_home, monkeypatch, env) -> World:  # type: ignore[no-untyped-def]
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    world.cursor_usage()
    return world


def values_in(text: str) -> list[str]:
    return [value for value in SECRETS.values() if value in text]


# --- the module ----------------------------------------------------------------------------


def test_looks_secret_and_the_claude_family() -> None:
    for name in ("GH_TOKEN", "x_api_key", "DB_PASSWORD", "AWS_SECRET_ACCESS_KEY", "FOO_SECRET"):
        assert childenv.looks_secret(name), name
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"):
        assert childenv.looks_secret(name), name
    for name in ("PATH", "HOME", "HARMLESS", "TOKENIZERS_PARALLELISM", "EDITOR"):
        assert not childenv.looks_secret(name), name


def test_strict_drops_every_secret_and_the_family_but_not_what_was_passed() -> None:
    parent = {**SECRETS, "PATH": "/bin", "HARMLESS": "kept"}

    built = childenv.build(parent, strict=True, pass_env=["GH_TOKEN"])

    assert built.env == {"PATH": "/bin", "HARMLESS": "kept", "GH_TOKEN": SECRETS["GH_TOKEN"]}
    assert "GH_TOKEN" not in built.dropped and "VENDOR_API_KEY" in built.dropped
    assert list(built.dropped) == sorted(built.dropped)
    assert not any(value in repr(built.dropped) for value in SECRETS.values())  # names only


def test_not_strict_drops_only_the_claude_family() -> None:
    built = childenv.build({**SECRETS, "PATH": "/bin"}, strict=False)

    assert set(built.env) == {
        "PATH",
        "GH_TOKEN",
        "MY_SERVICE_SECRET",
        "VENDOR_API_KEY",
        "AWS_SESSION_TOKEN",
    }
    assert set(built.dropped) == {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
    }


def test_what_the_account_needs_is_set_last_and_beats_the_inherited_value() -> None:
    built = childenv.build(
        {"CLAUDE_CODE_OAUTH_TOKEN": "inherited", "PATH": "/bin"},
        strict=False,
        set_vars={"CLAUDE_CODE_OAUTH_TOKEN": "the-accounts-own"},
    )

    assert built.env["CLAUDE_CODE_OAUTH_TOKEN"] == "the-accounts-own"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in built.dropped


def test_a_claude_credential_can_never_be_passed_to_a_strict_child() -> None:
    with pytest.raises(UsageError, match="never passed"):
        childenv.build({"ANTHROPIC_API_KEY": "x"}, strict=True, pass_env=["ANTHROPIC_API_KEY"])


def test_the_parent_carrying_a_claude_account() -> None:
    assert childenv.parent_carries_claude_account({"CLAUDE_CODE_OAUTH_TOKEN": "t"})
    assert childenv.parent_carries_claude_account({"CLAUDE_CONFIG_DIR": "/d"})
    assert not childenv.parent_carries_claude_account({"ANTHROPIC_API_KEY": "k", "PATH": "/bin"})


# --- cursor-agent ----------------------------------------------------------------------------


def test_cursor_agent_gets_no_secret_and_no_claude_credential(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert world.main("run", "cursor", "--", "claude", "-p", "x", "--model", "haiku") == 0

    (seen,) = world.fake.envs()
    assert not [name for name in seen if childenv.looks_secret(name)], "a secret-like name leaked"
    assert values_in(json.dumps(seen)) == []
    assert seen["HARMLESS"] == "kept" and "PATH" in seen
    err = capsys.readouterr().err
    assert "not passed to the child:" in err and "CLAUDE_CODE_OAUTH_TOKEN" in err
    assert values_in(err) == []  # names only, never values


def test_pass_env_hands_one_variable_through_on_purpose(world: World) -> None:
    world.main(
        "run", "cursor", "--pass-env", "GH_TOKEN", "--", "claude", "-p", "x", "--model", "haiku"
    )

    (seen,) = world.fake.envs()
    assert seen["GH_TOKEN"] == SECRETS["GH_TOKEN"]
    assert "VENDOR_API_KEY" not in seen and "CLAUDE_CODE_OAUTH_TOKEN" not in seen


def test_a_claude_credential_cannot_be_passed_to_cursor(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    code = world.main(
        "run", "cursor", "--pass-env", "CLAUDE_CODE_OAUTH_TOKEN", "--", "claude", "-p", "x",
        "--model", "haiku",
    )  # fmt: skip

    assert code == 2 and "never passed to this child" in capsys.readouterr().err
    assert world.fake.calls() == []


def test_a_command_that_is_not_claude_also_gets_the_scrubbed_environment(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    probe = (
        "import os; print(sorted(k for k in os.environ "
        "if k.endswith(('_TOKEN', '_KEY', '_SECRET', '_DIR'))))"
    )

    assert world.main("run", "cursor", "--", sys.executable, "-c", probe) == 0

    assert capfd.readouterr().out.strip() == "[]"


def test_auto_that_picks_cursor_scrubs_too(world: World) -> None:
    auto_world(world, main_weekly=90)

    world.main("run", "auto", "--needs", "slack", "--", "claude", "-p", "hi", "--model", "haiku")

    (seen,) = world.fake.envs()
    assert not [name for name in seen if childenv.looks_secret(name)]


def test_the_dry_run_names_what_it_would_not_pass(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.main(
        "run", "--json", "cursor", "--dry-run", "--", "claude", "-p", "x", "--model", "haiku"
    )

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "ANTHROPIC_API_KEY" in payload["env_dropped"]
    assert values_in(out) == []


# --- Claude Code: the account is set on purpose --------------------------------------------------


def test_a_stale_extra_token_never_decides_the_account(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review finding 2: inside an extra session, `run auto` that picks main must run as main."""
    auto_world(world, main_weekly=10)  # main is the live login

    assert world.main("run", "auto", "--needs", "slack", "--", "claude", "-p", "hi") == 0

    (call,) = world.claude_calls(full=True)
    seen = call["env"]
    assert isinstance(seen, dict)
    assert seen["CLAUDE_CODE_OAUTH_TOKEN"] == "main-live-token"  # main's own, set explicitly
    assert "EXTRATOKEN" not in json.dumps(seen)
    assert "CLAUDE_CONFIG_DIR" not in seen and "ANTHROPIC_API_KEY" not in seen
    assert seen["GH_TOKEN"] == SECRETS["GH_TOKEN"]  # Claude Code on main keeps the person's own
    err = capsys.readouterr().err
    assert "carries a Claude token or config dir" in err and "never on the inherited one" in err


def test_the_live_login_runs_without_an_override_only_when_nothing_is_inherited(
    tmp_home,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    auto_world(world, main_weekly=10)

    world.main("run", "auto", "--needs", "slack", "--", "claude", "-p", "hi")

    (call,) = world.claude_calls(full=True)
    assert call["has_token"] is False and call["has_config_dir"] is False


def test_a_non_live_account_gets_its_own_token_over_the_inherited_one(world: World) -> None:
    world.claude_account("main", session=5, weekly=5)
    world.claude_account("extra", session=5, weekly=5, active=True)  # extra is the live login

    world.main("run", "auto", "--needs", "slack", "--", "claude", "-p", "hi")

    (call,) = world.claude_calls(full=True)
    seen = call["env"]
    assert isinstance(seen, dict)
    assert seen["CLAUDE_CODE_OAUTH_TOKEN"] == "main-file-token"
    assert "CLAUDE_CONFIG_DIR" not in seen


def test_an_explicit_claude_account_run_is_scrubbed_of_the_inherited_family_too(
    world: World,
) -> None:
    world.claude_account("extra", session=5, weekly=5)

    world.main("run", "extra", "--", "claude", "-p", "hi")

    (call,) = world.claude_calls(full=True)
    seen = call["env"]
    assert isinstance(seen, dict)
    assert seen["CLAUDE_CODE_OAUTH_TOKEN"] == "extra-file-token"
    assert "ANTHROPIC_API_KEY" not in seen and "CLAUDE_CONFIG_DIR" not in seen


def test_claude_own_commands_keep_the_environment_untouched(world: World) -> None:
    world.main("run", "auto", "--", "claude", "--version")

    (call,) = world.claude_calls(full=True)
    seen = call["env"]
    assert isinstance(seen, dict)
    assert seen["CLAUDE_CODE_OAUTH_TOKEN"] == SECRETS["CLAUDE_CODE_OAUTH_TOKEN"]  # the person's own
