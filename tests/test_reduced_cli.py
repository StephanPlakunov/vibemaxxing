"""What vibe is for: `vibe` shows every account's usage, `vibe <account>` starts an agent on it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient, fixture_usage, make_credential, seed_account
from tests.test_cli import context
from vibemaxxing import cli


def _two_accounts(tmp_home: Path) -> FakeHttpClient:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "main", credential=make_credential(), active=True)
    seed_account(root, "side", credential=make_credential(access="side-a", refresh="side-r"))
    client = FakeHttpClient()
    client.queue_json(200, fixture_usage())
    client.queue_json(200, fixture_usage())
    return client


def test_bare_vibe_prints_the_usage_view(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([], context=context(tmp_home, _two_accounts(tmp_home))) == 0
    bare = capsys.readouterr().out

    assert "  main  " in bare and "  side  " in bare and "█" in bare and "pool" in bare
    assert cli.main(["usage"], context=context(tmp_home, _two_accounts(tmp_home))) == 0
    assert capsys.readouterr().out == bare


def test_no_account_is_marked_as_the_active_one(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["list"], context=context(tmp_home, _two_accounts(tmp_home))) == 0
    out = capsys.readouterr().out

    headers = [line for line in out.splitlines() if line.startswith(("* ", "● "))]
    assert headers == []
    assert out.splitlines()[0].startswith("  main  ")


def test_the_json_still_says_which_account_claude_codes_own_login_is(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # control-room's usage watcher reads `active` from `vibe --json list`.
    for argv in (["--json"], ["--json", "list"], ["list", "--json"]):
        assert cli.main(argv, context=context(tmp_home, _two_accounts(tmp_home))) == 0, argv
        accounts = json.loads(capsys.readouterr().out)["accounts"]
        assert {a["alias"]: a["active"] for a in accounts} == {"main": True, "side": False}


def test_the_usage_web_dashboard_is_gone(tmp_home: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["usage", "web"], context=context(tmp_home, FakeHttpClient()))


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (["main"], ["run", "main"]),
        (["main", "--", "claude", "-p", "hi"], ["run", "main", "--", "claude", "-p", "hi"]),
        (["cursor", "--dry-run"], ["run", "cursor", "--dry-run"]),
        (["--json", "main", "--dry-run"], ["--json", "run", "main", "--dry-run"]),
        # A command name always wins; an account named like one starts with `vibe run`.
        (["list"], ["list"]),
        (["run", "main"], ["run", "main"]),
        (["statusline"], ["statusline"]),
        ([], []),
        (["--json"], ["--json"]),
        (["--version"], ["--version"]),
    ],
)
def test_an_account_name_is_the_run_shorthand(given: list[str], expected: list[str]) -> None:
    assert cli.account_shorthand(given) == expected


def test_vibe_account_runs_what_vibe_run_account_runs(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "main", credential=make_credential(), active=True)
    ctx = context(tmp_home, FakeHttpClient())

    outputs = []
    for argv in (
        ["--json", "run", "main", "--dry-run", "--", "claude", "-p", "hi"],
        ["--json", "main", "--dry-run", "--", "claude", "-p", "hi"],
    ):
        assert cli.main(argv, context=ctx) == 0, argv
        outputs.append(json.loads(capsys.readouterr().out))

    assert outputs[0] == outputs[1]
    assert outputs[1]["alias"] == "main" and outputs[1]["dry_run"] is True
    assert outputs[1]["argv"][-2:] == ["-p", "hi"]


def test_an_unknown_name_says_there_is_no_such_account(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_account(tmp_home / ".vibemaxxing", "main", credential=make_credential(), active=True)

    assert cli.main(["nosuch"], context=context(tmp_home, FakeHttpClient())) != 0
    assert 'no account named "nosuch"' in capsys.readouterr().err
