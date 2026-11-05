"""The token broker: vibe is the one refresher; Claude Code reads mirrors with no refresh token.

Fakes only: the token endpoint is a FakeHttpClient, every Keychain item a FakeKeychain, every
config dir a temp dir under a throwaway HOME. Nothing reaches the real Keychain or a real login.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient, FakeKeychain, make_credential, seed_account
from tests.test_run_cursor import NOW_S, World
from vibemaxxing import auto, broker, credentials, envelope, keychain, oauth, statusline, store
from vibemaxxing.credentials import Identity, credential_to_blob
from vibemaxxing.errors import VibeError
from vibemaxxing.httpclient import HTTPError
from vibemaxxing.models import AccountState

HOUR_MS = 3600 * 1000
NOW_MS = int(NOW_S * 1000)
MAIN = Identity("main@example.com", "umain", None, "org-main", None, None, None)
EXTRA = Identity("extra@example.com", "uextra", None, "org-extra", None, None, None)


# --- a small world ----------------------------------------------------------------------------


class Broker:
    """Two accounts vibe holds, main live, and the broker on; Claude Code's items are fakes."""

    def __init__(self, home: Path, *, on: bool = True, main_left_h: float = 7.5) -> None:
        self.home = home
        self.root = home / ".vibemaxxing"
        self.client = FakeHttpClient()
        self.port = FakeKeychain()  # Claude Code's default item (~/.claude)
        self.dirs: dict[str, FakeKeychain] = {}
        (home / ".claude").mkdir()
        seed_account(
            self.root,
            "main",
            identity=MAIN,
            credential=make_credential(
                access="main-at-1",
                refresh="main-rt-1",
                expires_at_ms=NOW_MS + int(main_left_h * HOUR_MS),
            ),
            active=True,
        )
        seed_account(
            self.root,
            "extra",
            identity=EXTRA,
            credential=make_credential(
                access="extra-at-1", refresh="extra-rt-1", expires_at_ms=NOW_MS + 7 * HOUR_MS
            ),
        )
        (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "umain"}}))
        # What vibe wrote into the default item when it made main live: a mirror.
        self.port.blob = broker.mirror_blob(
            store.read_account(self.root, "main").credential, {"mcpOAuth": {"kept": True}}
        )
        for alias in ("main", "extra"):
            (home / f".claude-{alias}").mkdir()
        broker.write_settings(self.root, broker.BrokerSettings(on=on))

    def port_for(self, directory: Path) -> FakeKeychain:
        return self.dirs.setdefault(str(directory), FakeKeychain())

    def dir_port(self, alias: str) -> FakeKeychain:
        return self.port_for(self.home / f".claude-{alias}")

    def run(self, now_s: float = NOW_S) -> broker.Report:
        return broker.run(
            self.root,
            client=self.client,
            default_port=self.port,
            port_for=self.port_for,
            now_s=now_s,
            home=self.home,
        )

    def tick(self, now_s: float = NOW_S) -> auto.TickResult:
        return auto.tick(
            self.root,
            client=self.client,
            port=self.port,
            now_s=now_s,
            notify=lambda _: None,
            port_for=self.port_for,
            home=self.home,
        )

    def token_requests(self) -> list[dict[str, object]]:
        return [r.json_body() for r in self.client.requests if r.url == oauth.TOKEN_URL]

    def stored(self, alias: str) -> credentials.Credential:
        return store.read_account(self.root, alias).credential


def oauth_member(blob: str | None) -> dict[str, object]:
    assert blob is not None
    member = json.loads(blob)["claudeAiOauth"]
    assert isinstance(member, dict)
    return member


def token_answer(access: str, refresh: str, *, hours: int = 8) -> dict[str, object]:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": hours * 3600,
        "scope": "user:profile user:inference user:sessions:claude_code user:mcp_servers",
    }


@pytest.fixture
def w(tmp_home: Path) -> Broker:
    return Broker(tmp_home)


# --- refresh ahead of expiry ------------------------------------------------------------------


