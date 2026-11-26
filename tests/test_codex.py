"""The Codex account kind: the CLI's login read, its plan usage fetched, and every surface.

No test reaches the real ~/.codex, ChatGPT or the network: fake logins, a fake HTTP client and
answers in the shape the Codex CLI's own backend client reads.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from textual.widgets import Static

from tests.codex_fakes import (
    FAKE_ACCESS,
    StubAuth,
    api_key_document,
    auth_document,
    login,
    spend_answer,
    usage_answer,
    write_auth,
)
from tests.fakes import FakeHttpClient, FakeKeychain, fixture_usage, make_credential, seed_account
from tests.test_cursor_display import DOM_SHIM, REPORT
from vibemaxxing import cli, codex, codex_usage, launch, store, tui, web
from vibemaxxing.errors import NetworkError
from vibemaxxing.httpclient import CODEX_HOSTS, HTTPError, UrllibClient

NOW_S = 1_757_930_000.0


class Rig:
    def __init__(self, tmp_home: Path, document: object | None = None) -> None:
        self.root = tmp_home / ".vibemaxxing"
        self.auth = StubAuth(auth_document() if document is None else document)
        self.client = FakeHttpClient()
        self.codex_client = FakeHttpClient()
        self.port = FakeKeychain()
        self.now_s = NOW_S

    def ctx(self) -> cli.Context:
        return cli.Context(
            root=self.root,
            client=self.client,
            port=self.port,
            now_s=self.now_s,
            clock=lambda: self.now_s,
            prompt=lambda _: "",
            browser=lambda _: True,
            codex_client=self.codex_client,
            codex_auth=self.auth,
        )

    def main(self, *argv: str) -> int:
        return cli.main(list(argv), context=self.ctx())

    def add(self, alias: str = "zvoove-codex") -> None:
        assert self.main("add", alias, "--kind", "codex") == 0


@pytest.fixture
def rig(tmp_home: Path) -> Rig:
    return Rig(tmp_home)


# --- reading the CLI's login ---------------------------------------------------------------


def test_the_login_is_read_from_auth_json_and_nothing_is_written(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    path = write_auth(home, auth_document(email="me@example.com", plan="pro", exp=2_000_000_000))
    before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in home.iterdir()))

    found = codex.FileAuth().read(home)

    assert found is not None
    assert (found.auth_mode, found.email, found.plan) == ("chatgpt", "me@example.com", "pro")
    assert found.account_id == "acct-123" and found.expires_at == 2_000_000_000
    assert found.access_token is not None and found.usable
    assert FAKE_ACCESS not in repr(found) and FAKE_ACCESS not in str(found)  # a Secret
    assert before == (
        path.read_bytes(),
        path.stat().st_mtime_ns,
        sorted(p.name for p in home.iterdir()),
    )


def test_an_api_key_login_has_no_token_and_says_so() -> None:
    found = codex.parse_auth(api_key_document())

    assert found is not None and found.auth_mode == "apikey"
    assert found.access_token is None and not found.usable and found.has_api_key


def test_an_older_file_without_auth_mode_still_reads() -> None:
    found = codex.parse_auth(auth_document(mode=None))

    assert found is not None and found.auth_mode == "chatgpt" and found.usable


@pytest.mark.parametrize(
    "document", [None, [], "x", {}, {"tokens": "no"}, {"OPENAI_API_KEY": None}]
)
def test_a_file_that_is_not_a_login_reads_as_none(document: object) -> None:
    assert codex.parse_auth(document) is None


def test_a_missing_or_broken_file_reads_as_none(tmp_path: Path) -> None:
    assert codex.FileAuth().read(tmp_path / "nowhere") is None
    (tmp_path / "auth.json").write_text("{not json")
    assert codex.FileAuth().read(tmp_path) is None


def test_the_home_is_codex_home_or_dot_codex(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert codex.default_home() == tmp_home / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(tmp_home / "elsewhere"))
    assert codex.default_home() == tmp_home / "elsewhere"


# --- parsing and fetching the usage ----------------------------------------------------------


def test_the_windows_are_labelled_as_the_cli_labels_them() -> None:
    usage = codex_usage.parse_usage(usage_answer(session_percent=12, weekly_percent=34))

    assert usage is not None and usage.plan == "plus"
    assert [(w.label, w.used_percent, w.window_minutes) for w in usage.windows] == [
        ("5h", 12.0, 300),
        ("weekly", 34.0, 10_080),
    ]
    assert usage.windows[0].resets_at_s == NOW_S + 3 * 3600
    assert (usage.allowed, usage.limit_reached) == (True, False)
    assert usage.credits_balance == "0" and usage.credits_unlimited is False
    assert codex_usage.window_label(1440, secondary=False) == "daily"
    assert codex_usage.window_label(43_200, secondary=False) == "monthly"
    assert codex_usage.window_label(None, secondary=False) == "usage"
    assert codex_usage.window_label(77, secondary=True) == "secondary usage"


def test_additional_limits_are_named_by_the_limit() -> None:
    answer = usage_answer()
    answer["additional_rate_limits"] = [
        {
            "limit_name": "GPT-5-Codex-Spark",
            "metered_feature": "spark",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 7,
                    "limit_window_seconds": 18_000,
                    "reset_after_seconds": 1,
                    "reset_at": 1_757_940_000,
                },
            },
        }
    ]

    usage = codex_usage.parse_usage(answer)

    assert usage is not None
    assert [w.label for w in usage.windows] == ["5h", "weekly", "GPT-5-Codex-Spark 5h"]


def test_a_shape_the_cli_does_not_read_is_unknown_never_invented() -> None:
    assert codex_usage.parse_usage({"shape": "changed"}) is None
    assert codex_usage.parse_usage({}) is None
    bad = {"plan_type": "plus", "rate_limit": {"primary_window": {"used_percent": "lots"}}}
    parsed = codex_usage.parse_usage(bad)
    assert parsed is not None and parsed.windows == ()  # no number made up from a bad window


def test_the_payload_round_trips_through_the_cache_without_a_token() -> None:
    usage = codex_usage.parse_usage(usage_answer())
    assert usage is not None

    payload = codex_usage.usage_to_payload(usage)

    assert codex_usage.usage_from_payload(payload) == usage
    assert FAKE_ACCESS not in json.dumps(payload)


def test_the_request_is_the_clis_own_get_with_one_bearer_header() -> None:
    client = FakeHttpClient()
    client.queue_json(200, usage_answer())

    usage, why = codex_usage.fetch(client, login())

    assert usage is not None and why == ""
    (request,) = client.requests
    assert request.method == "GET" and request.body is None
    assert request.url == "https://chatgpt.com/backend-api/wham/usage"
    assert request.headers["Authorization"] == f"Bearer {FAKE_ACCESS}"
    assert request.headers["ChatGPT-Account-ID"] == "acct-123"


def test_a_rejected_token_says_to_run_codex_and_carries_no_token() -> None:
    client = FakeHttpClient()
    client.queue(HTTPError(401, f"nope {FAKE_ACCESS}".encode()))

    usage, why = codex_usage.fetch(client, login())

    assert usage is None
    assert (
        why == "usage unknown (ChatGPT answered HTTP 401 - run `codex` once, it renews its login)"
    )
    assert FAKE_ACCESS not in why


def test_a_network_failure_is_a_reason_not_a_crash() -> None:
    client = FakeHttpClient()
    client.queue(NetworkError("could not reach chatgpt.com: TimeoutError", "vibe list"))

    usage, why = codex_usage.fetch(client, login())

    assert usage is None and "could not reach chatgpt.com" in why


def test_the_codex_client_reaches_chatgpt_and_no_other_host() -> None:
    assert set(CODEX_HOSTS) == {"chatgpt.com"}
    client = UrllibClient(CODEX_HOSTS)
    for url in ("https://api.anthropic.com/x", "https://api2.cursor.sh/x", "https://evil.example/"):
        with pytest.raises(NetworkError, match="refusing to contact"):
            client.request("GET", url, headers={"Authorization": "Bearer x"})
    # and no Claude client may be pointed at ChatGPT
    with pytest.raises(NetworkError, match="refusing to contact"):
        UrllibClient().request("GET", codex_usage.USAGE_URL, headers={})


# --- read_codex: cache, expiry, states --------------------------------------------------------


def services(rig: Rig, *, now_s: float = NOW_S) -> launch.Services:
    from dataclasses import replace

    return replace(rig.ctx().services(), now_s=now_s)


def test_a_fresh_answer_is_cached_for_two_minutes(rig: Rig) -> None:
    rig.add()
    rig.codex_client.queue_json(200, usage_answer())

    first = launch.read_codex(services(rig), "zvoove-codex")
    again = launch.read_codex(services(rig, now_s=NOW_S + 60), "zvoove-codex")

    assert first.state == "ok" and first.note == "" and again.usage == first.usage
    assert len(rig.codex_client.requests) == 1  # the second read used the cache
    rig.codex_client.queue_json(200, usage_answer(session_percent=50))
    later = launch.read_codex(services(rig, now_s=NOW_S + 130), "zvoove-codex")
    assert later.usage is not None and later.usage.windows[0].used_percent == 50


def test_an_ended_token_sends_nothing_and_shows_the_last_numbers_with_their_time(
    tmp_home: Path,
) -> None:
    rig = Rig(tmp_home, auth_document(exp=NOW_S + 100))
    rig.add()
    rig.codex_client.queue_json(200, usage_answer())
    launch.read_codex(services(rig), "zvoove-codex")

    shown = launch.read_codex(services(rig, now_s=NOW_S + 300), "zvoove-codex")

    assert len(rig.codex_client.requests) == 1  # the ended token was not sent
    assert shown.state == "ok" and shown.usage is not None
    assert "token ended" in shown.note and "run `codex` to renew it" in shown.note
    assert "showing" in shown.note
    # nothing older than 15 minutes is shown as if it were current
    gone = launch.read_codex(services(rig, now_s=NOW_S + 2000), "zvoove-codex")
    assert gone.state == "unknown" and gone.usage is None and "token ended" in gone.note


def test_a_failing_request_shows_the_old_numbers_then_unknown(rig: Rig) -> None:
    rig.add()
    rig.codex_client.queue_json(200, usage_answer())
    launch.read_codex(services(rig), "zvoove-codex")
    rig.codex_client.queue(HTTPError(500, b""))

    shown = launch.read_codex(services(rig, now_s=NOW_S + 200), "zvoove-codex")

    assert shown.usage is not None and "HTTP 500" in shown.note and "showing" in shown.note
    rig.codex_client.queue(HTTPError(500, b""))
    unknown = launch.read_codex(services(rig, now_s=NOW_S + 5000), "zvoove-codex")
    assert unknown.state == "unknown" and unknown.usage is None


def test_no_login_and_an_api_key_login_are_states_of_their_own(tmp_home: Path) -> None:
    gone = Rig(tmp_home)
    gone.add()
    gone.auth.document = None
    reading = launch.read_codex(services(gone), "zvoove-codex")
    assert reading.state == "needs_login" and "codex login" in reading.note

    gone.auth.document = api_key_document()
    key = launch.read_codex(services(gone), "zvoove-codex")
    assert key.state == "api_key" and "API key" in key.note and key.usage is None
    assert gone.codex_client.requests == []  # nothing to ask for


def test_a_cache_of_another_account_under_the_same_alias_is_not_used(rig: Rig) -> None:
    rig.add()
    rig.codex_client.queue_json(200, usage_answer(session_percent=88))
    launch.read_codex(services(rig), "zvoove-codex")
    assert rig.main("remove", "zvoove-codex") == 0
    rig.now_s = NOW_S + 10
    rig.add()  # new added_at
    rig.codex_client.queue_json(200, usage_answer(session_percent=3))

    reading = launch.read_codex(services(rig, now_s=NOW_S + 10), "zvoove-codex")

    assert reading.usage is not None and reading.usage.windows[0].used_percent == 3


# --- vibe add / remove / alias / switch / run --------------------------------------------------


def test_add_codex_stores_a_marker_only_and_reads_no_network(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rig.main("add", "zvoove-codex", "--kind", "codex") == 0

    out = capsys.readouterr().out
    assert 'added Codex account "zvoove-codex" (logged in as dev@example.com, plan plus)' in out
    marker = rig.root / "codex" / "zvoove-codex.json"
    document = json.loads(marker.read_text())
    assert document == {"schema": 1, "alias": "zvoove-codex", "kind": "codex", "added_at": NOW_S}
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert store.list_aliases(rig.root) == []  # no Claude account file, no credential of ours
    assert rig.port.log == [] and rig.codex_client.requests == [] and rig.client.requests == []
    assert FAKE_ACCESS not in out


def test_add_codex_json_and_default_alias(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    assert rig.main("add", "--kind", "codex", "--json") == 0

    assert json.loads(capsys.readouterr().out) == {
        "schema": 1,
        "ok": True,
        "action": "add",
        "alias": "codex",
        "kind": "codex",
        "logged_in": True,
    }


def test_add_codex_binds_the_home_it_was_added_under(
    rig: Rig, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_home / "codex-b"
    monkeypatch.setenv("CODEX_HOME", str(other))

    rig.add("b")

    assert rig.auth.reads == [other]
    assert codex.read_marker(rig.root, "b").codex_home == other
    rig.codex_client.queue_json(200, usage_answer())
    launch.read_codex(services(rig), "b")  # a read of the account asks that same home
    assert rig.auth.reads[-1] == other


def test_add_codex_without_a_login_says_how_to_log_in(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = Rig(tmp_home)
    rig.auth.document = None

    assert rig.main("add", "zvoove-codex", "--kind", "codex") == 0

    out = capsys.readouterr().out
    assert "no Codex login to read: run: codex login" in out
    assert "cli_auth_credentials_store" in out  # the keyring mode keeps no auth.json
    assert codex.is_codex(rig.root, "zvoove-codex")


def test_add_codex_with_an_api_key_login_says_it_has_no_usage(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = Rig(tmp_home, api_key_document())

    assert rig.main("add", "--kind", "codex", "--json") == 0

    assert json.loads(capsys.readouterr().out)["logged_in"] is False


def test_aliases_are_one_namespace_across_all_three_kinds(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_account(rig.root, "work", active=True)
    rig.add("seat")
    capsys.readouterr()

    assert rig.main("add", "work", "--kind", "codex") == 2
    assert rig.main("add", "auto", "--kind", "codex") == 2
    assert rig.main("add", "seat", "--kind", "codex") == 2
    assert rig.main("add", "seat") == 2  # a Claude login may not take a Codex alias
    assert rig.main("add", "x", "--kind", "codex", "--from-config-dir", "/tmp/nowhere") == 2
    assert capsys.readouterr().err.count("already used (or reserved)") == 4


def test_switch_run_and_auto_never_go_to_a_codex_account(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_account(rig.root, "work", active=True)
    rig.add("seat")
    capsys.readouterr()

    assert rig.main("switch", "seat") == 2
    assert "is a Codex account" in capsys.readouterr().err
    assert rig.main("run", "seat", "--dry-run", "--", "claude") == 2
    assert "is a Codex account" in capsys.readouterr().err
    # Auto-switch and the pool walk the Claude store: a Codex marker is not in it.
    assert store.list_aliases(rig.root) == ["work"]
    assert rig.port.log == [] or all("seat" not in str(entry) for entry in rig.port.log)


def test_remove_and_alias_know_the_kind(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    rig.add("seat")
    rig.codex_client.queue_json(200, usage_answer())
    rig.main("list")  # leaves a usage cache behind
    assert store.usage_path(rig.root, "seat").exists()
    capsys.readouterr()

    assert rig.main("alias", "seat", "zvoove-codex") == 0
    assert codex.list_codex_aliases(rig.root) == ["zvoove-codex"]
    assert rig.main("alias", "zvoove-codex", "auto") == 2
    assert rig.main("remove", "zvoove-codex") == 0
    assert codex.list_codex_aliases(rig.root) == []
    assert rig.main("remove", "zvoove-codex") == 4
    assert not store.usage_path(rig.root, "zvoove-codex").exists()


# --- vibe list ---------------------------------------------------------------------------


def test_list_shows_the_plan_windows_next_to_claude(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_account(rig.root, "main", credential=make_credential(), active=True)
    rig.add()
    capsys.readouterr()
    rig.client.queue_json(200, fixture_usage())
    rig.codex_client.queue_json(200, usage_answer(session_percent=12, weekly_percent=34))

    assert rig.main("list") == 0

    out = capsys.readouterr().out
    assert "zvoove-codex  dev@example.com  Codex · plus" in out
    assert "5h" in out and "12%" in out and "weekly" in out and "34%" in out and "resets" in out
    assert out.index("main") < out.index("zvoove-codex") < out.index("pool")
    assert FAKE_ACCESS not in out


def test_list_alone_with_only_a_codex_account(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    rig.add()
    capsys.readouterr()
    rig.codex_client.queue_json(200, usage_answer())

    assert rig.main("list") == 0

    out = capsys.readouterr().out
    assert "zvoove-codex" in out and "no accounts yet" not in out


def test_list_says_unknown_never_a_made_up_number(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.add()
    capsys.readouterr()
    rig.codex_client.queue_json(200, {"shape": "changed"})

    assert rig.main("list") == 0

    out = capsys.readouterr().out
    assert "usage: unknown (usage unknown (the answer is not in a shape vibe knows))" in out
    assert "%" not in out


def test_list_for_a_missing_login_and_an_api_key(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = Rig(tmp_home)
    rig.add()
    rig.auth.document = None
    capsys.readouterr()
    assert rig.main("list") == 0
    assert "run: codex login" in capsys.readouterr().out

    rig.auth.document = api_key_document()
    assert rig.main("list") == 0
    assert "signed in with an API key: no plan usage to read" in capsys.readouterr().out


def test_list_json_has_a_codex_member_only_when_an_account_exists(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_account(rig.root, "main", credential=make_credential(), active=True)
    rig.client.queue_json(200, fixture_usage())
    assert rig.main("list", "--json") == 0
    assert "codex" not in json.loads(capsys.readouterr().out)  # Claude-only: as it always was

    rig.add()
    capsys.readouterr()
    rig.client.queue_json(200, fixture_usage())
    rig.codex_client.queue_json(200, usage_answer(limit_reached=True, weekly_percent=100))
    assert rig.main("list", "--json") == 0

    payload = json.loads(capsys.readouterr().out)
    (entry,) = payload["codex"]
    assert entry == {
        "alias": "zvoove-codex",
        "kind": "codex",
        "state": "ok",
        "message": None,
        "email": "dev@example.com",
        "plan": "plus",
        "limit_reached": True,
        "credits_balance": "0",
        "credits_unlimited": False,
        "spend": None,
        "windows": [
            {
                "label": "5h",
                "percent": 12,
                "window_minutes": 300,
                "resets_at": "2025-09-15T12:53:20+00:00",
            },
            {
                "label": "weekly",
                "percent": 100,
                "window_minutes": 10_080,
                "resets_at": "2025-09-19T09:53:20+00:00",
            },
        ],
    }
    assert [a["alias"] for a in payload["accounts"]] == ["main"]  # never in accounts or the pool
    assert payload["pool"]["accounts"] == 1
    assert FAKE_ACCESS not in json.dumps(payload)


def test_a_damaged_marker_does_not_stop_list(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    rig.add()
    (rig.root / "codex" / "zvoove-codex.json").write_text("{broken")
    capsys.readouterr()

    assert rig.main("list") == 0

    assert "zvoove-codex" in capsys.readouterr().out


# --- the dashboard -----------------------------------------------------------------------


def test_the_snapshot_carries_the_account_and_the_claude_only_one_does_not(rig: Rig) -> None:
    seed_account(rig.root, "main", credential=make_credential(), active=True)
    rig.add()
    rig.client.queue_json(200, fixture_usage())
    rig.codex_client.queue_json(200, usage_answer())

    envelope = web.snapshot(rig.ctx())

    (entry,) = envelope["codex"]  # type: ignore[misc]
    assert entry["alias"] == "zvoove-codex" and entry["windows"][0]["percent"] == 12  # type: ignore[index]
    assert [a["alias"] for a in envelope["accounts"]] == ["main"]  # type: ignore[attr-defined]

    codex.delete_marker(rig.root, "zvoove-codex")
    rig.client.queue_json(200, fixture_usage())
    assert "codex" not in web.snapshot(rig.ctx())


def reading(**kw: object) -> launch.CodexReading:
    usage = codex_usage.parse_usage(usage_answer())
    base: dict[str, object] = {
        "alias": "zvoove-codex",
        "state": "ok",
        "email": "dev@example.com",
        "plan": "plus",
        "usage": usage,
        "note": "",
    }
    return launch.CodexReading(**{**base, **kw})  # type: ignore[arg-type]


def test_the_codex_panel_shows_a_bar_per_window_and_its_reset() -> None:
    text = tui.codex_content(reading(), now_s=NOW_S).plain

    assert "zvoove-codex" in text and "dev@example.com" in text and "Codex · plus" in text
    assert "5h" in text and " 12%" in text and "weekly" in text and " 34%" in text
    assert "in 3h" in text and "in 4d" in text


def test_the_codex_panel_for_unknown_logged_out_and_limit_reached() -> None:
    unknown = tui.codex_content(
        reading(usage=None, state="unknown", note="usage unknown (HTTP 500)"), now_s=NOW_S
    ).plain
    assert "usage: unknown (usage unknown (HTTP 500))" in unknown and "%" not in unknown

    out = tui.codex_content(
        reading(usage=None, state="needs_login", note="run: codex login"), now_s=NOW_S
    ).plain
    assert "run: codex login" in out

    reached = codex_usage.parse_usage(usage_answer(limit_reached=True, weekly_percent=100))
    styled = tui.codex_content(reading(usage=reached), now_s=NOW_S)
    marked = " ".join(styled.plain[s.start : s.end] for s in styled.spans if s.style)
    assert "100%" in marked  # elevated: the used-up window stands out


def test_the_panel_scrubs_what_the_note_might_carry() -> None:
    from vibemaxxing.redact import Secret

    Secret(FAKE_ACCESS)  # registers it, as a real read would
    text = tui.codex_content(
        reading(usage=None, state="unknown", note=f"boom {FAKE_ACCESS}"), now_s=NOW_S
    ).plain

    assert FAKE_ACCESS not in text


async def test_the_dashboard_paints_the_account_next_to_the_claude_ones(tmp_home: Path) -> None:
    rig = Rig(tmp_home)
    seed_account(rig.root, "main", credential=make_credential(), active=True)
    rig.add()
    rig.client.queue_json(200, fixture_usage())
    rig.codex_client.queue_json(200, usage_answer())
    app = tui.Dashboard(rig.ctx())

    async with app.run_test() as pilot:
        await asyncio.wait_for(app.first_paint.wait(), timeout=10)
        await pilot.pause()
        assert set(app.panels) == {"main", "zvoove-codex"}
        text = app.panels["zvoove-codex"].visual.plain  # type: ignore[union-attr]
        assert "5h" in text and "weekly" in text and "34%" in text
        assert "pool" in str(app.query_one("#footer", Static).visual.plain)  # type: ignore[union-attr]


def web_envelope() -> dict[str, object]:
    return {
        "schema": 1,
        "generated_at": "2026-11-19T12:00:00+00:00",
        "accounts": [
            {
                "alias": "main", "active": True, "state": "ok", "message": None,
                "email": "me@example.com", "display_name": None, "organization": None,
                "plan": "max", "updated_at": None,
                "rows": [{"kind": "session", "label": "Session", "percent": 3,
                          "severity": "normal", "resets_at": "2026-11-19T15:00:00+00:00"}],
                "breakdown": [],
            }
        ],
        "pool": {"accounts": 1, "remaining_account_weeks": 0.9, "dry_in_seconds": None},
        "resets": [],
        "codex": [
            {
                "alias": "zvoove-codex", "kind": "codex", "state": "ok", "message": None,
                "email": "dev@example.com", "plan": "plus", "limit_reached": False,
                "credits_balance": "0", "credits_unlimited": False,
                "windows": [
                    {"label": "5h", "percent": 12, "window_minutes": 300,
                     "resets_at": "2026-11-19T15:00:00+00:00"},
                    {"label": "weekly", "percent": 34, "window_minutes": 10080,
                     "resets_at": "2026-11-23T15:00:00+00:00"},
                ],
            }
        ],
    }  # fmt: skip


def run_page(tmp_path: Path, envelope: dict[str, object]) -> list[str]:
    page = web.page()
    script = page[page.index("<script>") + len("<script>") : page.rindex("</script>")]
    harness = tmp_path / "page.js"
    harness.write_text(f"const ENVELOPE = {json.dumps(envelope)};{DOM_SHIM}{script}{REPORT}")
    done = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip().splitlines()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_renders_the_account_as_a_ledger_entry(tmp_path: Path) -> None:
    texts, widths, rest = run_page(tmp_path, web_envelope())

    shown = json.loads(texts)
    assert len(shown) == 2  # main, then the Codex account
    text = shown[1]
    assert "zvoove-codex" in text and "dev@example.com" in text and "Codex · plus" in text
    assert "5h" in text and "12%" in text and "weekly" in text and "34%" in text
    assert json.loads(widths)[1] == ["12%", "34%"]  # both bars painted
    assert rest.startswith("true updated")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("state", ["unknown", "needs_login", "api_key"])
def test_the_page_says_unknown_or_logged_out_without_inventing_numbers(
    tmp_path: Path, state: str
) -> None:
    envelope = web_envelope()
    entries = envelope["codex"]
    assert isinstance(entries, list)
    entries[0].update(state=state, windows=[], message="usage unknown (HTTP 500)")

    out = json.loads(run_page(tmp_path, envelope)[0])[1]

    assert "usage unknown (HTTP 500)" in out
    assert "%" not in out  # no number is invented


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_claude_only_envelope_renders_as_before(tmp_path: Path) -> None:
    envelope = web_envelope()
    del envelope["codex"]

    texts, _, rest = run_page(tmp_path, envelope)

    assert len(json.loads(texts)) == 1 and rest.startswith("true updated")


# --- a usage-based workspace: a spend limit in credits, no windows ---------------------------


def test_the_credit_limit_is_read_in_its_own_unit() -> None:
    usage = codex_usage.parse_usage(spend_answer(used="12.5", used_percent=0))

    assert (
        usage is not None
        and usage.windows == ()
        and usage.plan == "self_serve_business_usage_based"
    )
    spend = usage.spend
    assert spend is not None
    assert (spend.unit, spend.used, spend.limit, spend.resets_at_s) == (
        "credit",
        12.5,
        5100.0,
        1_796_083_200.0,
    )
    assert codex_usage.spend_title(spend) == "Credits used 12.5 of 5,100"
    assert usage.limit_reached is False
    # The cache round trip keeps it.
    assert codex_usage.usage_from_payload(codex_usage.usage_to_payload(usage)) == usage


def test_another_unit_is_shown_as_the_answer_names_it() -> None:
    answer = spend_answer()
    answer["spend_control"]["individual_limit"]["unit"] = "token"  # type: ignore[index]

    usage = codex_usage.parse_usage(answer)

    assert usage is not None and usage.spend is not None
    assert codex_usage.spend_title(usage.spend) == "Tokens used 0 of 5,100"


def test_a_spend_limit_that_is_not_a_number_is_no_limit() -> None:
    answer = spend_answer()
    answer["spend_control"]["individual_limit"]["limit"] = "lots"  # type: ignore[index]

    usage = codex_usage.parse_usage(answer)

    assert usage is not None and usage.spend is None


def test_list_shows_the_credit_limit_not_no_limits_reported(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.add()
    capsys.readouterr()
    rig.codex_client.queue_json(200, spend_answer())

    assert rig.main("list") == 0

    out = capsys.readouterr().out
    assert "Credits used 0 of 5,100 (resets 1 Dec)" in out
    assert "no limits reported" not in out


def test_list_json_carries_the_spend_limit(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    rig.add()
    capsys.readouterr()
    rig.codex_client.queue_json(200, spend_answer(used="510.0", used_percent=10))

    assert rig.main("list", "--json") == 0

    (entry,) = json.loads(capsys.readouterr().out)["codex"]
    assert entry["windows"] == []
    assert entry["spend"] == {
        "label": "Credits used 510 of 5,100",
        "unit": "credit",
        "used": 510.0,
        "limit": 5100.0,
        "percent": 10,
        "resets_at": "2026-12-01T00:00:00+00:00",
    }


def test_the_codex_panel_shows_the_credit_limit_as_one_bar() -> None:
    usage = codex_usage.parse_usage(spend_answer(used="510.0", used_percent=10))

    text = tui.codex_content(reading(usage=usage), now_s=1_795_000_000.0).plain

    assert "Credits used 510 of 5,100 █" in text and " 10%" in text and "in 13d" in text
    assert "no limits reported" not in text


def test_the_status_line_says_the_credit_limit_and_marks_old_numbers(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    from vibemaxxing import auto, statusline

    rig.add()
    capsys.readouterr()
    rig.codex_client.queue_json(200, spend_answer())
    assert rig.main("list") == 0

    auto.publish_status(rig.root, live=None, now_s=rig.now_s)
    document = statusline.read(rig.root)

    assert "zvoove-codex: 0%" in statusline.render(document, now_s=rig.now_s)
    assert "zvoove-codex: 0%?" in statusline.render(document, now_s=rig.now_s + 3600)


def test_the_status_line_shows_windows_for_a_plan_that_has_them(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    from vibemaxxing import auto, statusline

    rig.add()
    capsys.readouterr()
    rig.codex_client.queue_json(200, usage_answer())
    assert rig.main("list") == 0

    auto.publish_status(rig.root, live=None, now_s=rig.now_s)

    assert "zvoove-codex: 5h 12% weekly 34%" in statusline.render(
        statusline.read(rig.root), now_s=rig.now_s
    )
