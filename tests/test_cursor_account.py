"""The Cursor account kind: add, list, remove, alias, switch, and that auto never goes there."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from tests.cursor_fakes import StubAgent, StubToken, usage_answer
from tests.fakes import FakeHttpClient, FakeKeychain, fixture_usage, make_credential, seed_account
from vibemaxxing import auto, cli, cursor, store
from vibemaxxing.credentials import Identity
from vibemaxxing.errors import NetworkError
from vibemaxxing.pool import Policy, Settings, Step

NOW_S = 1_757_930_000.0


class Rig:
    def __init__(self, tmp_home: Path, **agent: object) -> None:
        self.root = tmp_home / ".vibemaxxing"
        self.agent = StubAgent(**agent)  # type: ignore[arg-type]
        self.client = FakeHttpClient()
        self.cursor_client = FakeHttpClient()
        self.port = FakeKeychain()
        self.token = StubToken()

    def ctx(self) -> cli.Context:
        return cli.Context(
            root=self.root,
            client=self.client,
            port=self.port,
            now_s=NOW_S,
            clock=lambda: NOW_S,
            prompt=lambda _: "",
            browser=lambda _: True,
            agent=self.agent,
            cursor_client=self.cursor_client,
            cursor_token=self.token,
        )

    def main(self, *argv: str) -> int:
        return cli.main(list(argv), context=self.ctx())

    def queue_usage(self, **kw: int | float) -> None:
        self.cursor_client.queue_json(200, usage_answer(**kw))
        self.cursor_client.queue_json(200, {"hardLimit": 250})


@pytest.fixture
def rig(tmp_home: Path) -> Rig:
    return Rig(tmp_home)


# --- vibe add --kind cursor -------------------------------------------------------------


def test_add_cursor_checks_status_and_stores_a_marker_only(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rig.main("add", "cursor", "--kind", "cursor") == 0

    out = capsys.readouterr().out
    assert 'added Cursor account "cursor" (logged in as dev@example.com, team Zvoove)' in out
    marker = rig.root / "cursor" / "cursor.json"
    document = json.loads(marker.read_text())
    assert document == {"schema": 1, "alias": "cursor", "kind": "cursor", "added_at": NOW_S}
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    # vibe handles no Cursor credential: no account file, no Keychain, no token, no request.
    assert store.list_aliases(rig.root) == []
    assert rig.port.log == []
    assert rig.cursor_client.requests == [] and rig.client.requests == []
    assert rig.agent.calls == ["status"]


def test_add_cursor_defaults_the_alias_and_has_a_json_envelope(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rig.main("add", "--kind", "cursor", "--json") == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema": 1,
        "ok": True,
        "action": "add",
        "alias": "cursor",
        "kind": "cursor",
        "logged_in": True,
    }


def test_add_cursor_when_logged_out_says_to_run_cursor_agent_login(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = Rig(tmp_home, logged_in=False)

    assert rig.main("add", "zvoove", "--kind", "cursor") == 0

    out = capsys.readouterr().out
    assert "is not logged in: run: cursor-agent login" in out
    assert cursor.is_cursor(rig.root, "zvoove")


def test_add_cursor_refuses_when_cursor_agent_is_not_installed(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = Rig(tmp_home, installed=False)

    assert rig.main("add", "--kind", "cursor") == 2

    assert "cursor-agent is not installed" in capsys.readouterr().err
    assert cursor.list_cursor_aliases(rig.root) == []


def test_aliases_are_one_namespace_and_auto_is_reserved(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_account(rig.root, "work", active=True)
    rig.main("add", "seat", "--kind", "cursor")
    capsys.readouterr()

    assert rig.main("add", "work", "--kind", "cursor") == 2  # a Claude account holds it
    assert rig.main("add", "auto", "--kind", "cursor") == 2  # reserved
    assert rig.main("add", "seat") == 2  # a Claude login may not take the Cursor alias
    assert rig.main("add", "auto") == 2
    assert not (rig.root / "accounts" / "seat.json").exists()
    assert rig.main("add", "x", "--kind", "cursor", "--from-config-dir", "/tmp/nowhere") == 2
    assert capsys.readouterr().err.count("already used (or reserved)") == 4


def test_remove_alias_and_switch_know_the_kind(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.main("add", "seat", "--kind", "cursor")
    rig.queue_usage()
    rig.main("list")  # leaves a usage cache behind
    assert store.usage_path(rig.root, "seat").exists()
    capsys.readouterr()

    assert rig.main("switch", "seat") == 2
    assert "is a Cursor account" in capsys.readouterr().err

    assert rig.main("alias", "seat", "zvoove") == 0
    assert cursor.list_cursor_aliases(rig.root) == ["zvoove"]
    assert rig.main("alias", "zvoove", "auto") == 2  # reserved

    assert rig.main("remove", "zvoove") == 0
    assert cursor.list_cursor_aliases(rig.root) == []
    assert not store.usage_path(rig.root, "seat").exists()
    assert rig.main("remove", "zvoove") == 4  # not found


def test_alias_refuses_a_name_a_claude_account_holds(rig: Rig) -> None:
    seed_account(rig.root, "work", active=True)
    rig.main("add", "seat", "--kind", "cursor")

    assert rig.main("alias", "seat", "work") == 2
    assert cursor.list_cursor_aliases(rig.root) == ["seat"]


# --- vibe list / usage ----------------------------------------------------------------------


def test_list_shows_the_cursor_seat_with_its_real_numbers(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.main("add", "cursor", "--kind", "cursor")
    capsys.readouterr()
    rig.queue_usage(percent=42.4, on_demand_used=1250, on_demand_limit=25_000)

    assert rig.main("list") == 0

    out = capsys.readouterr().out
    assert "cursor  dev@example.com  Cursor · team Zvoove" in out
    assert "Included + bonus" in out and "42%" in out
    assert "On-demand" in out and "$12.50 of $250.00" in out and "billed to the company" in out
    assert "resets" in out
    assert "routing: available (on-demand $237.50 of $250.00 left)" in out
    assert "usage: see cursor.com/dashboard" not in out  # real numbers, not the fallback


def test_list_shows_unknown_never_a_made_up_number(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.main("add", "cursor", "--kind", "cursor")
    capsys.readouterr()
    rig.cursor_client.queue_json(200, {"shape": "changed"})
    rig.cursor_client.queue_json(200, {})

    assert rig.main("list") == 0

    out = capsys.readouterr().out
    assert "usage: unknown (usage unknown (the answer is not in a shape vibe knows))" in out
    assert "see cursor.com/dashboard" in out
    assert "%" not in out and "$" not in out


def test_list_without_a_token_or_when_logged_out(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = Rig(tmp_home)
    rig.main("add", "cursor", "--kind", "cursor")
    rig.token.value = None
    capsys.readouterr()

    rig.main("list")
    assert "usage: unknown (usage unknown (no Cursor login token found))" in capsys.readouterr().out

    rig.agent.logged_in = False
    rig.main("list")
    assert "not logged in — run: cursor-agent login" in capsys.readouterr().out
    assert rig.cursor_client.requests == []


def test_list_json_adds_a_cursor_member_only_when_there_is_a_seat(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_account(rig.root, "work", credential=make_credential(), active=True)
    rig.client.queue_json(200, fixture_usage())
    rig.main("list", "--json")
    assert "cursor" not in json.loads(capsys.readouterr().out)  # a Claude-only setup is unchanged

    rig.main("add", "cursor", "--kind", "cursor")
    capsys.readouterr()
    rig.client.queue_json(200, fixture_usage())
    rig.queue_usage(percent=10.0, on_demand_used=0)
    rig.main("list", "--json")
    payload = json.loads(capsys.readouterr().out)

    assert [account["alias"] for account in payload["accounts"]] == ["work"]
    assert payload["pool"]["accounts"] == 1  # the seat is not an account-week
    (entry,) = payload["cursor"]
    assert entry == {
        "alias": "cursor",
        "kind": "cursor",
        "state": "ok",
        "message": None,
        "email": "dev@example.com",
        "team": "Zvoove",
        "plan": None,
        "included_percent": 10.0,
        "on_demand_used_cents": 0,
        "on_demand_limit_cents": 25_000,
        "resets_at": entry["resets_at"],
        "available": True,
        "available_why": "included usage and bonus left, on-demand untouched",
    }
    assert entry["resets_at"].endswith("+00:00")


def test_list_shows_a_seat_with_no_claude_account(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.main("add", "cursor", "--kind", "cursor")
    capsys.readouterr()
    rig.queue_usage()

    assert rig.main("usage", "--once") == 0

    out = capsys.readouterr().out
    assert "no accounts yet" not in out and "Included + bonus" in out


def test_a_cached_answer_spares_the_next_request_and_a_dead_one_is_not_shown_forever(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.main("add", "cursor", "--kind", "cursor")
    rig.queue_usage()
    rig.main("list")
    capsys.readouterr()
    assert len(rig.cursor_client.requests) == 2

    rig.main("list")  # inside two minutes: from the cache
    assert len(rig.cursor_client.requests) == 2

    # Three minutes later the endpoint is down: the cached numbers are shown with their time...
    rig.cursor_client.queue(NetworkError("could not reach api2.cursor.sh: URLError", "x"))
    ctx = rig.ctx()
    ctx.now_s = NOW_S + 180
    cli.main(["list"], context=ctx)
    assert "showing" in capsys.readouterr().out

    # ...and half an hour later they are no longer trusted.
    rig.cursor_client.queue(NetworkError("could not reach api2.cursor.sh: URLError", "x"))
    ctx.now_s = NOW_S + 1800
    cli.main(["list"], context=ctx)
    assert "usage: unknown" in capsys.readouterr().out


def test_the_token_never_reaches_any_output(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    rig.main("add", "cursor", "--kind", "cursor")
    capsys.readouterr()
    rig.cursor_client.queue(NetworkError(f"boom {rig.token.value}", "x"))

    rig.main("list")
    rig.cursor_client.queue(NetworkError(f"boom {rig.token.value}", "x"))
    rig.main("list", "--json")

    captured = capsys.readouterr()
    assert rig.token.value not in captured.out + captured.err
    for path in rig.root.rglob("*"):
        if path.is_file():
            assert str(rig.token.value).encode() not in path.read_bytes()


# --- auto never goes to Cursor -----------------------------------------------------------------


def _claude_world(
    tmp_home: Path, rig: Rig, policy: Policy | None = None, *, b_weekly: int = 99
) -> None:
    for alias, weekly in (("a", 99), ("b", b_weekly)):
        seed_account(
            rig.root,
            alias,
            identity=Identity(None, f"u{alias}", None, None, None, None, None),
            credential=make_credential(access=f"{alias}-file", refresh=f"{alias}-rt"),
            active=alias == "a",
        )
        store.write_usage_cache(
            rig.root,
            alias,
            store.UsageCache(
                1_757_930_000.0,
                NOW_S - 10,
                {
                    "limits": [
                        {
                            "kind": "session",
                            "percent": 10,
                            "resets_at": "2099-01-01T00:00:00+00:00",
                        },
                        {
                            "kind": "weekly_all",
                            "percent": weekly,
                            "resets_at": "2099-01-01T00:00:00+00:00",
                        },
                    ]
                },
                None,
                0,
                None,
            ),
        )
    (tmp_home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "ua"}}))
    from vibemaxxing.credentials import credential_to_blob

    rig.port.blob = credential_to_blob(make_credential(access="a-live", refresh="a-live-rt"))
    auto.write_settings(rig.root, Settings(auto_switch=True, policy=policy or Policy()))


@pytest.mark.parametrize("named", [False, True])
def test_auto_tick_never_switches_to_cursor_even_when_everything_else_is_spent(
    tmp_home: Path, rig: Rig, named: bool
) -> None:
    rig.main("add", "cursor", "--kind", "cursor")
    # Cursor has plenty of room; both Claude accounts are at 99%.
    rig.queue_usage(percent=1.0, on_demand_used=0)
    policy = Policy(steps=(Step("a", 85), Step("cursor", 99), Step("b", 99))) if named else None
    _claude_world(tmp_home, rig, policy)

    result = auto.tick(
        rig.root, client=rig.client, port=rig.port, now_s=NOW_S, notify=lambda _: None
    )

    assert "cursor" not in result.line
    assert store.read_active(rig.root) in ("a", "b")
    assert rig.cursor_client.requests == []  # a tick does not even ask Cursor
    assert auto.read_last(rig.root) is None or auto.read_last(rig.root).to_alias != "cursor"  # type: ignore[union-attr]


def test_control_the_same_world_does_switch_when_a_claude_account_has_room(
    tmp_home: Path, rig: Rig
) -> None:
    rig.main("add", "cursor", "--kind", "cursor")
    _claude_world(tmp_home, rig, b_weekly=10)

    result = auto.tick(
        rig.root, client=rig.client, port=rig.port, now_s=NOW_S, notify=lambda _: None
    )

    assert result.switched and "a -> b" in result.line


def test_auto_exclude_and_include_do_not_pretend_cursor_is_a_claude_account(rig: Rig) -> None:
    rig.main("add", "cursor", "--kind", "cursor")

    assert rig.main("auto", "exclude", "cursor") == 4
