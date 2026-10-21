"""`vibe switch` made safe to run with nobody watching.

One test per defect: a login vibe does not know is never overwritten (D1), a dead
account is never switched onto (D2), the live account is read from Claude Code's
own identity rather than the `active` pointer (D3), and a dead refresh token is
posted once, not once per poll (D4).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient, FakeKeychain, fixture_usage, make_credential, seed_account
from vibemaxxing import cli, credentials, envelope, oauth, store
from vibemaxxing.credentials import Identity, credential_to_blob
from vibemaxxing.errors import NeedsLoginError, UsageError, VibeError
from vibemaxxing.httpclient import HTTPError
from vibemaxxing.models import AccountState

NOW_S = 1_757_930_000.0
NOW_MS = int(NOW_S * 1000)


def _identity(uuid: str) -> Identity:
    return Identity(None, uuid, None, None, None, None, None)


def _claude_json(home: Path, uuid: str) -> None:
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": uuid}}))


def test_switch_refuses_unknown_live_login(tmp_home: Path) -> None:
    # Someone ran `claude /login` as an account vibe has never stored. Switching
    # would write the port over it, and that refresh token exists nowhere else.
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "a", identity=_identity("ua"), active=True)
    seed_account(root, "b", identity=_identity("ub"))
    _claude_json(tmp_home, "stranger")
    stranger = credential_to_blob(make_credential(access="stranger-access", refresh="stranger-rt"))
    port = FakeKeychain(blob=stranger)
    a_before = store.account_path(root, "a").read_bytes()

    with pytest.raises(UsageError) as excinfo:
        store.switch(root, "b", port)

    assert excinfo.value.recovery == "vibe add"
    assert port.blob == stranger
    assert port.log == ["keychain.read"], "the guard may read the port and must never write it"
    assert store.read_active(root) == "a"
    assert store.account_path(root, "a").read_bytes() == a_before, (
        "the stranger's tokens must not land in an account that is not theirs"
    )
    assert store.claim_refresh(root, "a", now_s=NOW_S), "a refused switch releases its claims"
    assert store.claim_refresh(root, "b", now_s=NOW_S)


def test_switch_only_refuses_a_login_it_can_prove_is_unknown(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "a", active=True)  # stored without a uuid: it might be the live one
    seed_account(root, "b", identity=_identity("ub"))
    _claude_json(tmp_home, "somebody")
    live = credential_to_blob(make_credential(access="live-access", refresh="live-rt"))

    store.switch(root, "b", FakeKeychain(blob=live))

    assert store.read_active(root) == "b"
    assert store.read_account(root, "a").credential.refresh_token.reveal() == "live-rt"

    # And a blob with no token in it is not a login anyone could lose.
    seed_account(root, "c", identity=_identity("uc"), active=True)
    seed_account(root, "a", identity=_identity("ua"))
    port = FakeKeychain(blob='{"claudeAiOauth": {}, "mcpOAuth": {"kept": true}}')
    _claude_json(tmp_home, "stranger")

    store.switch(root, "b", port)

    assert port.blob is not None and json.loads(port.blob)["mcpOAuth"] == {"kept": True}


def _pointer_is_stale(tmp_home: Path) -> tuple[Path, FakeKeychain]:
    """`active` says a, but Claude Code is logged in as b and has rotated b's token."""
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "a", identity=_identity("ua"), active=True)
    seed_account(root, "b", identity=_identity("ub"), credential=make_credential(refresh="b-spent"))
    seed_account(root, "c", identity=_identity("uc"), credential=make_credential(access="c-file"))
    _claude_json(tmp_home, "ub")
    rotated = make_credential(access="b-rotated", refresh="b-rotated-rt")
    return root, FakeKeychain(blob=credential_to_blob(rotated))


def test_switch_saves_the_rotation_of_the_account_that_is_really_live(tmp_home: Path) -> None:
    # The old rule skipped the resync when the pointer disagreed with the live uuid and
    # wrote the port anyway, so b's only live refresh token was lost and b read
    # "login lapsed" the next morning.
    root, port = _pointer_is_stale(tmp_home)
    a_before = store.account_path(root, "a").read_bytes()

    store.switch(root, "c", port)

    assert store.read_account(root, "b").credential.refresh_token.reveal() == "b-rotated-rt"
    assert store.account_path(root, "a").read_bytes() == a_before
    assert port.blob is not None
    assert credentials.parse_blob(port.blob).access_token.reveal() == "c-file"
    assert store.read_active(root) == "c"


