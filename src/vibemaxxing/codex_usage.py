"""What is left on a Codex plan, read the way the Codex CLI's own `/status` reads it.

The CLI asks the ChatGPT backend ``GET https://chatgpt.com/backend-api/wham/usage`` with
``Authorization: Bearer <access token>`` and ``ChatGPT-Account-ID: <account id>`` (openai/codex
``codex-rs/backend-client/src/client/rate_limit_resets.rs`` ``rate_limit_status_url``,
``codex-rs/model-provider/src/bearer_auth_provider.rs``). The answer is the OpenAPI model
``RateLimitStatusPayload`` (``codex-rs/codex-backend-openapi-models``): ``plan_type``,
``rate_limit.{allowed, limit_reached, primary_window, secondary_window}`` with each window
``{used_percent, limit_window_seconds, reset_after_seconds, reset_at}``, ``credits``, and
``additional_rate_limits``. The windows are labelled by length as the CLI's TUI does
(``codex-rs/tui/src/chatwidget/rate_limits.rs``: 5h, daily, weekly, monthly, annual).

The endpoint is the CLI's own, not a documented API: any shape that is not what the CLI's
code reads comes out as "unknown", and nothing here is routed by.

The access token is read from ``auth.json`` at the moment of the request, held in a
``Secret``, and goes into one header. vibe never stores it, never logs it, and none of its
messages carry it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from vibemaxxing.codex import CodexLogin
from vibemaxxing.errors import VibeError
from vibemaxxing.httpclient import HttpClient, HTTPError
from vibemaxxing.redact import scrub

USAGE_URL: Final = "https://chatgpt.com/backend-api/wham/usage"
ACCOUNT_HEADER: Final = "ChatGPT-Account-ID"
# A cached answer is used for this long. The windows move slowly (5h and weekly).
FRESH_S: Final = 120.0
# An older one is shown with its time up to this age.
STALE_S: Final = 900.0
# A token this close to its end is not sent: the CLI renews it, vibe never does.
EXPIRY_SKEW_S: Final = 30.0


@dataclass(frozen=True)
class Window:
    label: str
    used_percent: float
    window_minutes: int | None
    resets_at_s: float | None


@dataclass(frozen=True)
class Spend:
    """A spend limit the plan reports (``spend_control.individual_limit``), in the unit it names.

    A usage-based workspace has no rate windows, only this: ``unit`` is what the answer says
    ("credit"), ``used`` and ``limit`` are in that unit, nothing is converted or assumed.
    """

    unit: str
    used: float
    limit: float
    used_percent: float | None
    resets_at_s: float | None


def number_text(value: float) -> str:
    """``5100`` as ``5,100``, ``0.5`` as ``0.5``; no trailing zeros."""
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def spend_title(spend: Spend) -> str:
    """``Credits used 0 of 5,100``: the unit as the answer names it, capitalised and plural."""
    unit = spend.unit.strip().capitalize()
    plural = unit if unit.endswith("s") else f"{unit}s"
    return f"{plural} used {number_text(spend.used)} of {number_text(spend.limit)}"


def spend_percent(spend: Spend) -> float:
    """The percent the answer reports; else used / limit."""
    if spend.used_percent is not None:
        return spend.used_percent
    return 0.0 if spend.limit <= 0 else spend.used / spend.limit * 100


@dataclass(frozen=True)
class CodexUsage:
    """What a Codex plan has left. Empty ``windows`` means the answer named none."""

    plan: str | None
    windows: tuple[Window, ...]
    allowed: bool | None = None
    limit_reached: bool | None = None
    credits_balance: str | None = None
    credits_unlimited: bool | None = None
    spend: Spend | None = None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _quantity(value: object) -> float | None:
    """A number the answer sends as a number or as a string (``"5100"``, ``"0.0"``)."""
    if isinstance(value, str):
        try:
            return _number(float(value))
        except ValueError:
            return None
    return _number(value)


def _spend_of(control: Mapping[str, object] | None) -> Spend | None:
    limit = _member(control, "individual_limit") if control is not None else None
    if limit is None:
        return None
    unit, cap, used = limit.get("unit"), _quantity(limit.get("limit")), _quantity(limit.get("used"))
    if not isinstance(unit, str) or not unit.strip() or cap is None or used is None:
        return None
    reset = _number(limit.get("reset_at"))
    return Spend(
        unit=unit.strip(),
        used=used,
        limit=cap,
        used_percent=_number(limit.get("used_percent")),
        resets_at_s=None if reset is None or reset <= 0 else reset,
    )


def _member(raw: object, key: str) -> Mapping[str, object] | None:
    if not isinstance(raw, Mapping):
        return None
    found = raw.get(key)
    return found if isinstance(found, Mapping) else None


def _near(minutes: int, expected: int) -> bool:
    return expected * 0.95 <= minutes <= expected * 1.05


def window_label(minutes: int | None, *, secondary: bool) -> str:
    """The CLI's own names for a window length (``get_limits_duration``)."""
    if minutes is not None:
        for name, expected in (
            ("5h", 5 * 60),
            ("daily", 24 * 60),
            ("weekly", 7 * 24 * 60),
            ("monthly", 30 * 24 * 60),
            ("annual", 365 * 24 * 60),
        ):
            if _near(minutes, expected):
                return name
    return "secondary usage" if secondary else "usage"


