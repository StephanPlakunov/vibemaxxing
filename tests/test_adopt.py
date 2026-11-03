"""`vibe add ALIAS --from-config-dir DIR --adopt-stored-login`: the stored login goes to a dir.

For an account nobody can log in to again. vibe's stored copy becomes Claude Code's own login for
the dir (its Keychain item and the oauthAccount of DIR/.claude.json), and vibe stops refreshing it
before Claude Code can. Fakes only: a fake Keychain per dir, a fake HTTP client, no real login.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from tests.fakes import FakeKeychain, make_credential, seed_account
from tests.test_run_cursor import NOW_S, World
from vibemaxxing import oauth, store
from vibemaxxing.credentials import (
    Credential,
    Identity,
    credential_to_blob,
    credential_to_disk,
    parse_blob,
)
from vibemaxxing.errors import VibeError
from vibemaxxing.httpclient import HTTPError
from vibemaxxing.models import AccountState

NOW_MS = int(NOW_S * 1000)
FUTURE_MS = NOW_MS + 4 * 3600 * 1000
EXTRA = Identity(
    "claude2@intra-ai.de", "uextra", "Intra AI", "org-intra", "team_tier_1", "stripe", "Claude2"
)
STORED = dict(access="stored-at-0000", refresh="stored-rt-0000")
SECRETS = ("stored-at-0000", "stored-rt-0000", "new-at-1111", "new-rt-1111", "stash-rt-2222")


@dataclass
class WatchedKeychain(FakeKeychain):
    """Records what the account file said at the instant the item was written."""

    root: Path | None = None
    owned_at_write: list[str | None] = field(default_factory=list)
    fail_write: bool = False
    land_then_fail: bool = False
    garble: bool = False

    def write(self, blob: str) -> None:
        assert self.root is not None
        self.owned_at_write.append(store.read_account(self.root, "extra").config_dir)
        if self.fail_write:
            raise VibeError("the macOS Keychain refused to write it", "vibe list")
        super().write("{}" if self.garble else blob)
        if self.land_then_fail:
            raise VibeError("the macOS Keychain answered late", "vibe list")


def stored_credential(*, expires_ms: int = FUTURE_MS, **tokens: str) -> Credential:
    base = {**STORED, **tokens}
    return make_credential(
        access=base["access"],
        refresh=base["refresh"],
        expires_at_ms=expires_ms,
        subscription_type="team",
    )


@pytest.fixture
def world(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    world = World(tmp_home, monkeypatch)
    world.add_cursor()
    world.cursor_usage()
    return world


def setup(world: World, *, credential: Credential | None = None, **account: object) -> Path:
    """extra, vibe's own, with a stored login; ~/.claude-extra exists with its onboarding state."""
    directory = world.home / ".claude-extra"
    directory.mkdir(exist_ok=True)
    (directory / ".claude.json").write_text(
        json.dumps({"firstStartTime": "2026-11-03", "mcpServers": {"vibe-bridge": {"x": 1}}})
    )
    seed_account(
        world.root,
        "extra",
        identity=EXTRA,
        credential=credential or stored_credential(),
        **account,  # type: ignore[arg-type]
    )
    world.dirs[str(directory)] = WatchedKeychain(root=world.root)
    return directory


def dir_port(world: World, directory: Path) -> WatchedKeychain:
    port = world.dirs[str(directory)]
    assert isinstance(port, WatchedKeychain)
    return port


def adopt(world: World, directory: Path) -> int:
    return world.main("add", "extra", "--from-config-dir", str(directory), "--adopt-stored-login")


def token_posts(world: World) -> int:
    return len([r for r in world.client.requests if r.url == oauth.TOKEN_URL])


def nothing_handed_over(world: World, directory: Path) -> None:
    assert dir_port(world, directory).blob is None
    assert store.read_account(world.root, "extra").config_dir is None
    json.loads((directory / ".claude.json").read_text())  # still the file Claude Code wrote


# --- the hand-over --------------------------------------------------------------------------------