def test_a_token_is_renewed_once_half_its_lifetime_is_gone() -> None:
    issued = NOW_MS
    token = make_credential(expires_at_ms=issued + 8 * HOUR_MS)

    assert broker.refresh_at_ms(token, issued, 0.5) == issued + 4 * HOUR_MS
    assert not broker.due(token, issued, 0.5, now_ms=issued + 4 * HOUR_MS - 1)
    assert broker.due(token, issued, 0.5, now_ms=issued + 4 * HOUR_MS)


def test_never_later_than_an_hour_before_the_end_well_ahead_of_claude_codes_five_minutes() -> None:
    token = make_credential(expires_at_ms=NOW_MS + 90 * 60 * 1000)
    # Issued 30 minutes before its end would mean "15 minutes left" at half; the floor wins.
    issued = token.expires_at_ms - 30 * 60 * 1000

    at = broker.refresh_at_ms(token, issued, 0.5)

    assert at == token.expires_at_ms - broker.MIN_LEAD_MS
    assert token.expires_at_ms - at > credentials.EXPIRY_BUFFER_MS


def test_the_tick_refreshes_a_due_account_and_leaves_one_that_is_not_due(tmp_home: Path) -> None:
    w = Broker(tmp_home, main_left_h=3)  # under half of eight hours left: due
    w.client.queue_json(200, token_answer("main-at-2", "main-rt-2"))

    report = w.run()

    assert [body["refresh_token"] for body in w.token_requests()] == ["main-rt-1"]
    assert w.stored("main").access_token.reveal() == "main-at-2"
    assert w.stored("main").refresh_token.reveal() == "main-rt-2"
    assert w.stored("extra").access_token.reveal() == "extra-at-1"  # 7 h left: not due
    assert "refreshed main" in report.lines
    assert report.problems == ()


def test_the_active_account_is_vibes_to_refresh_only_with_the_broker_on(tmp_home: Path) -> None:
    w = Broker(tmp_home, on=False)
    spent = make_credential(refresh="main-rt-1", expires_at_ms=NOW_MS - 1)

    outcome = oauth.refresh(w.root, "main", spent, w.client, now_ms=NOW_MS)

    assert outcome.error == "active" and w.client.requests == []  # Claude Code's, as before

    broker.write_settings(w.root, broker.BrokerSettings(on=True))
    w.client.queue_json(200, token_answer("main-at-2", "main-rt-2"))

    assert oauth.refresh(w.root, "main", spent, w.client, now_ms=NOW_MS).error is None


