"""What is left on the Cursor seat, read the way `cursor-agent`'s own `/usage` reads it.

`cursor-agent` has no usage command a script can run, but its interactive `/usage` calls
``aiserver.v1.DashboardService/GetCurrentPeriodUsage`` (and `GetHardLimit`) on its API
endpoint, authenticated with the access token `cursor-agent login` stored. So the same
token works for that RPC, by the agent's own code. This module sends the same two calls as
Connect unary JSON and reads the answer defensively.

The token is read at the moment of the request, held in a ``Secret``, and goes into one
header. vibe never stores it, never logs it, and none of its messages carry it.

Not verified against the live endpoint: no test ever touches it, and nobody here read the
real token. Any shape that is not what the agent's code reads comes out as "unknown".
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from vibemaxxing.errors import VibeError
from vibemaxxing.httpclient import HttpClient, HTTPError
from vibemaxxing.redact import Secret, scrub

API_BASE: Final = "https://api2.cursor.sh"
SERVICE: Final = "aiserver.v1.DashboardService"
KEYCHAIN_SERVICE: Final = "cursor-access-token"
KEYCHAIN_ACCOUNT: Final = "cursor-user"
SECURITY_BIN: Final = "/usr/bin/security"
SECURITY_TIMEOUT_S: Final = 10.0
SECURITY_ABSENT_CODE: Final = 44
# The seat's personal on-demand cap is spent to within this much, in cents.
DEFAULT_MARGIN_CENTS: Final = 1000
# A cached answer older than this is not used to route (cents move while sessions run).
FRESH_S: Final = 120.0
# Close to the cap a cached answer is trusted for this long only: dollars move while it runs.
NEAR_CAP_FRESH_S: Final = 30.0
# A cached answer older than FRESH_S is shown (with its time) up to this age, never routed by.
STALE_S: Final = 900.0


class CursorTokenPort(Protocol):
    def read(self) -> Secret | None:
        """The agent's access token, or None when there is none. Runtime only."""
        ...


