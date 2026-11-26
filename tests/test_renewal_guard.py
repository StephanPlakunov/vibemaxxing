"""`vibe broker on` waits while the live login is about to be renewed.

Taking the live login over writes a mirror (no refresh token) into Claude Code's default item. A
running Claude Code session renews its login in the minutes before the access token ends, and a
renewal in that window would spend the refresh token vibe just took. So the takeover waits while
the live access token ends within RENEWAL_GUARD_S, unless it ended more than RENEWAL_IDLE_S ago
(no session uses it then). The expiry is read from the port, never from the account file, and
nothing is refreshed for it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tests.fakes import FakeKeychain, make_credential
from vibemaxxing import auto, broker
from vibemaxxing.credentials import Credential, credential_to_blob
from vibemaxxing.errors import VibeError

NOW_S = 1_757_930_000.0
MINUTE = 60.0


def _login(in_s: float) -> Credential:
    return make_credential(
        access="a-renewed-at", refresh="a-renewed-rt", expires_at_ms=int((NOW_S + in_s) * 1000)
    )


def expiring(in_s: float) -> FakeKeychain:
    """A port holding a's login as Claude Code last renewed it, ending ``in_s`` from now."""
    return FakeKeychain(blob=credential_to_blob(_login(in_s), base={"mcpOAuth": {"kept": True}}))


def test_the_guard_times_are_constants() -> None:
    assert (auto.RENEWAL_GUARD_S, auto.RENEWAL_IDLE_S) == (600, 900)


@pytest.mark.parametrize(
    ("in_s", "line"),
    [
        (5 * MINUTE, "waiting: a's login renews within 10 min"),
        (10 * MINUTE, "waiting: a's login renews within 10 min"),
        (0.0, "waiting: a's login renews within 10 min"),
        (-5 * MINUTE, "waiting: a's login ended 5 min ago and a session may be renewing it"),
        (-15 * MINUTE, "waiting: a's login ended 15 min ago and a session may be renewing it"),
    ],
)
def test_a_live_login_about_to_be_renewed_is_a_wait(in_s: float, line: str) -> None:
    port = expiring(in_s)
    blob = port.blob

    assert auto.renewal_wait(port, "a", NOW_S) == line
    assert port.blob == blob and "keychain.write" not in port.log


@pytest.mark.parametrize("in_s", [30 * MINUTE, 10 * MINUTE + 1, -20 * MINUTE, -15 * MINUTE - 1])
def test_a_login_with_time_left_or_long_ended_need_not_wait(in_s: float) -> None:
    assert auto.renewal_wait(expiring(in_s), "a", NOW_S) is None


def test_a_mirror_has_nothing_to_race() -> None:
    # The broker's own mirror holds no refresh token: Claude Code can renew nothing there.
    mirror = FakeKeychain(blob=broker.mirror_blob(_login(2 * MINUTE), {}))

    assert auto.renewal_wait(mirror, "a", NOW_S) is None


@dataclass
class RefusingKeychain(FakeKeychain):
    def read(self) -> str | None:
        raise VibeError("the macOS Keychain refused to read it")


@pytest.mark.parametrize(
    "port",
    [RefusingKeychain(), FakeKeychain(blob=None), FakeKeychain(blob='{"claudeAiOauth": {}}')],
)
def test_a_login_that_cannot_be_read_for_the_check_is_a_wait(port: FakeKeychain) -> None:
    # It could be read a moment earlier; not knowing is no licence.
    assert auto.renewal_wait(port, "a", NOW_S) == (
        "waiting: could not read a's login to see whether it is being renewed"
    )