def test_a_settings_member_that_cannot_be_read_means_off(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    root.mkdir()
    for raw in ({"on": "yes"}, {"on": True, "mirrors": ["x"]}, {"on": True, "refresh_left": 2}):
        (root / "settings.json").write_text(json.dumps({"schema": 1, "broker": raw}))
        assert broker.read_settings(root).on is False


def test_auto_settings_keep_the_broker_member(w: Broker) -> None:
    auto.write_settings(w.root, replace(auto.read_settings(w.root), auto_switch=True))

    assert broker.read_settings(w.root).on is True


# --- mirror rewrite ---------------------------------------------------------------------------


def test_a_refresh_rewrites_every_mirror_of_that_account_and_no_refresh_token_reaches_one(
    tmp_home: Path,
) -> None:
    w = Broker(tmp_home, main_left_h=3)
    w.client.queue_json(200, token_answer("main-at-2", "main-rt-2"))

    w.run()

    for blob in (w.port.blob, w.dir_port("main").blob):
        member = oauth_member(blob)
        assert member["accessToken"] == "main-at-2"
        assert "refreshToken" not in member and "refreshTokenExpiresAt" not in member
        assert member["expiresAt"] == w.stored("main").expires_at_ms  # the real end
        assert member["scopes"] == list(w.stored("main").scopes)
    assert json.loads(w.port.blob or "")["mcpOAuth"] == {"kept": True}  # siblings stay
    extra = oauth_member(w.dir_port("extra").blob)
    assert extra["accessToken"] == "extra-at-1" and "refreshToken" not in extra
    profile = json.loads((tmp_home / ".claude-extra" / ".claude.json").read_text())
    assert profile["oauthAccount"]["accountUuid"] == "uextra"
    assert profile["oauthAccount"]["organizationUuid"] == "org-extra"  # Remote Control needs it


def test_an_unchanged_mirror_is_not_written_again(w: Broker) -> None:
    w.run()
    w.dir_port("extra").log.clear()

    w.run(NOW_S + 60)

    assert "keychain.write" not in w.dir_port("extra").log


def test_a_mirror_claude_code_wrote_back_an_older_token_into_is_put_right(w: Broker) -> None:
    w.run()
    old = make_credential(access="extra-at-0", refresh="x", expires_at_ms=NOW_MS + HOUR_MS)
    w.dir_port("extra").blob = credential_to_blob(old, mirror=True)

    w.run(NOW_S + 60)

    assert oauth_member(w.dir_port("extra").blob)["accessToken"] == "extra-at-1"


def test_a_dir_that_does_not_exist_gets_no_mirror_until_it_is_made(w: Broker) -> None:
    (w.home / ".claude-extra").rmdir()

    w.run()

    assert str(w.home / ".claude-extra") not in w.dirs


def test_the_mirror_write_holds_claude_codes_own_write_lock(w: Broker) -> None:
    lock = w.home / ".claude-extra" / keychain.STORAGE_LOCK
    seen: list[bool] = []

    class Watching(FakeKeychain):
        def write(self, blob: str) -> None:
            seen.append(lock.is_dir())
            super().write(blob)

    w.dirs[str(w.home / ".claude-extra")] = Watching()

    w.run()

    assert seen == [True] and not lock.exists()


def test_a_lock_claude_code_holds_is_waited_for_and_a_stale_one_is_taken(tmp_path: Path) -> None:
    lock = tmp_path / keychain.STORAGE_LOCK
    lock.mkdir()
    clock = [1000.0]
    os.utime(lock, (clock[0], clock[0]))

    with (
        pytest.raises(VibeError, match="writing its login"),
        keychain.storage_lock(
            tmp_path,
            wait_s=1,
            clock=lambda: clock[0],
            sleep=lambda s: clock.__setitem__(0, clock[0] + s),
        ),
    ):
        pass

    clock[0] += keychain.STORAGE_LOCK_STALE_S
    with keychain.storage_lock(tmp_path, clock=lambda: clock[0], sleep=lambda _: None):
        assert lock.is_dir()
    assert not lock.exists()


# --- a mirror never refreshes -----------------------------------------------------------------


def test_a_mirror_blob_has_no_refresh_token_key_the_one_thing_claude_code_refreshes_with() -> None:
    member = oauth_member(broker.mirror_blob(make_credential(refresh="secret-rt"), None))

    assert "refreshToken" not in member
    assert "secret-rt" not in json.dumps(member)
    assert not credentials.holds_refresh_token(json.dumps({"claudeAiOauth": member}))


def test_a_switch_never_copies_a_mirror_into_the_store_or_a_refresh_token_into_the_item(
    w: Broker,
) -> None:
    store.switch(w.root, "extra", w.port, now_s=NOW_S)

    assert w.stored("main").refresh_token.reveal() == "main-rt-1"  # not replaced by "nothing"
    member = oauth_member(w.port.blob)
    assert member["accessToken"] == "extra-at-1" and "refreshToken" not in member
    assert json.loads(w.port.blob or "")["mcpOAuth"] == {"kept": True}


def test_a_login_claude_code_made_in_a_mirror_dir_is_adopted_and_then_mirrored(w: Broker) -> None:
    # `claude /login` in ~/.claude-extra: a newer lineage of extra, held by Claude Code.
    (w.home / ".claude-extra" / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "uextra"}})
    )
    newer = make_credential(
        access="extra-cc-at", refresh="extra-cc-rt", expires_at_ms=NOW_MS + 8 * HOUR_MS
    )
    w.dir_port("extra").blob = credential_to_blob(newer)

    report = w.run()

    assert w.stored("extra").refresh_token.reveal() == "extra-cc-rt"
    member = oauth_member(w.dir_port("extra").blob)
    assert member["accessToken"] == "extra-cc-at" and "refreshToken" not in member
    assert any(line.startswith("took over") for line in report.lines)