class AgentTokenStore:
    """Where `cursor-agent login` keeps the access token: the macOS Keychain, else auth.json.

    The item names come from the agent's own code: service ``cursor-access-token``,
    account ``cursor-user``; elsewhere ``auth.json`` with an ``accessToken`` member. Read
    only. Nothing here is ever written, logged or put in an error message.
    """

    def read(self) -> Secret | None:
        platform: str = sys.platform
        if platform == "darwin":
            token = self._keychain()
            if token is not None:
                return token
        return self._file()

    def _keychain(self) -> Secret | None:
        try:
            done = subprocess.run(
                [
                    SECURITY_BIN,
                    "find-generic-password",
                    "-a",
                    KEYCHAIN_ACCOUNT,
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-w",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=SECURITY_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
            return None
        value = done.stdout.strip()
        return Secret(value) if done.returncode == 0 and value else None

    def _file(self) -> Secret | None:
        home = Path(os.path.expanduser("~"))
        platform: str = sys.platform
        path = (
            home / ".cursor" / "auth.json"
            if platform == "darwin"
            else Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
            / "cursor"
            / "auth.json"
        )
        try:
            parsed: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = parsed.get("accessToken") if isinstance(parsed, dict) else None
        return Secret(value) if isinstance(value, str) and value else None


# --- parsing -------------------------------------------------------------------------


@dataclass(frozen=True)
class CursorUsage:
    """What a Cursor seat has left. A field is None when the answer did not say."""

    # The included allowance plus whatever bonus Cursor adds on top, as a percent of the plan.
    included_percent: float | None
    # On-demand (usage-based) spend this cycle and the personal cap, in cents. A cap of 0 means
    # on-demand is off; None means the answer named no cap.
    on_demand_used_cents: int | None
    on_demand_limit_cents: int | None
    cycle_end_s: float | None
    plan: str | None = None


def _num(value: object) -> float | None:
    """A JSON number or the decimal string a Connect int64 arrives as; None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _cents(value: object) -> int | None:
    number = _num(value)
    return None if number is None else round(number)


def _member(raw: object, key: str) -> Mapping[str, object] | None:
    if not isinstance(raw, Mapping):
        return None
    inner: object = raw.get(key)
    return inner if isinstance(inner, Mapping) else None


def _percent(plan: Mapping[str, object] | None) -> float | None:
    if plan is None:
        return None
    given = _num(plan.get("totalPercentUsed"))
    if given is not None:
        return given
    used, limit = _num(plan.get("includedSpend", plan.get("used"))), _num(plan.get("limit"))
    return used / limit * 100 if used is not None and limit else None


def parse_usage(
    current: Mapping[str, object] | None, hard_limit: Mapping[str, object] | None
) -> CursorUsage | None:
    """The agent's `GetCurrentPeriodUsage` answer (and `GetHardLimit`), or the web API's shape.

    None when neither shape is recognised, which callers show as "unknown". The cap comes from
    ``spendLimitUsage.individualLimit`` (cents), else ``GetHardLimit`` (dollars; 0 or
    ``noUsageBasedAllowed`` is on-demand off, 2147483647 is no cap).
    """
    if current is None:
        return None
    plan = _member(current, "planUsage")
    spend = _member(current, "spendLimitUsage")
    web_plan = _member(_member(current, "individualUsage"), "plan")
    web_demand = _member(_member(current, "individualUsage"), "onDemand")
    if plan is None and spend is None and web_plan is None and web_demand is None:
        return None
    used = _cents(spend.get("individualUsed")) if spend is not None else None
    limit = _cents(spend.get("individualLimit")) if spend is not None else None
    if spend is not None and used is None:
        # The real answer omits a field that is zero (proto3 JSON): no `individualUsed` is $0
        # used, and `individualRemaining` then carries the whole cap. Derive it from that, and
        # from nothing weaker: with neither figure there is no on-demand number to trust.
        remaining = _cents(spend.get("individualRemaining"))
        if limit is not None and remaining is not None:
            used = max(0, limit - remaining)
        elif limit == 0:
            used = 0
    if web_demand is not None:
        used = _cents(web_demand.get("used")) if used is None else used
        limit = _cents(web_demand.get("limit")) if limit is None else limit
        if web_demand.get("enabled") is False:
            limit = 0
    if hard_limit is not None and limit is None:
        if hard_limit.get("noUsageBasedAllowed") is True:
            limit = 0
        else:
            dollars = _num(hard_limit.get("hardLimit"))
            if dollars is not None and dollars < 2_147_483_647:
                limit = round(dollars * 100)
    end = _num(current.get("billingCycleEnd"))
    if end is None:
        end = _num(current.get("billingCycleEndMs"))
    percent = _percent(plan if plan is not None else web_plan)
    membership = current.get("membershipType")
    return CursorUsage(
        included_percent=percent,
        on_demand_used_cents=used,
        on_demand_limit_cents=limit,
        cycle_end_s=None if end is None or end <= 0 else end / 1000.0,
        plan=membership if isinstance(membership, str) else None,
    )


@dataclass(frozen=True)
class Availability:
    """Whether Cursor can take a run now. ``ok`` None is unknown, and unknown is not available."""

    ok: bool | None
    why: str


HARD_CAP_CENTS: Final = 25_000


def availability(
    usage: CursorUsage | None,
    *,
    margin_cents: int,
    reason: str = "",
    ceiling_cents: int = HARD_CAP_CENTS,
) -> Availability:
    """Cursor is used until it is used up, on-demand included, to within ``margin_cents``.

    While on-demand spend is zero the seat is still on included usage plus Cursor's bonus,
    which is not visible as a number: it ends the moment on-demand starts counting. Then the
    cap decides, and the cap is the lower of what Cursor reports and vibe's own ceiling
    (``ceiling_cents``, $250): vibe never allows more than that, whatever the answer says, and
    an answer that names no cap is held to the ceiling. A cap of 0 means on-demand is off.
    """
    if usage is None:
        return Availability(None, reason or "usage unknown")
    used, reported = usage.on_demand_used_cents, usage.on_demand_limit_cents
    if used is None:
        return Availability(None, "usage unknown (no on-demand figure in the answer)")
    if used <= 0:
        return Availability(True, "included usage and bonus left, on-demand untouched")
    cap = ceiling_cents if reported is None else min(reported, ceiling_cents)
    if cap <= 0:
        return Availability(False, "included usage is used up and on-demand is off")
    left = cap - used
    if left > margin_cents:
        return Availability(True, f"on-demand ${left / 100:.2f} of ${cap / 100:.2f} left")
    return Availability(False, f"on-demand used to within ${margin_cents / 100:.2f} of its cap")


def near_cap(usage: CursorUsage | None, *, margin_cents: int, ceiling_cents: int) -> bool:
    """Whether a reading is close enough to the cap that only a very fresh one may route."""
    if usage is None or usage.on_demand_used_cents is None or usage.on_demand_used_cents <= 0:
        return False
    reported = usage.on_demand_limit_cents
    cap = ceiling_cents if reported is None else min(reported, ceiling_cents)
    return cap - usage.on_demand_used_cents <= margin_cents * 5


# --- the request ---------------------------------------------------------------------


def _rpc(
    client: HttpClient, token: Secret, method: str, *, timeout_s: float
) -> Mapping[str, object]:
    response = client.request(
        "POST",
        f"{API_BASE}/{SERVICE}/{method}",
        headers={
            # The sixth permitted reveal() site (contract s5): one header, nowhere else.
            "Authorization": f"Bearer {token.reveal()}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
        },
        body=b"{}",
        timeout_s=timeout_s,
    )
    return response.json()


def fetch(
    client: HttpClient, port: CursorTokenPort, *, timeout_s: float = 10.0
) -> tuple[CursorUsage | None, str]:
    """The seat's usage and, when there is none, a reason that is safe to print."""
    try:
        token = port.read()
    except VibeError as exc:
        return None, scrub(exc.message)
    if token is None:
        return None, "usage unknown (no Cursor login token found)"
    try:
        current = _rpc(client, token, "GetCurrentPeriodUsage", timeout_s=timeout_s)
    except HTTPError as exc:
        hint = " - run: cursor-agent login" if exc.status in (401, 403) else ""
        return None, f"usage unknown (Cursor answered HTTP {exc.status}{hint})"
    except VibeError as exc:
        return None, f"usage unknown ({scrub(exc.message)})"
    try:
        hard: Mapping[str, object] | None = _rpc(client, token, "GetHardLimit", timeout_s=timeout_s)
    except (HTTPError, VibeError):
        hard = None  # the cap is optional: the usage answer usually names it
    parsed = parse_usage(current, hard)
    if parsed is None:
        return None, "usage unknown (the answer is not in a shape vibe knows)"
    return parsed, ""