def test_switch_holds_the_claim_of_the_account_it_resyncs_into(tmp_home: Path) -> None:
    root, port = _pointer_is_stale(tmp_home)
    assert store.claim_refresh(root, "b", now_s=1e12)  # a refresh of b is in flight

    with pytest.raises(UsageError):
        store.switch(root, "c", port)

    assert port.log == [], "refused before the port is read, so nothing is half done"
    assert store.read_active(root) == "a"


def test_switching_to_the_live_account_repairs_a_stale_pointer_without_loss(
    tmp_home: Path,
) -> None:
    root, port = _pointer_is_stale(tmp_home)
    # b's stored copy is dead; the port's login is the live one and must win.
    stored = store.read_account(root, "b")
    store.write_account(root, replace(stored, state=AccountState.NEEDS_LOGIN, message="dead"))

    store.switch(root, "b", port)

    assert store.read_active(root) == "b"
    assert port.blob is not None
    assert credentials.parse_blob(port.blob).refresh_token.reveal() == "b-rotated-rt"
    repaired = store.read_account(root, "b")
    assert repaired.credential.refresh_token.reveal() == "b-rotated-rt"
    assert repaired.state is AccountState.OK, "the port's login is live, whatever the file said"


def test_switch_refuses_lapsed_target(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "a", identity=_identity("ua"), active=True)
    seed_account(root, "b", credential=make_credential(refresh_expires_at_ms=NOW_MS - 1))
    _claude_json(tmp_home, "ua")
    live = credential_to_blob(make_credential(access="cc-access", refresh="cc-rt"))
    port = FakeKeychain(blob=live)

    with pytest.raises(NeedsLoginError) as excinfo:
        store.switch(root, "b", port)

    assert excinfo.value.recovery == "vibe add b"
    assert port.log == [], "a dead target must be refused before the port is touched"
    assert port.blob == live
    assert store.read_active(root) == "a"


def test_live_alias_follows_claude_identity(tmp_home: Path) -> None:
    # `active` says a, but Claude Code is running as b: the pointer is only what
    # vibe last wrote, the live login is whatever is in the port and ~/.claude.json.
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "a", identity=_identity("ua"), active=True)
    seed_account(root, "b", identity=_identity("ub"))
    _claude_json(tmp_home, "ub")
    port = FakeKeychain(blob=credential_to_blob(make_credential(access="b-live", refresh="b-rt")))

    assert store.live_alias(root, port) == "b"
    assert store.read_active(root) == "a", "read_active keeps meaning what it always meant"


def test_live_alias_is_none_whenever_it_cannot_be_told(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "a", identity=_identity("shared"))
    seed_account(root, "b", identity=_identity("shared"))
    seed_account(root, "c", identity=_identity("uc"))
    blob = credential_to_blob(make_credential(access="live", refresh="live-rt"))

    assert store.live_alias(root, FakeKeychain(blob=blob)) is None, "no ~/.claude.json"
    _claude_json(tmp_home, "stranger")
    assert store.live_alias(root, FakeKeychain(blob=blob)) is None, "a login nobody stored"
    _claude_json(tmp_home, "uc")
    assert store.live_alias(root, FakeKeychain(blob=None)) is None, "no login in the port"
    assert store.live_alias(root, FakeKeychain(blob='{"claudeAiOauth": {}}')) is None
    assert store.live_alias(root, FakeKeychain(blob=blob)) == "c"

    unreadable = FakeKeychain(blob=blob)

    def refuse() -> str | None:
        raise VibeError("the macOS Keychain refused to read it", "vibe add")

    monkeypatch.setattr(unreadable, "read", refuse)
    assert store.live_alias(root, unreadable) is None, "an unreadable port is unknown, not an error"

    _claude_json(tmp_home, "shared")
    assert store.live_alias(root, FakeKeychain(blob=blob)) is None, "two aliases, neither active"
    store.write_active(root, "b")
    assert store.live_alias(root, FakeKeychain(blob=blob)) == "b", "the pointer breaks the tie"


def test_dead_refresh_token_is_posted_once(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "old", credential=make_credential(expires_at_ms=NOW_MS - 1))
    client = FakeHttpClient()
    client.queue(HTTPError(400, b'{"error":"invalid_grant"}'))

    first = envelope.collect(root, client=client, port=FakeKeychain(), now_s=NOW_S)
    second = envelope.collect(root, client=client, port=FakeKeychain(), now_s=NOW_S + 60)

    posts = [request for request in client.requests if request.url == oauth.TOKEN_URL]
    assert len(posts) == 1, "a refresh token the server called invalid_grant is never posted again"
    assert [view.state for view in first + second] == [AccountState.NEEDS_LOGIN] * 2
    assert "vibe add old" in str(second[0].message)
    stored = store.read_account(root, "old")
    assert stored.state is AccountState.NEEDS_LOGIN
    assert stored.message is not None and "vibe add old" in stored.message