def test_a_login_of_another_account_in_a_mirror_dir_is_never_written_over(w: Broker) -> None:
    (w.home / ".claude-extra" / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "somebody-else"}})
    )
    stranger = credential_to_blob(make_credential(access="other-at", refresh="other-rt"))
    w.dir_port("extra").blob = stranger

    report = w.run()

    assert w.dir_port("extra").blob == stranger
    assert w.stored("extra").refresh_token.reveal() == "extra-rt-1"
    assert any("did not write" in problem for problem in report.problems)


# --- a failed refresh keeps the old mirror ----------------------------------------------------


def test_a_failed_refresh_keeps_every_mirror_and_is_reported_then_retried_later(
    tmp_home: Path,
) -> None:
    w = Broker(tmp_home, main_left_h=3)
    w.run(NOW_S - 3600 * 10)  # the mirrors, written ten hours earlier when main was not due
    w.client.queue(HTTPError(503, b"{}"))

    report = w.run()

    assert oauth_member(w.dir_port("main").blob)["accessToken"] == "main-at-1"
    assert oauth_member(w.port.blob)["accessToken"] == "main-at-1"
    assert report.problems == ("main: refresh failed (transient)",)
    assert broker.problems(w.root, home=w.home) == ["main: refresh failed (transient)"]

    w.run(NOW_S + 30)  # within the backoff: no second request
    assert len(w.token_requests()) == 1

    w.client.queue_json(200, token_answer("main-at-2", "main-rt-2"))
    w.run(NOW_S + 61)
    assert oauth_member(w.dir_port("main").blob)["accessToken"] == "main-at-2"
    assert broker.problems(w.root, home=w.home) == []


def test_a_rejected_refresh_token_keeps_the_mirror_and_says_how_to_fix_it(tmp_home: Path) -> None:
    w = Broker(tmp_home, main_left_h=3)
    w.run(NOW_S - 3600 * 10)
    w.client.queue(HTTPError(400, json.dumps({"error": "invalid_grant"}).encode()))

    report = w.run()

    assert oauth_member(w.dir_port("main").blob)["accessToken"] == "main-at-1"
    assert "vibe add main" in report.problems[0]
    assert store.read_account(w.root, "main").state is AccountState.NEEDS_LOGIN
    w.run(NOW_S + 3600)
    assert len(w.token_requests()) == 1  # a dead lineage is not posted again


def test_the_problem_shows_in_auto_status_and_vibe_list(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_home, monkeypatch)
    seed_account(world.root, "main", identity=MAIN, active=True)
    (tmp_home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "umain"}}))
    world.port.blob = broker.mirror_blob(store.read_account(world.root, "main").credential, None)
    broker.write_settings(world.root, broker.BrokerSettings(on=True))
    broker.write_state(world.root, {"main": broker.Holding(error="refresh failed (transient)")})

    for _ in range(4):
        world.client.queue(HTTPError(503, b"{}"))  # the usage polls of both commands
    world.main("auto", "status")
    status = capsys.readouterr().out
    world.main("list")
    listed = capsys.readouterr().out

    assert "broker       on" in status and "problem: refresh failed (transient)" in status
    assert "broker problem: main: refresh failed (transient)" in listed


# --- switch and mirrors together --------------------------------------------------------------


def test_a_switch_moves_the_default_mirror_and_leaves_each_dir_on_its_own_account(
    w: Broker,
) -> None:
    w.run()

    store.switch(w.root, "extra", w.port, now_s=NOW_S)
    w.run(NOW_S + 60)

    assert oauth_member(w.port.blob)["accessToken"] == "extra-at-1"
    assert oauth_member(w.dir_port("main").blob)["accessToken"] == "main-at-1"
    assert oauth_member(w.dir_port("extra").blob)["accessToken"] == "extra-at-1"
    assert json.loads((w.home / ".claude.json").read_text())["oauthAccount"]["accountUuid"] == (
        "uextra"
    )
    assert w.stored("main").refresh_token.reveal() == "main-rt-1"
    assert w.stored("extra").refresh_token.reveal() == "extra-rt-1"