def _window(raw: Mapping[str, object], *, secondary: bool, prefix: str = "") -> Window | None:
    used = _number(raw.get("used_percent"))
    if used is None:
        return None
    seconds = _number(raw.get("limit_window_seconds"))
    # Whole minutes, rounded up, as the CLI does; a window of no length is unlabelled.
    minutes = None if seconds is None or seconds <= 0 else int((seconds + 59) // 60)
    reset = _number(raw.get("reset_at"))
    return Window(
        label=prefix + window_label(minutes, secondary=secondary),
        used_percent=used,
        window_minutes=minutes,
        resets_at_s=None if reset is None or reset <= 0 else reset,
    )


def _windows_of(details: Mapping[str, object] | None, *, prefix: str = "") -> list[Window]:
    if details is None:
        return []
    found: list[Window] = []
    for key, secondary in (("primary_window", False), ("secondary_window", True)):
        raw = _member(details, key)
        parsed = None if raw is None else _window(raw, secondary=secondary, prefix=prefix)
        if parsed is not None:
            found.append(parsed)
    return found


def parse_usage(answer: Mapping[str, object]) -> CodexUsage | None:
    """The ``/wham/usage`` answer as `CodexUsage`; None when it is not the shape the CLI reads."""
    plan = answer.get("plan_type")
    limits = _member(answer, "rate_limit")
    credits = _member(answer, "credits")
    extra = answer.get("additional_rate_limits")
    if not isinstance(plan, str) and limits is None and credits is None:
        return None
    windows = _windows_of(limits)
    for item in extra if isinstance(extra, list) else []:
        if isinstance(item, Mapping):
            name = item.get("limit_name")
            prefix = f"{name} " if isinstance(name, str) and name else ""
            windows.extend(_windows_of(_member(item, "rate_limit"), prefix=prefix))
    control = _member(answer, "spend_control")
    spend = _spend_of(control)
    allowed = limits.get("allowed") if limits is not None else None
    reached = limits.get("limit_reached") if limits is not None else None
    if reached is None and control is not None:
        reached = control.get("reached")
    balance = credits.get("balance") if credits is not None else None
    unlimited = credits.get("unlimited") if credits is not None else None
    return CodexUsage(
        plan=plan if isinstance(plan, str) else None,
        windows=tuple(windows),
        allowed=allowed if isinstance(allowed, bool) else None,
        limit_reached=reached if isinstance(reached, bool) else None,
        credits_balance=balance if isinstance(balance, str) else None,
        credits_unlimited=unlimited if isinstance(unlimited, bool) else None,
        spend=spend,
    )


def usage_to_payload(usage: CodexUsage) -> dict[str, object]:
    """What the shared usage cache holds for a Codex account: numbers only, never a token."""
    return {
        "plan": usage.plan,
        "windows": [
            {
                "label": w.label,
                "used_percent": w.used_percent,
                "window_minutes": w.window_minutes,
                "resets_at_s": w.resets_at_s,
            }
            for w in usage.windows
        ],
        "allowed": usage.allowed,
        "limit_reached": usage.limit_reached,
        "credits_balance": usage.credits_balance,
        "credits_unlimited": usage.credits_unlimited,
        "spend": None
        if usage.spend is None
        else {
            "unit": usage.spend.unit,
            "used": usage.spend.used,
            "limit": usage.spend.limit,
            "used_percent": usage.spend.used_percent,
            "resets_at_s": usage.spend.resets_at_s,
        },
    }


def usage_from_payload(payload: Mapping[str, object]) -> CodexUsage:
    windows: list[Window] = []
    raw = payload.get("windows")
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        label, used = item.get("label"), _number(item.get("used_percent"))
        minutes, reset = _number(item.get("window_minutes")), _number(item.get("resets_at_s"))
        if isinstance(label, str) and used is not None:
            windows.append(Window(label, used, None if minutes is None else int(minutes), reset))
    plan, allowed, reached = (
        payload.get("plan"),
        payload.get("allowed"),
        payload.get("limit_reached"),
    )
    balance, unlimited = payload.get("credits_balance"), payload.get("credits_unlimited")
    held = payload.get("spend")
    spend = None
    if isinstance(held, Mapping):
        unit, cap, used = held.get("unit"), _number(held.get("limit")), _number(held.get("used"))
        if isinstance(unit, str) and cap is not None and used is not None:
            reset = _number(held.get("resets_at_s"))
            spend = Spend(unit, used, cap, _number(held.get("used_percent")), reset)
    return CodexUsage(
        plan=plan if isinstance(plan, str) else None,
        windows=tuple(windows),
        allowed=allowed if isinstance(allowed, bool) else None,
        limit_reached=reached if isinstance(reached, bool) else None,
        credits_balance=balance if isinstance(balance, str) else None,
        credits_unlimited=unlimited if isinstance(unlimited, bool) else None,
        spend=spend,
    )


# --- the request ---------------------------------------------------------------------


def fetch(
    client: HttpClient, login: CodexLogin, *, timeout_s: float = 10.0
) -> tuple[CodexUsage | None, str]:
    """The plan's usage and, when there is none, a reason that is safe to print."""
    if login.access_token is None:
        return None, "usage unknown (no ChatGPT access token in the Codex login)"
    headers = {
        # The seventh permitted reveal() site (contract s5): one header, nowhere else.
        "Authorization": f"Bearer {login.access_token.reveal()}",
        "Accept": "application/json",
    }
    if login.account_id is not None:
        headers[ACCOUNT_HEADER] = login.account_id
    try:
        response = client.request("GET", USAGE_URL, headers=headers, timeout_s=timeout_s)
        answer = response.json()
    except HTTPError as exc:
        hint = " - run `codex` once, it renews its login" if exc.status in (401, 403) else ""
        return None, f"usage unknown (ChatGPT answered HTTP {exc.status}{hint})"
    except VibeError as exc:
        return None, f"usage unknown ({scrub(exc.message)})"
    parsed = parse_usage(answer)
    if parsed is None:
        return None, "usage unknown (the answer is not in a shape vibe knows)"
    return parsed, ""