def _ctx(tmp_home: Path, client: FakeHttpClient, port: FakeKeychain) -> cli.Context:
    return cli.Context(
        root=tmp_home / ".vibemaxxing",
        client=client,
        port=port,
        now_s=NOW_S,
        clock=lambda: NOW_S,
        prompt=lambda _: "",
        browser=lambda _: True,
    )


def _expired_target(tmp_home: Path) -> tuple[Path, FakeKeychain]:
    """a is live, b is stored with an access token that has expired."""
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "a", identity=_identity("ua"), active=True)
    seed_account(root, "b", credential=make_credential(expires_at_ms=NOW_MS - 1))
    _claude_json(tmp_home, "ua")
    return root, FakeKeychain(
        blob=credential_to_blob(make_credential(access="cc", refresh="cc-rt"))
    )


def test_switch_refreshes_an_expired_target_before_the_port_is_written(tmp_home: Path) -> None:
    root, port = _expired_target(tmp_home)
    client = FakeHttpClient()
    client.queue_json(
        200, {"access_token": "fresh-access-token", "refresh_token": "fresh-rt", "expires_in": 3600}
    )

    assert cli.main(["switch", "b"], context=_ctx(tmp_home, client, port)) == 0

    assert [request.url for request in client.requests] == [oauth.TOKEN_URL]
    assert port.blob is not None
    assert credentials.parse_blob(port.blob).access_token.reveal() == "fresh-access-token"
    assert store.read_active(root) == "b"


def test_switch_refuses_a_target_the_server_rejects(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, port = _expired_target(tmp_home)
    live = port.blob
    client = FakeHttpClient()
    client.queue(HTTPError(400, b'{"error":"invalid_grant"}'))

    assert cli.main(["switch", "b"], context=_ctx(tmp_home, client, port)) == 3

    assert "vibe add b" in capsys.readouterr().err
    assert port.blob == live and port.log == [], "the port is never touched for a dead target"
    assert store.read_active(root) == "a"
    assert store.read_account(root, "b").state is AccountState.NEEDS_LOGIN

    # The mark is what stops the next attempt posting the same dead token.
    assert cli.main(["switch", "b"], context=_ctx(tmp_home, client, port)) == 3
    assert len(client.requests) == 1


def test_switch_says_retry_when_it_cannot_confirm_the_target(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A 500 says nothing about the login, so `vibe add` would be the wrong advice.
    root, port = _expired_target(tmp_home)
    client = FakeHttpClient()
    client.queue(HTTPError(500, b"upstream is having a day"))

    assert cli.main(["switch", "b"], context=_ctx(tmp_home, client, port)) == 6

    assert "vibe switch b" in capsys.readouterr().err
    assert port.log == []
    assert store.read_account(root, "b").state is AccountState.OK


def test_a_stale_refresh_token_does_not_mark_a_newer_login_dead(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "old", credential=make_credential(refresh="newer-rt"))
    stale = make_credential(refresh="stale-rt", expires_at_ms=NOW_MS - 1)
    client = FakeHttpClient()
    client.queue(HTTPError(400, b'{"error":"invalid_grant"}'))

    outcome = oauth.refresh(root, "old", stale, client, now_ms=NOW_MS)

    assert outcome.error == "invalid_grant"
    assert store.read_account(root, "old").state is AccountState.OK, (
        "a `vibe add` that landed meanwhile must not be marked dead for the old token's sins"
    )


def test_a_fresh_login_clears_needs_login(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "old", credential=make_credential(expires_at_ms=NOW_MS - 1))
    dead = FakeHttpClient()
    dead.queue(HTTPError(400, b'{"error":"invalid_grant"}'))
    envelope.collect(root, client=dead, port=FakeKeychain(), now_s=NOW_S)
    assert store.read_account(root, "old").state is AccountState.NEEDS_LOGIN

    monkeypatch.setattr(oauth, "new_state", lambda: "state-nonce")
    login = FakeHttpClient()
    login.queue_json(
        200, {"access_token": "new-access-token", "refresh_token": "new-rt", "expires_in": 3600}
    )
    ctx = _ctx(tmp_home, login, FakeKeychain())
    ctx.prompt = lambda _: "the-code#state-nonce"
    assert cli.main(["add", "old"], context=ctx) == 0
    assert store.read_account(root, "old").state is AccountState.OK

    # A login made here has no plan yet, so the first poll asks the profile endpoint too.
    login.queue_json(200, {"organization": {"organization_type": "claude_max"}})
    login.queue_json(200, fixture_usage())
    (view,) = envelope.collect(root, client=login, port=FakeKeychain(), now_s=NOW_S)
    assert view.state is AccountState.OK
    assert view.summary is not None