def test_after_a_switch_the_old_live_account_is_still_renewed_for_its_fixed_sessions(
    w: Broker,
) -> None:
    store.switch(w.root, "extra", w.port, now_s=NOW_S)
    later = NOW_S + 4 * 3600  # main has 3.5 h left, extra 3 h: both due (in alias order)
    w.client.queue_json(200, token_answer("extra-at-2", "extra-rt-2"))
    w.client.queue_json(200, token_answer("main-at-2", "main-rt-2"))

    w.run(later)

    assert oauth_member(w.dir_port("main").blob)["accessToken"] == "main-at-2"
    assert oauth_member(w.port.blob)["accessToken"] == "extra-at-2"
    assert oauth_member(w.dir_port("extra").blob)["accessToken"] == "extra-at-2"


def test_the_tick_renews_mirrors_while_auto_switch_is_off(tmp_home: Path) -> None:
    w = Broker(tmp_home, main_left_h=3)
    w.client.queue_json(200, token_answer("main-at-2", "main-rt-2"))

    result = w.tick()

    assert result.line.startswith("stay: ") and "refreshed main" in result.line
    assert oauth_member(w.port.blob)["accessToken"] == "main-at-2"


def test_no_renewal_wait_for_a_mirror_claude_code_cannot_renew(w: Broker) -> None:
    w.port.blob = broker.mirror_blob(make_credential(expires_at_ms=NOW_MS + 60_000), None)

    assert auto.renewal_wait(w.port, "main", NOW_S) is None


def test_polls_read_the_active_account_from_the_store_with_the_broker_on(w: Broker) -> None:
    w.client.queue(HTTPError(503, b"{}"))
    w.client.queue(HTTPError(503, b"{}"))

    envelope.collect(w.root, client=w.client, port=w.port, now_s=NOW_S, port_for=w.port_for)

    tokens = {r.headers.get("Authorization") for r in w.client.requests}
    assert "Bearer main-at-1" in tokens


# --- turning it on and off --------------------------------------------------------------------


def test_on_takes_the_live_login_into_the_store_and_leaves_a_mirror(tmp_home: Path) -> None:
    w = Broker(tmp_home, on=False)
    renewed = make_credential(
        access="main-cc-at", refresh="main-cc-rt", expires_at_ms=NOW_MS + 6 * HOUR_MS
    )
    w.port.blob = credential_to_blob(renewed, {"mcpOAuth": {"kept": True}})  # Claude Code's login

    broker.turn_on(
        w.root, client=w.client, default_port=w.port, port_for=w.port_for, now_s=NOW_S, home=w.home
    )

    assert w.stored("main").refresh_token.reveal() == "main-cc-rt"
    member = oauth_member(w.port.blob)
    assert member["accessToken"] == "main-cc-at" and "refreshToken" not in member
    assert json.loads(w.port.blob or "")["mcpOAuth"] == {"kept": True}
    assert broker.is_on(w.root)
    assert oauth_member(w.dir_port("extra").blob)["accessToken"] == "extra-at-1"
    assert w.client.requests == []


def test_on_waits_while_the_live_login_may_be_being_renewed(tmp_home: Path) -> None:
    w = Broker(tmp_home, on=False)
    w.port.blob = credential_to_blob(
        make_credential(access="main-cc-at", refresh="main-cc-rt", expires_at_ms=NOW_MS + 60_000)
    )

    with pytest.raises(VibeError, match="renews within"):
        broker.turn_on(
            w.root,
            client=w.client,
            default_port=w.port,
            port_for=w.port_for,
            now_s=NOW_S,
            home=w.home,
        )

    assert not broker.is_on(w.root)
    assert w.stored("main").refresh_token.reveal() == "main-rt-1"