def test_the_stored_login_becomes_claude_codes_login_for_the_dir(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = setup(world)
    stored = store.read_account(world.root, "extra").credential

    assert adopt(world, directory) == 0

    port = dir_port(world, directory)
    assert port.blob is not None
    blob = json.loads(port.blob)
    # Claude Code's own shape (read from the real item, names only): one claudeAiOauth object
    assert set(blob) == {"claudeAiOauth"}
    assert set(blob["claudeAiOauth"]) == set(credential_to_disk(stored))
    assert {"accessToken", "refreshToken", "expiresAt", "scopes", "subscriptionType"} <= set(
        blob["claudeAiOauth"]
    )
    got = parse_blob(port.blob)
    assert got == stored  # access, refresh, expiry, scopes, plan: every member as stored
    profile = json.loads((directory / ".claude.json").read_text())
    assert profile["oauthAccount"]["accountUuid"] == "uextra"
    assert profile["oauthAccount"]["emailAddress"] == "claude2@intra-ai.de"
    assert profile["oauthAccount"]["organizationUuid"] == "org-intra"
    assert "profileFetchedAt" not in profile["oauthAccount"]  # Claude Code refetches the rest
    assert profile["mcpServers"] == {"vibe-bridge": {"x": 1}}  # what was there stays
    assert profile["firstStartTime"] == "2026-11-03"
    account = store.read_account(world.root, "extra")
    assert account.config_dir == str(directory) and account.credential == stored
    out = capsys.readouterr()
    assert not any(secret in out.out + out.err for secret in SECRETS)
    assert world.client.requests == []  # a token that still works is not spent on a refresh
    assert "keychain.write" not in world.port.log  # the default login was never touched
    assert (directory / ".claude.json").stat().st_mode & 0o777 == 0o600


def test_vibe_stops_refreshing_before_claude_code_has_the_login(world: World) -> None:
    directory = setup(world)

    assert adopt(world, directory) == 0

    # At the instant the item was written, the account file already said it was the dir's.
    assert dir_port(world, directory).owned_at_write == [str(directory)]


def test_a_refresh_that_read_the_account_before_the_hand_over_cannot_post_after_it(
    world: World,
) -> None:
    directory = setup(world, credential=stored_credential(expires_ms=1_000))
    before = store.read_account(world.root, "extra").credential  # what a tick read at 08:00
    world.client.queue_json(
        200, {"access_token": "new-at-1111", "refresh_token": "new-rt-1111", "expires_in": 28800}
    )
    assert adopt(world, directory) == 0
    posts_for_adoption = token_posts(world)

    late = oauth.refresh(world.root, "extra", before, world.client, now_ms=NOW_MS)

    assert late.error == "owned" and late.credential is None
    assert token_posts(world) == posts_for_adoption  # the late tick sent nothing


def test_a_refresh_that_passed_the_first_check_sees_the_mark_under_the_claim(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window between oauth.refresh's first look and its claim: the hand-over lands in it."""
    directory = setup(world, credential=stored_credential(expires_ms=1_000))
    before = store.read_account(world.root, "extra").credential
    claim = store.claim_refresh

    def claim_then_lose_the_race(root: Path, alias: str, *, now_s: float) -> bool:
        won = claim(root, alias, now_s=now_s)
        # the adoption took and released the claim first, and marked the account meanwhile
        store.write_account(
            root, replace(store.read_account(root, alias), config_dir=str(directory))
        )
        return won

    monkeypatch.setattr(store, "claim_refresh", claim_then_lose_the_race)

    outcome = oauth.refresh(world.root, "extra", before, world.client, now_ms=NOW_MS)

    assert outcome.error == "owned" and world.client.requests == []
    assert not store._claim_path(world.root, "extra").exists()  # and the claim is let go


def test_a_claim_held_by_a_refresh_in_progress_stops_the_hand_over_with_nothing_written(
    world: World,
) -> None:
    directory = setup(world)
    assert store.claim_refresh(
        world.root, "extra", now_s=time.time()
    )  # the claim helper uses the real clock

    assert adopt(world, directory) != 0

    nothing_handed_over(world, directory)


def test_after_it_a_run_uses_the_dir_and_vibe_refreshes_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = setup(world, credential=stored_credential(expires_ms=1_000))
    world.client.queue_json(
        200, {"access_token": "new-at-1111", "refresh_token": "new-rt-1111", "expires_in": 28800}
    )
    assert adopt(world, directory) == 0
    posts = token_posts(world)

    assert world.main("run", "extra", "--", "claude", "-p", "hi") == 0

    (call,) = world.claude_calls(full=True)
    env = call["env"]
    assert isinstance(env, dict)
    assert env["CLAUDE_CONFIG_DIR"] == str(directory) and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert token_posts(world) == posts


# --- an access token that has ended, a stash ------------------------------------------------------


def test_an_ended_access_token_is_renewed_once_first_and_claude_code_gets_the_new_one(
    world: World,
) -> None:
    directory = setup(world, credential=stored_credential(expires_ms=1_000))
    world.client.queue_json(
        200, {"access_token": "new-at-1111", "refresh_token": "new-rt-1111", "expires_in": 28800}
    )

    assert adopt(world, directory) == 0

    assert token_posts(world) == 1
    port = dir_port(world, directory)
    assert port.blob is not None
    handed = parse_blob(port.blob)
    assert handed.refresh_token.reveal() == "new-rt-1111"  # the newest, not the spent one
    assert handed.access_token.reveal() == "new-at-1111" and handed.expires_at_ms > NOW_MS
    assert store.read_account(world.root, "extra").credential == handed
    assert store.read_stash(world.root, "extra") is None


def test_a_dead_lineage_is_found_before_anything_is_handed_over(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = setup(world, credential=stored_credential(expires_ms=1_000))
    world.client.queue(HTTPError(400, b'{"error": "invalid_grant"}'))

    assert adopt(world, directory) != 0

    nothing_handed_over(world, directory)
    assert "nothing was handed over" in capsys.readouterr().err


def test_a_refresh_that_could_not_be_made_hands_nothing_over(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = setup(world, credential=stored_credential(expires_ms=1_000))
    world.client.queue(HTTPError(503, b"{}"))

    assert adopt(world, directory) != 0

    nothing_handed_over(world, directory)
    assert "nothing was handed over" in capsys.readouterr().err


def test_a_successor_in_the_stash_is_what_gets_handed_over(world: World) -> None:
    """An interrupted refresh: the file's token is spent; the stash holds the next."""
    directory = setup(world)
    store.write_stash(
        world.root,
        "extra",
        stored_credential(
            access="stash-at-2222", refresh="stash-rt-2222", expires_ms=FUTURE_MS + 1
        ),
    )

    assert adopt(world, directory) == 0

    port = dir_port(world, directory)
    assert port.blob is not None
    assert parse_blob(port.blob).refresh_token.reveal() == "stash-rt-2222"
    assert world.client.requests == []  # recovered from the stash: no request
    assert store.read_stash(world.root, "extra") is None


# --- refusals: nothing is written -----------------------------------------------------------------


def test_the_active_account_is_refused(world: World) -> None:
    directory = setup(world)
    store.write_active(world.root, "extra")

    assert adopt(world, directory) != 0

    assert dir_port(world, directory).blob is None
    assert store.read_account(world.root, "extra").config_dir is None


def test_the_account_claude_code_runs_as_is_refused(world: World) -> None:
    directory = setup(world)
    (world.home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "uextra"}})
    )
    world.port.blob = credential_to_blob(make_credential(access="live-at", refresh="live-rt"))

    assert adopt(world, directory) != 0

    nothing_handed_over(world, directory)


def test_a_dir_that_already_has_a_login_is_never_overwritten(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = setup(world)
    mine = credential_to_blob(make_credential(access="theirs-at", refresh="theirs-rt"))
    dir_port(world, directory).blob = mine

    assert adopt(world, directory) != 0

    assert dir_port(world, directory).blob == mine
    assert store.read_account(world.root, "extra").config_dir is None
    assert f"vibe add extra --from-config-dir {directory}" in capsys.readouterr().err


def test_a_dir_whose_profile_names_another_account_is_refused(world: World) -> None:
    directory = setup(world)
    (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uother"}}))

    assert adopt(world, directory) != 0

    assert dir_port(world, directory).blob is None
    assert json.loads((directory / ".claude.json").read_text())["oauthAccount"] == {
        "accountUuid": "uother"
    }


@pytest.mark.parametrize("state", ["needs_login", "lapsed", "no_uuid"])
def test_a_stored_login_that_cannot_be_handed_over_is_refused(world: World, state: str) -> None:
    directory = setup(world)
    account = store.read_account(world.root, "extra")
    if state == "needs_login":
        account = replace(account, state=AccountState.NEEDS_LOGIN)
    elif state == "lapsed":
        credential = make_credential(refresh_expires_at_ms=1_000, **{"access": "a", "refresh": "r"})
        account = replace(account, credential=credential)
    else:
        account = replace(account, identity=Identity(*([None] * 7)))
    store.write_account(world.root, account)

    assert adopt(world, directory) != 0

    assert dir_port(world, directory).blob is None
    assert store.read_account(world.root, "extra").config_dir is None


def test_an_account_that_lives_in_another_dir_is_refused(world: World) -> None:
    directory = setup(world, config_dir="/somewhere/.claude-else")

    assert adopt(world, directory) != 0

    assert dir_port(world, directory).blob is None
    assert store.read_account(world.root, "extra").config_dir == "/somewhere/.claude-else"


def test_it_needs_an_account_and_a_dir_and_a_claude_account(world: World) -> None:
    setup(world)

    assert world.main("add", "--from-config-dir", "/x", "--adopt-stored-login") != 0
    assert world.main("add", "extra", "--adopt-stored-login") != 0
    assert (
        world.main(
            "add", "cursor", "--kind", "cursor", "--from-config-dir", "/x", "--adopt-stored-login"
        )
        != 0
    )
    assert world.main("add", "nobody", "--from-config-dir", "/x", "--adopt-stored-login") != 0


# --- a failure half way ---------------------------------------------------------------------------


def test_a_refused_keychain_write_gives_the_lineage_back_to_vibe(world: World) -> None:
    directory = setup(world)
    dir_port(world, directory).fail_write = True

    assert adopt(world, directory) != 0

    # The item is not there, so nobody else holds the lineage: vibe may refresh it again.
    assert dir_port(world, directory).blob is None
    assert store.read_account(world.root, "extra").config_dir is None


def test_a_write_that_may_have_landed_keeps_the_mark_and_a_rerun_finishes_it(
    world: World,
) -> None:
    directory = setup(world)
    port = dir_port(world, directory)
    port.land_then_fail = True

    assert adopt(world, directory) != 0

    assert port.blob is not None  # it did land
    assert store.read_account(world.root, "extra").config_dir == str(
        directory
    )  # a second holder is worse
    # vibe never refreshes it meanwhile
    assert (
        oauth.refresh(
            world.root,
            "extra",
            store.read_account(world.root, "extra").credential,
            world.client,
            now_ms=NOW_MS,
        ).error
        == "owned"
    )
    # a rerun sees a login in the dir and says so, without touching it
    land = port.blob
    assert adopt(world, directory) != 0 and port.blob == land


def test_a_garbled_read_back_is_a_failure_and_keeps_the_mark(world: World) -> None:
    directory = setup(world)
    dir_port(world, directory).garble = True

    assert adopt(world, directory) != 0

    assert store.read_account(world.root, "extra").config_dir == str(directory)


def test_a_crash_after_the_mark_is_finished_by_running_it_again(world: World) -> None:
    """Marked as the dir's, item never written: `vibe run extra` says no login; this fixes it."""
    directory = setup(world, config_dir=None)
    account = store.read_account(world.root, "extra")
    store.write_account(world.root, replace(account, config_dir=str(directory)))

    assert adopt(world, directory) == 0

    port = dir_port(world, directory)
    assert port.blob is not None
    assert parse_blob(port.blob) == account.credential


def test_a_corrupt_profile_stops_before_the_login_is_written(world: World) -> None:
    directory = setup(world)
    (directory / ".claude.json").write_text("{ not json")

    assert adopt(world, directory) != 0

    assert dir_port(world, directory).blob is None
    assert store.read_account(world.root, "extra").config_dir is None
    assert (directory / ".claude.json").read_text() == "{ not json"  # Claude Code's file, as found


# --- until it is adopted: what a poll may refresh -------------------------------------------------


def vibe_owned(world: World, *, ends_in_s: int) -> None:
    world.claude_account("extra", session=5, weekly=5)  # a fresh usage cache: polls ask nothing
    account = store.read_account(world.root, "extra")
    credential = make_credential(
        access="extra-at-0", refresh="extra-rt-0", expires_at_ms=NOW_MS + ends_in_s * 1000
    )
    store.write_account(world.root, replace(account, credential=credential))


def test_a_poll_leaves_a_token_alone_that_still_works(world: World) -> None:
    """A session started with it may be using it: the poll refreshes only once it has ended."""
    vibe_owned(world, ends_in_s=180)  # inside the 5-minute window, still valid
    world.claude_account("main", session=5, weekly=5, active=True)

    assert world.main("list") == 0

    assert token_posts(world) == 0
    assert store.read_account(world.root, "extra").credential.access_token.reveal() == "extra-at-0"


def test_a_poll_refreshes_a_token_that_has_ended(world: World) -> None:
    vibe_owned(world, ends_in_s=-60)
    world.claude_account("main", session=5, weekly=5, active=True)
    world.client.queue_json(
        200, {"access_token": "new-at-1111", "refresh_token": "new-rt-1111", "expires_in": 28800}
    )

    assert world.main("list") == 0

    assert token_posts(world) == 1


def test_a_run_still_refreshes_inside_the_window_because_its_child_cannot(world: World) -> None:
    vibe_owned(world, ends_in_s=180)
    world.client.queue_json(
        200, {"access_token": "new-at-1111", "refresh_token": "new-rt-1111", "expires_in": 28800}
    )

    assert world.main("run", "extra", "--", "claude", "-p", "hi") == 0

    assert token_posts(world) == 1
    (call,) = world.claude_calls(full=True)
    env = call["env"]
    assert isinstance(env, dict) and env["CLAUDE_CODE_OAUTH_TOKEN"] == "new-at-1111"
