"""The tick waits before switching away while the live login is about to be renewed.

After a switch a running Claude Code session may hold the outgoing login for up to 30 s (its
Keychain cache). If that login's access token is near its end, the session renews it and writes
the outgoing account back over the incoming one, and the refresh token vibe just stored for the
outgoing account is spent. So a switch away waits while the live access token ends within
RENEWAL_GUARD_S, unless it ended more than RENEWAL_IDLE_S ago (no session uses it then). The
expiry is read from the port, never from the account file, and nothing is refreshed for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.fakes import FakeKeychain, make_credential
from tests.test_auto import NOW_S, World, payload, world
from tests.test_policy import ON
from vibemaxxing import auto, credentials, store
from vibemaxxing.credentials import credential_to_blob
from vibemaxxing.errors import VibeError

MINUTE = 60.0


def live_expiring(w: World, in_s: float, *, alias: str = "a") -> None:
    """The port holds a's login as Claude Code last renewed it, ending ``in_s`` from now."""
    w.port.blob = credential_to_blob(
        make_credential(
            access=f"{alias}-renewed-at",
            refresh=f"{alias}-renewed-rt",
            expires_at_ms=int((NOW_S + in_s) * 1000),
        ),
        base={"mcpOAuth": {"kept": True}},
    )


def spent_a(tmp_home: Path) -> World:
    # a is spent, b has room: the default rule wants to switch a -> b.
    return world(tmp_home, {"a": (99, 10), "b": (10, 10)}, live="a")


def test_the_guard_times_are_constants() -> None:
    assert (auto.RENEWAL_GUARD_S, auto.RENEWAL_IDLE_S) == (600, 900)


@pytest.mark.parametrize(
    ("in_s", "line"),
    [
        (5 * MINUTE, "stay: waiting: a's login renews within 10 min"),
        (10 * MINUTE, "stay: waiting: a's login renews within 10 min"),
        (0.0, "stay: waiting: a's login renews within 10 min"),
        (-5 * MINUTE, "stay: waiting: a's login ended 5 min ago and a session may be renewing it"),
        (
            -15 * MINUTE,
            "stay: waiting: a's login ended 15 min ago and a session may be renewing it",
        ),
    ],
)
def test_a_live_login_about_to_be_renewed_holds_the_switch(
    tmp_home: Path, in_s: float, line: str
) -> None:
    w = spent_a(tmp_home)
    live_expiring(w, in_s)
    blob = w.port.blob
    files = {alias: store.account_path(w.root, alias).read_text() for alias in ("a", "b")}

    result = w.tick()

    assert (result.line, result.switched) == (line, False)
    assert w.port.blob == blob and "keychain.write" not in w.port.log
    assert store.read_active(w.root) == "a" and auto.read_last(w.root) is None
    assert {alias: store.account_path(w.root, alias).read_text() for alias in ("a", "b")} == files
    assert w.client.requests == [] and w.notes == []  # nothing refreshed, for a or for b


@pytest.mark.parametrize("in_s", [30 * MINUTE, 10 * MINUTE + 1, -20 * MINUTE, -15 * MINUTE - 1])
def test_a_login_with_time_left_or_long_ended_switches(tmp_home: Path, in_s: float) -> None:
    w = spent_a(tmp_home)
    live_expiring(w, in_s)

    result = w.tick()

    assert result.switched and result.line.startswith("switched a -> b: ")
    assert store.read_active(w.root) == "b"


def test_the_expiry_is_the_ports_not_the_files(tmp_home: Path) -> None:
    """The file's copy of a says it ends in two minutes; Claude Code has renewed it since."""
    w = spent_a(tmp_home)
    stale = store.read_account(w.root, "a")
    store.write_account(
        w.root,
        store.Account(
            "a",
            make_credential(
                access="a-file", refresh="a-rt", expires_at_ms=int((NOW_S + 120) * 1000)
            ),
            stale.identity,
            stale.state,
            None,
            stale.added_at,
        ),
    )
    live_expiring(w, 3 * 3600)

    assert w.tick().switched

    # store.switch read the outgoing login back from the port into a's file: the renewed one,
    # which is what a holds now. The guard is what makes that copy the last one: no session
    # renews a in the 30 s it may still hold it, so this refresh token is not spent behind it.
    back = store.read_account(w.root, "a").credential
    assert (back.access_token.reveal(), back.refresh_token.reveal()) == (
        "a-renewed-at",
        "a-renewed-rt",
    )
    assert w.port.blob is not None
    assert credentials.parse_blob(w.port.blob).access_token.reveal() == "b-file"


def test_a_file_with_time_left_does_not_hide_a_port_about_to_renew(tmp_home: Path) -> None:
    w = spent_a(tmp_home)  # a's file ends in 2100
    live_expiring(w, 4 * MINUTE)

    assert w.tick().line == "stay: waiting: a's login renews within 10 min"


def test_once_the_renewal_landed_the_next_tick_switches(tmp_home: Path) -> None:
    w = spent_a(tmp_home)
    live_expiring(w, 4 * MINUTE)
    assert not w.tick().switched

    live_expiring(w, 8 * 3600)  # Claude Code renewed it in the Keychain
    w.client.queue_json(200, payload(99, 10, now=NOW_S + MINUTE))  # a is polled every minute

    assert w.tick(NOW_S + MINUTE).switched
    assert store.read_account(w.root, "a").credential.refresh_token.reveal() == "a-renewed-rt"


@dataclass
class RefusingKeychain(FakeKeychain):
    def read(self) -> str | None:
        raise VibeError("the macOS Keychain refused to read it")


@pytest.mark.parametrize(
    "port",
    [RefusingKeychain(), FakeKeychain(blob=None), FakeKeychain(blob='{"claudeAiOauth": {}}')],
)
def test_a_login_that_cannot_be_read_for_the_check_is_a_wait(port: FakeKeychain) -> None:
    # It could be read a moment earlier (the tick knew who is live); not knowing is no licence.
    assert auto.renewal_wait(port, "a", NOW_S) == (
        "waiting: could not read a's login to see whether it is being renewed"
    )


def test_a_stay_for_another_reason_never_reads_the_guard(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (10, 10), "b": (10, 10)}, live="a")
    live_expiring(w, 2 * MINUTE)

    assert w.tick().line == "stay: a is below 99%"


def test_the_ordered_policy_waits_too_even_when_the_live_account_is_spent(
    tmp_home: Path,
) -> None:
    w = world(tmp_home, {"main": (99, 10), "extra": (10, 10)}, live="main")
    auto.write_settings(w.root, ON)
    live_expiring(w, 3 * MINUTE, alias="main")

    assert w.tick().line == "stay: waiting: main's login renews within 10 min"