def test_off_gives_the_live_login_back_and_removes_the_dir_mirrors(w: Broker) -> None:
    w.run()

    said = broker.turn_off(w.root, default_port=w.port, port_for=w.port_for, home=w.home)

    assert oauth_member(w.port.blob)["refreshToken"] == "main-rt-1"
    assert w.dir_port("main").blob is None and w.dir_port("extra").blob is None
    assert not broker.is_on(w.root)
    assert said[0] == "gave main's login back to Claude Code's default login"


# --- vibe run <alias> and the status line -----------------------------------------------------


@pytest.fixture
def run_world(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    for name in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "VIBE_SESSION",
    ):
        monkeypatch.delenv(name, raising=False)
    world = World(tmp_home, monkeypatch)
    seed_account(
        world.root,
        "main",
        identity=MAIN,
        credential=make_credential(access="main-at-1", refresh="main-rt-1"),
        active=True,
    )
    seed_account(
        world.root,
        "extra",
        identity=EXTRA,
        credential=make_credential(access="extra-at-1", refresh="extra-rt-1"),
    )
    (tmp_home / ".claude").mkdir()
    (tmp_home / ".claude" / "CLAUDE.md").write_text("shared")
    (tmp_home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "umain"}}))
    world.port.blob = broker.mirror_blob(store.read_account(world.root, "main").credential, None)
    broker.write_settings(world.root, broker.BrokerSettings(on=True))
    return world


@pytest.mark.parametrize("alias", ["main", "extra"])
def test_vibe_run_alias_starts_in_its_mirror_dir_with_no_token_in_the_environment(
    run_world: World, monkeypatch: pytest.MonkeyPatch, alias: str
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "INHERITED-TOKEN-0000")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")

    assert run_world.main("run", alias, "--", "claude", "-p", "hi") == 0

    (call,) = run_world.claude_calls(full=True)
    seen = call["env"]
    assert isinstance(seen, dict)
    directory = run_world.home / f".claude-{alias}"
    assert seen["CLAUDE_CONFIG_DIR"] == str(directory)
    assert seen["VIBE_SESSION"] == alias
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen
    blob = json.dumps(seen)
    assert f"{alias}-at-1" not in blob and "INHERITED" not in blob
    member = oauth_member(run_world.port_for(directory).blob)
    assert member["accessToken"] == f"{alias}-at-1" and "refreshToken" not in member
    assert (directory / "CLAUDE.md").is_symlink()
    assert run_world.client.requests == []


def test_a_dry_run_writes_no_mirror(run_world: World) -> None:
    assert run_world.main("run", "--dry-run", "extra", "--", "claude") == 0

    assert not (run_world.home / ".claude-extra").exists()


def test_a_run_on_a_dead_login_starts_nothing(run_world: World) -> None:
    account = store.read_account(run_world.root, "extra")
    store.write_account(
        run_world.root,
        replace(
            account,
            credential=replace(account.credential, expires_at_ms=NOW_MS - 1),
            state=AccountState.NEEDS_LOGIN,
            message="the refresh token was rejected",
        ),
    )

    assert run_world.main("run", "extra", "--", "claude", "-p", "hi") != 0
    assert run_world.claude_calls() == []


@pytest.mark.parametrize("marked", [True, False])
def test_the_status_line_says_fixed_for_a_session_in_a_mirror_dir(
    tmp_home: Path, marked: bool
) -> None:
    w = Broker(tmp_home)
    environ = {"CLAUDE_CONFIG_DIR": str(tmp_home / ".claude-main")}
    if marked:
        environ["VIBE_SESSION"] = "main"  # what `vibe run main` sets

    session = statusline.session_account(environ, root=w.root, home=tmp_home)
    line = statusline.render({"live": "extra", "accounts": []}, now_s=NOW_S, session=session)

    assert session == ("main", True)
    assert line.startswith("▶ main (fixed)")


