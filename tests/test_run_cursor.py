"""`vibe run cursor -- claude ...` and `vibe run auto`, against fake cursor-agent and claude.

Nothing here starts the real agent or the real Claude Code: a script of that name sits first on a
PATH that holds nothing else, records its argv, and exits. No login, no Keychain, no request.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.cursor_fakes import (
    FakeAgent,
    StubToken,
    install_fake_agent,
    install_fake_claude,
    usage_answer,
)
from tests.fakes import FakeHttpClient, FakeKeychain, make_credential, seed_account
from vibemaxxing import cli, cursor, launch, store
from vibemaxxing.credentials import Identity, credential_to_blob
from vibemaxxing.errors import NetworkError

CHAT = "8ecd5bf4-84eb-4551-a928-e0f5e14044d0"
# Tuesday 3 Nov 2026, 12:00 Berlin: main's budget is 40%.
NOW_S = datetime(2026, 11, 3, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
ADDED_AT = 1_757_930_000.0


class World:
    def __init__(self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch, **agent: object) -> None:
        self.home = tmp_home
        self.root = tmp_home / ".vibemaxxing"
        self.fake: FakeAgent = install_fake_agent(tmp_home, monkeypatch, **agent)  # type: ignore[arg-type]
        self.claude_record = install_fake_claude(self.fake)
        self.client = FakeHttpClient()
        self.cursor_client = FakeHttpClient()
        self.port = FakeKeychain()
        self.token = StubToken()
        # Claude Code's login per config dir. A dir nobody set up has none; nothing here ever
        # reaches the real Keychain.
        self.dirs: dict[str, FakeKeychain] = {}
        monkeypatch.chdir(tmp_home)

    def port_for(self, directory: Path) -> FakeKeychain:
        return self.dirs.setdefault(str(directory), FakeKeychain())

    def ctx(self) -> cli.Context:
        return cli.Context(
            root=self.root,
            client=self.client,
            port=self.port,
            now_s=NOW_S,
            clock=lambda: NOW_S,
            prompt=lambda _: "",
            browser=lambda _: True,
            # the default agent: `cursor-agent` resolved on PATH, which is the fake
            cursor_client=self.cursor_client,
            cursor_token=self.token,
            port_for=self.port_for,
        )

    def main(self, *argv: str) -> int:
        return cli.main(list(argv), context=self.ctx())

    def add_cursor(self) -> None:
        cursor.write_marker(self.root, "cursor", added_at=ADDED_AT)

    def claude_account(
        self, alias: str, *, session: int, weekly: int, active: bool = False
    ) -> None:
        seed_account(
            self.root,
            alias,
            identity=Identity(None, f"u{alias}", None, None, None, None, None),
            credential=make_credential(access=f"{alias}-file-token", refresh=f"{alias}-rt"),
            active=active,
        )
        reset = "2099-01-01T00:00:00+00:00"
        store.write_usage_cache(
            self.root,
            alias,
            store.UsageCache(
                ADDED_AT,
                NOW_S - 10,
                {
                    "limits": [
                        {"kind": "session", "percent": session, "resets_at": reset},
                        {"kind": "weekly_all", "percent": weekly, "resets_at": reset},
                    ]
                },
                None,
                0,
                None,
            ),
        )
        if active:
            (self.home / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"accountUuid": f"u{alias}"}})
            )
            self.port.blob = credential_to_blob(
                make_credential(access=f"{alias}-live-token", refresh=f"{alias}-live-rt")
            )

    def cursor_usage(self, **kw: int | float) -> None:
        self.cursor_client.queue_json(200, usage_answer(**kw))
        self.cursor_client.queue_json(200, {"hardLimit": 250})

    def _all_claude_calls(self) -> list[dict[str, object]]:
        if not self.claude_record.exists():
            return []
        return [json.loads(line) for line in self.claude_record.read_text().splitlines()]

    def claude_calls(self, *, full: bool = False) -> list[dict[str, object]]:
        """The runs of the fake claude."""
        calls = self._all_claude_calls()
        if not full:
            for call in calls:
                for key in ("has_config_dir", "cwd", "env"):
                    call.pop(key, None)
        return calls


@pytest.fixture
def world(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    world.cursor_usage()
    return world


# --- vibe run cursor -- claude ... ---------------------------------------------------------


def test_a_headless_claude_call_becomes_the_matching_cursor_agent_call(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    code = world.main(
        "run",
        "cursor",
        "--",
        "claude",
        "-p",
        "fix the bug",
        "--model",
        "sonnet",
        "--output-format",
        "stream-json",
        "--permission-mode",
        "auto",
    )

    assert code == 0
    assert world.fake.calls() == [
        [
            "-p",
            "--output-format",
            "stream-json",
            "--auto-review",
            "--model",
            "claude-sonnet-5-5-medium",
            "fix the bug",
        ]
    ]
    # A run on a named account says only what may go wrong: here, Cursor's untrusted workspace.
    assert capsys.readouterr().err.count("\n") == 1
    assert world.claude_calls() == []  # Claude Code itself never started


def test_interactive_claude_starts_the_cursor_tui_quietly(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert world.main("run", "cursor", "--", "claude", "--model", "opus") == 0

    assert world.fake.calls() == [["--model", "claude-opus-5-5-medium"]]
    assert capsys.readouterr().err == ""


def test_the_exit_code_of_cursor_agent_is_the_exit_code(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_home, monkeypatch, exit_code=7)
    world.add_cursor()
    world.cursor_usage()

    assert world.main("run", "cursor", "--", "claude", "-p", "x", "--model", "haiku") == 7


def test_json_mode_reports_kind_and_exit_code(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.main("run", "--json", "cursor", "--", "claude", "-p", "x", "--model", "haiku")

    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "run" and payload["alias"] == "cursor"
    assert payload["kind"] == "cursor" and payload["exit_code"] == 0


def test_a_command_that_is_not_claude_runs_unchanged(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert world.main("run", "cursor", "--", sys.executable, "-c", "print('plain')") == 0

    assert capfd.readouterr().out.strip() == "plain"
    assert world.fake.calls() == []  # cursor-agent was not involved


def test_the_default_model_comes_from_claude_codes_own_settings(
    world: World, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_home / ".claude"
    settings.mkdir()
    (settings / "settings.json").write_text(json.dumps({"model": "opus", "effortLevel": "low"}))

    world.main("run", "cursor", "--", "claude", "-p", "x")
    assert world.fake.calls()[-1][-3:-1] == ["--model", "claude-opus-5-5-low"]

    project = tmp_home / "project" / ".claude"
    project.mkdir(parents=True)
    (project / "settings.local.json").write_text(json.dumps({"model": "haiku"}))
    monkeypatch.chdir(tmp_home / "project")
    world.main("run", "cursor", "--", "claude", "-p", "x")
    assert world.fake.calls()[-1][-3:-1] == ["--model", "claude-haiku-5-5-low"]  # most local wins

    monkeypatch.setenv("ANTHROPIC_MODEL", "sonnet")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "medium")
    world.main("run", "cursor", "--", "claude", "-p", "x")
    assert world.fake.calls()[-1][-3:-1] == ["--model", "claude-sonnet-5-5-medium"]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["-p", "x", "--model", "sonnet", "--dangerously-skip-permissions"], "--allow-force"),
        (
            ["-p", "x", "--model", "sonnet", "--permission-mode", "bypassPermissions"],
            "--allow-force",
        ),
        (["-p", "x", "--model", "sonnet", "--allowedTools", "Bash"], "--allowedTools"),
        (["-p", "x", "--model", "sonnet", "--mcp-config", "x.json"], "--mcp-config"),
        (
            ["-p", "x", "--model", "sonnet", "--resume", "0a1b2c3d-1111-2222-3333-444455556666"],
            "Claude session",
        ),
        (["-p", "x", "--model", "claude-opus-9-9"], "no Cursor model for claude-opus-9-9"),
        (["-p", "x", "--model", "fable"], "NO ZDR"),
        (["-p", "x"], "cannot tell which Claude model"),
        (["mcp", "list"], "claude mcp"),
    ],
)
def test_a_refusal_names_the_problem_and_starts_nothing(
    world: World, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    assert world.main("run", "cursor", "--", "claude", *argv) == 2

    assert message in capsys.readouterr().err
    assert world.fake.calls() == []


def test_allow_flags_open_exactly_what_they_name(world: World) -> None:
    assert (
        world.main(
            "run",
            "cursor",
            "--allow-force",
            "--",
            "claude",
            "-p",
            "x",
            "--model",
            "haiku",
            "--dangerously-skip-permissions",
        )
        == 0
    )
    assert "--force" in world.fake.calls()[-1]

    assert (
        world.main("run", "cursor", "--allow-no-zdr", "--", "claude", "-p", "x", "--model", "fable")
        == 0
    )
    assert "claude-fable-5-1-medium" in world.fake.calls()[-1]


def test_a_cursor_chat_id_can_be_resumed(world: World) -> None:
    world.fake.make_chat(CHAT)

    assert (
        world.main(
            "run", "cursor", "--", "claude", "-p", "go on", "--resume", CHAT, "--model", "haiku"
        )
        == 0
    )

    assert world.fake.calls()[-1][:3] == ["-p", "--resume", CHAT]


def test_logged_out_cursor_is_refused_with_the_login_command(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_home, monkeypatch, logged_in=False)
    world.add_cursor()

    assert world.main("run", "cursor", "--", "claude", "-p", "x", "--model", "haiku") == 2

    assert "cursor-agent login" in capsys.readouterr().err
    assert world.fake.calls() == []


def test_needs_add_approve_mcps_only_for_headless_runs_and_only_for_connectors_cursor_has(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        world.main(
            "run", "cursor", "--needs", "slack", "--", "claude", "-p", "x", "--model", "haiku"
        )
        == 0
    )
    assert "--approve-mcps" in world.fake.calls()[-1]

    world.main("run", "cursor", "--", "claude", "-p", "x", "--model", "haiku")
    assert "--approve-mcps" not in world.fake.calls()[-1]

    assert world.main("run", "cursor", "--needs", "m365", "--", "claude", "-p", "x") == 2
    assert "cursor has no m365 connector" in capsys.readouterr().err


def test_routing_flags_are_refused_on_a_plain_claude_account(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.claude_account("main", session=1, weekly=1, active=True)

    assert world.main("run", "main", "--needs", "slack", "--", "claude") == 2
    assert world.main("run", "cursor", "--claude-only", "--", "claude") == 2
    assert world.main("run", "cursor", "--brief", "x", "--", "claude") == 2
    assert "for `vibe run auto`" in capsys.readouterr().err
    assert world.claude_calls() == []


def test_dry_run_prints_the_plan_and_runs_nothing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    code = world.main(
        "run", "cursor", "--dry-run", "--", "claude", "-p", "hello", "--model", "haiku"
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "would run on cursor (cursor):" in out and "claude-haiku-5-5-medium" in out
    assert world.fake.calls() == []


# --- vibe run auto -----------------------------------------------------------------------------


def auto_world(world: World, *, main_weekly: int, extra_weekly: int | None = 5) -> None:
    world.claude_account("main", session=5, weekly=main_weekly, active=True)
    if extra_weekly is not None:
        world.claude_account("extra", session=5, weekly=extra_weekly)


def test_auto_with_needs_and_main_within_budget_runs_claude_on_main_as_its_own_login(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=39)  # Tuesday's budget is 40%: below it, as the tick counts

    assert world.main("run", "auto", "--needs", "slack", "--", "claude", "-p", "hi") == 0

    # main is the login Claude Code already has: no token override
    assert world.claude_calls() == [{"argv": ["-p", "hi"], "has_token": False}]
    assert world.fake.calls() == []
    err = capsys.readouterr().err
    assert err == "vibemaxxing: auto picked main: 39% < 40%\n"
    assert world.cursor_client.requests == []  # main answered; Cursor was not even asked...
    # ...although its usage was read for the routing table (queued answers untouched or used)


def test_auto_over_budget_moves_a_needs_run_to_cursor_and_translates(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=41)

    code = world.main(
        "run", "auto", "--needs", "slack,atlassian", "--", "claude", "-p", "hi", "--model", "haiku"
    )

    assert code == 0
    assert world.claude_calls() == []
    assert world.fake.calls() == [
        ["-p", "--mode", "ask", "--model", "claude-haiku-5-5-medium", "--approve-mcps", "hi"]
    ]
    err = capsys.readouterr().err
    assert "vibemaxxing: auto picked cursor: main 41% > 40%\n" in err


def test_auto_without_a_flag_uses_the_default_class_needs_connectors(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=41)

    world.main("run", "auto", "--", "claude", "-p", "hi", "--model", "haiku")

    assert "auto picked cursor" in capsys.readouterr().err  # default_needs = slack, atlassian


def test_a_non_live_account_runs_on_its_token(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.claude_account("main", session=5, weekly=10)
    world.claude_account("extra", session=5, weekly=5, active=True)  # extra is the live login

    assert world.main("run", "auto", "--needs", "slack", "--", "claude", "-p", "hi") == 0

    assert capsys.readouterr().err == "vibemaxxing: auto picked main: 10% < 40%\n"
    assert world.claude_calls() == [{"argv": ["-p", "hi"], "has_token": True}]


def test_auto_no_needs_prefers_extra(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    auto_world(world, main_weekly=10)

    assert world.main("run", "auto", "--no-needs", "--", "claude", "-p", "hi") == 0

    assert capsys.readouterr().err == "vibemaxxing: auto picked extra\n"
    assert world.claude_calls() == [{"argv": ["-p", "hi"], "has_token": True}]


def test_auto_no_needs_extra_spent_main_over_budget_goes_to_cursor(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=60, extra_weekly=100)

    world.main("run", "auto", "--no-needs", "--", "claude", "-p", "hi", "--model", "opus")

    err = capsys.readouterr().err
    assert "auto picked cursor" in err
    assert world.fake.calls() == [
        ["-p", "--mode", "ask", "--model", "claude-opus-5-5-medium", "hi"]
    ]


def test_auto_claude_only_never_goes_to_cursor(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=60)

    assert world.main("run", "auto", "--claude-only", "--", "claude", "-p", "hi") == 0

    err = capsys.readouterr().err
    assert "auto picked extra: main 60% > 40%" in err
    assert "warning: no atlassian, slack on extra" in err
    assert world.fake.calls() == []


def test_auto_m365_goes_to_main_even_over_budget(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=90)

    assert world.main("run", "auto", "--needs", "m365", "--", "claude", "-p", "hi") == 0

    assert "auto picked main: last resort, main 90% > 40%" in capsys.readouterr().err
    assert world.claude_calls() == [{"argv": ["-p", "hi"], "has_token": False}]


def test_auto_treats_unknown_cursor_usage_as_unavailable_and_says_so(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    world.cursor_client.queue_json(200, {"shape": "changed"})
    world.cursor_client.queue_json(200, {})
    auto_world(world, main_weekly=90)

    assert (
        world.main("run", "auto", "--needs", "slack", "--dry-run", "--", "claude", "-p", "hi") == 0
    )

    err = capsys.readouterr().err
    assert "skipped" not in err  # one line: what was picked and why, not every step
    assert "auto picked extra: main 90% > 40%" in err and "auto picked cursor" not in err


def test_a_move_from_a_claude_session_to_cursor_starts_fresh_with_the_brief(
    world: World, capsys: pytest.CaptureFixture[str], tmp_home: Path
) -> None:
    auto_world(world, main_weekly=90)
    notes = tmp_home / "handover.md"
    notes.write_text("where I stopped")

    code = world.main(
        "run",
        "auto",
        "--needs",
        "slack",
        "--brief",
        "Fix the login bug",
        "--handover",
        str(notes),
        "--",
        "claude",
        "-p",
        "--resume",
        "0a1b2c3d-1111-2222-3333-444455556666",
        "--model",
        "haiku",
    )

    assert code == 0
    call = world.fake.calls()[-1]
    assert "--resume" not in call
    assert call[-1].startswith("Fix the login bug")
    assert f"handover notes in {notes}" in call[-1]
    err = capsys.readouterr().err
    assert (
        "a Claude session cannot be resumed on Cursor: starting a fresh run with the original brief"
        in err
    )
    assert "and the handover file" in err


def test_a_move_without_a_brief_says_it_starts_empty(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=90)

    world.main(
        "run", "auto", "--needs", "slack", "--", "claude", "--resume", "abc", "--model", "haiku"
    )

    assert "no --brief or --handover was given, so it starts empty" in capsys.readouterr().err
    assert world.fake.calls()[-1] == ["--model", "claude-haiku-5-5-medium"]


def test_a_cursor_chat_id_given_to_a_claude_run_is_dropped_with_a_note(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=10)
    world.fake.make_chat(CHAT)

    world.main(
        "run", "auto", "--needs", "slack", "--brief", "go", "--", "claude", "-p", "--resume", CHAT
    )

    assert world.claude_calls()[-1]["argv"] == ["-p", "go"]
    assert "a Cursor chat id, which Claude Code cannot resume" in capsys.readouterr().err


def test_auto_runs_only_claude_and_passes_claude_own_commands_through(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=10)

    assert world.main("run", "auto", "--", "ls") == 2
    assert "routes `claude` only" in capsys.readouterr().err

    assert world.main("run", "auto", "--", "claude", "--version") == 0
    assert world.claude_calls() == [
        {"argv": ["--version"], "has_token": False}
    ]  # no account involved
    assert world.cursor_client.requests == []


def test_auto_when_no_account_can_take_it(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_home, monkeypatch, logged_in=False)
    world.add_cursor()

    assert world.main("run", "auto", "--", "claude", "-p", "x") == 2

    assert "no account can take this run" in capsys.readouterr().err


def test_a_wrong_routing_in_settings_refuses_auto_but_not_list(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=10)
    (world.root / "settings.json").write_text(json.dumps({"schema": 1, "routing": {"surprise": 1}}))

    assert world.main("run", "auto", "--", "claude", "-p", "x") == 2
    assert "routing has unknown members: surprise" in capsys.readouterr().err
    world.client.queue_json(200, {"limits": []})
    world.client.queue_json(200, {"limits": []})
    assert world.main("list") == 0


def test_shell_init_prints_a_snippet_and_writes_nothing(
    world: World, tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    zshrc = tmp_home / ".zshrc"
    zshrc.write_text("# mine\n")

    assert world.main("shell-init") == 0

    out = capsys.readouterr().out
    assert 'cw() { vibe run auto -- claude "$@"; }' in out
    assert 'cs() { vibe run auto --no-needs -- claude "$@"; }' in out
    assert "claude()" not in out  # `claude` itself stays plain Claude Code
    assert zshrc.read_text() == "# mine\n"
    assert not (tmp_home / ".vibemaxxing").joinpath("shell-init").exists()


def test_headless_cursor_runs_say_the_workspace_must_be_trusted_and_the_flag_passes_trust(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.main("run", "cursor", "--", "claude", "-p", "x", "--model", "haiku")
    err = capsys.readouterr().err
    assert "never trusted" in err and "--trust-workspace" in err
    assert "--trust" not in world.fake.calls()[-1]  # never added on its own

    world.main("run", "cursor", "--trust-workspace", "--", "claude", "-p", "x", "--model", "haiku")
    assert "--trust" in world.fake.calls()[-1]
    assert "never trusted" not in capsys.readouterr().err


# --- round 3: roles, what a Cursor run may do, the money ceiling, the interactive line ---------


def test_differently_named_accounts_are_refused_not_sent_to_cursor(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.claude_account("stephan", session=5, weekly=5, active=True)
    world.claude_account("shared", session=5, weekly=5)

    assert (
        world.main("run", "auto", "--no-needs", "--", "claude", "-p", "hi", "--model", "haiku") == 2
    )

    err = capsys.readouterr().err
    assert "naming mismatch" in err and "routing.roles" in err
    assert world.fake.calls() == [] and world.claude_calls() == []

    (world.root / "settings.json").write_text(
        json.dumps({"schema": 1, "routing": {"roles": {"main": "stephan", "extra": "shared"}}})
    )
    assert world.main("run", "auto", "--no-needs", "--", "claude", "-p", "hi") == 0
    assert capsys.readouterr().err == "vibemaxxing: auto picked shared\n"


def test_a_headless_run_is_read_only_by_default(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.main("run", "cursor", "--", "claude", "-p", "x", "--model", "haiku")
    assert world.fake.calls()[-1][:3] == ["-p", "--mode", "ask"]
    assert "never trusted" in capsys.readouterr().err  # and nothing about the mode

    world.main(
        "run", "cursor", "--", "claude", "-p", "x", "--model", "haiku", "--permission-mode", "auto"
    )
    assert "--mode" not in world.fake.calls()[-1] and "--auto-review" in world.fake.calls()[-1]
    assert "auto-review" not in capsys.readouterr().err

    world.main("run", "cursor", "--allow-force", "--", "claude", "-p", "x", "--model", "haiku",
               "--dangerously-skip-permissions")  # fmt: skip
    assert "--force: every command and edit runs without asking" in capsys.readouterr().err
    assert "--mode" not in world.fake.calls()[-1]


def test_on_demand_at_the_cap_refuses_a_named_cursor_run_and_starts_nothing(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    world.cursor_usage(on_demand_used=24_500, on_demand_limit=25_000)  # within $10 of the cap

    assert world.main("run", "cursor", "--", "claude", "-p", "x", "--model", "haiku") == 2

    assert "on-demand limit" in capsys.readouterr().err
    assert world.fake.calls() == []


def test_vibes_ceiling_holds_when_cursor_reports_a_higher_cap(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    world.cursor_usage(on_demand_used=24_500, on_demand_limit=100_000)  # Cursor says $1000

    assert world.main("run", "cursor", "--", "claude", "-p", "x", "--model", "haiku") == 2
    assert world.fake.calls() == []


def test_an_unreadable_usage_warns_but_does_not_stop_a_run_you_named(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    world.cursor_client.queue_json(200, {"shape": "changed"})
    world.cursor_client.queue_json(200, {})

    assert world.main("run", "cursor", "--", "claude", "-p", "x", "--model", "haiku") == 0

    assert "warning: Cursor usage unknown" in capsys.readouterr().err
    assert len(world.fake.calls()) == 1


def test_a_cached_answer_near_the_cap_is_only_trusted_for_thirty_seconds(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    world.cursor_usage(on_demand_used=21_000, on_demand_limit=25_000)  # $40 left: near the cap
    ctx = world.ctx()
    svc = ctx.services()
    launch.read_cursor(svc, "cursor", margin_cents=1000)
    assert len(world.cursor_client.requests) == 2

    launch.read_cursor(replace(svc, now_s=NOW_S + 20), "cursor", margin_cents=1000)
    assert len(world.cursor_client.requests) == 2  # 20 s old: used

    world.cursor_usage(on_demand_used=24_800, on_demand_limit=25_000)
    reading = launch.read_cursor(replace(svc, now_s=NOW_S + 60), "cursor", margin_cents=1000)
    assert len(world.cursor_client.requests) == 4  # 60 s old and close to the cap: fetched again
    assert reading.availability.ok is False


def test_a_stale_answer_is_shown_but_never_routed_by(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    world.cursor_usage()
    svc = world.ctx().services()
    launch.read_cursor(svc, "cursor", margin_cents=1000)

    world.cursor_client.queue(NetworkError("offline", "x"))
    reading = launch.read_cursor(replace(svc, now_s=NOW_S + 300), "cursor", margin_cents=1000)

    assert reading.usage is not None and "showing" in reading.note  # still displayed
    assert reading.availability.ok is None and "too old to use" in reading.availability.why


def test_moved_to_cursors_tui_one_clear_line_is_the_last_thing_printed(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    auto_world(world, main_weekly=48)  # Tuesday's budget is 40%

    assert world.main("run", "auto", "--needs", "slack", "--", "claude", "--model", "opus") == 0

    assert capsys.readouterr().err == "vibemaxxing: auto picked cursor: main 48% > 40%\n"
    assert world.fake.calls() == [["--model", "claude-opus-5-5-medium"]]  # interactive: no -p


def test_the_move_names_the_main_role_alias(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.claude_account("stephan", session=5, weekly=48, active=True)
    (world.root / "settings.json").write_text(
        json.dumps({"schema": 1, "routing": {"roles": {"main": "stephan"}}})
    )

    world.main("run", "auto", "--needs", "slack", "--", "claude", "--model", "opus")

    assert capsys.readouterr().err == "vibemaxxing: auto picked cursor: stephan 48% > 40%\n"


def test_a_named_interactive_cursor_run_prints_nothing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.main("run", "cursor", "--", "claude", "--model", "opus")

    assert capsys.readouterr().err == ""
