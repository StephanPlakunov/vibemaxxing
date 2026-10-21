"""`vibe switch` made safe to run with nobody watching.

One test per defect: a login vibe does not know is never overwritten (D1), a dead
account is never switched onto (D2), the live account is read from Claude Code's
own identity rather than the `active` pointer (D3), and a dead refresh token is
posted once, not once per poll (D4).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient, FakeKeychain, make_credential, seed_account
from vibemaxxing import envelope, oauth, store
from vibemaxxing.credentials import Identity, credential_to_blob
from vibemaxxing.errors import NeedsLoginError, UsageError
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