def test_a_plain_session_on_the_default_login_follows_the_switch(tmp_home: Path) -> None:
    w = Broker(tmp_home)

    session = statusline.session_account({}, root=w.root, home=tmp_home)

    assert session == (None, False)
    assert statusline.render({"live": "extra"}, now_s=NOW_S, session=session).startswith("▶ extra")


def test_an_older_login_of_the_same_account_in_a_mirror_dir_is_replaced_by_the_mirror(
    w: Broker,
) -> None:
    (w.home / ".claude-extra" / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "uextra"}})
    )
    older = make_credential(
        access="extra-old-at", refresh="extra-old-rt", expires_at_ms=NOW_MS + HOUR_MS
    )
    w.dir_port("extra").blob = credential_to_blob(older)

    report = w.run()

    member = oauth_member(w.dir_port("extra").blob)
    assert member["accessToken"] == "extra-at-1" and "refreshToken" not in member
    assert w.stored("extra").refresh_token.reveal() == "extra-rt-1"
    assert report.problems == ()


def test_a_login_claude_code_may_be_renewing_right_now_is_not_taken_over_yet(w: Broker) -> None:
    (w.home / ".claude-extra" / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "uextra"}})
    )
    ending = make_credential(
        access="extra-cc-at", refresh="extra-cc-rt", expires_at_ms=NOW_MS + 4 * 60 * 1000
    )
    w.dir_port("extra").blob = credential_to_blob(ending)

    w.run()

    assert w.stored("extra").refresh_token.reveal() == "extra-rt-1"
    assert w.dir_port("extra").blob == credential_to_blob(ending)  # not written over either


def test_vibe_broker_mirror_creates_the_dir_and_links_what_claude_shares(
    run_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_world.main("broker", "mirror", "main") == 0

    directory = run_world.home / ".claude-main"
    assert (directory / "CLAUDE.md").is_symlink()
    assert oct(directory.stat().st_mode & 0o777) == "0o700"
    assert "refreshToken" not in oauth_member(run_world.port_for(directory).blob)
    assert "main mirrors into" in capsys.readouterr().out


def test_vibe_broker_mirror_refuses_the_default_dir_and_a_dir_another_account_has(
    run_world: World,
) -> None:
    assert run_world.main("broker", "mirror", "main", str(run_world.home / ".claude")) != 0
    assert run_world.main("broker", "mirror", "extra", str(run_world.home / ".claude-main")) != 0
    assert broker.read_settings(run_world.root).mirrors == {}


def test_a_tick_switch_with_the_broker_on_moves_the_default_mirror_and_keeps_both_lineages(
    tmp_home: Path,
) -> None:
    from tests.test_auto import NOW_S as AUTO_NOW_S
    from tests.test_auto import world as auto_world

    w = auto_world(tmp_home, {"a": (99, 10), "b": (10, 10)}, live="a")  # a spent: switch to b
    w.port.blob = broker.mirror_blob(
        store.read_account(w.root, "a").credential, {"mcpOAuth": {"kept": True}}
    )
    broker.write_settings(w.root, broker.BrokerSettings(on=True))
    (tmp_home / ".claude-a").mkdir()
    dirs: dict[str, FakeKeychain] = {}

    result = auto.tick(
        w.root,
        client=w.client,
        port=w.port,
        now_s=AUTO_NOW_S,
        notify=lambda _: None,
        port_for=lambda d: dirs.setdefault(str(d), FakeKeychain()),
        home=tmp_home,
    )

    assert result.switched, result.line
    member = oauth_member(w.port.blob)
    assert member["accessToken"] == "b-file" and "refreshToken" not in member
    assert json.loads(w.port.blob or "")["mcpOAuth"] == {"kept": True}
    assert oauth_member(dirs[str(tmp_home / ".claude-a")].blob)["accessToken"] == "a-file"
    assert store.read_account(w.root, "a").credential.refresh_token.reveal() == "a-rt"
    assert store.read_account(w.root, "b").credential.refresh_token.reveal() == "b-rt"
    assert [r for r in w.client.requests if r.url == oauth.TOKEN_URL] == []
