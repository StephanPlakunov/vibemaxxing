"""Pooled headroom across accounts, the burn-rate forecast over it, and the decision
to leave the live account for another. Pure: nothing here reads a clock or a file."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from vibemaxxing.models import AccountState, Sample
from vibemaxxing.poll import USAGE_FRESH_S
from vibemaxxing.usage import SCOPED_LABEL_PREFIX, Row

WEEKLY_ALL_KIND: Final = "weekly_all"
SCOPED_KIND: Final = "weekly_scoped"
# The rows a switch is about. weekly_scoped is one model's own cap and kinds the server
# adds later are not known to mean "this account is spent", so neither moves anyone --
# unless an ordered policy names the model (Policy.block_models).
TRIGGER_KINDS: Final = ("session", WEEKLY_ALL_KIND)
EARLIEST_RESET: Final = "earliest_reset"

REASON_OFF: Final = "auto-switch is off"
REASON_LIVE_UNKNOWN: Final = "live account unknown"

DEFAULT_THRESHOLD: Final = 99
DEFAULT_COOLDOWN_S: Final = 900
MIN_THRESHOLD: Final = 50
MAX_THRESHOLD: Final = 100


@dataclass(frozen=True)
class AccountUsage:
    state: AccountState
    rows: tuple[Row, ...]


def pool_remaining(accounts: Iterable[AccountUsage]) -> float:
    """Account-weeks left, summed over the weekly_all row only.

    The session and weekly_scoped rows overlap the weekly_all one, so counting
    them would inflate the pool by however many limit kinds the server ships.
    """
    total = 0.0
    for account in accounts:
        if account.state is not AccountState.OK:
            continue
        for row in account.rows:
            if row.kind == WEEKLY_ALL_KIND and row.percent is not None:
                total += (100 - min(max(row.percent, 0), 100)) / 100
                break
    return total


def dry_in(samples: Sequence[Sample]) -> timedelta | None:
    """Time until the pool reaches zero at the rate between oldest and newest.

    Deliberately not clamped at the next weekly reset: a forecast that stops at
    the boundary reads as "you are fine until then" when the truth is that the
    pool runs out first.
    """
    if len(samples) < 2:
        return None
    oldest = min(samples, key=lambda sample: sample.at_s)
    newest = max(samples, key=lambda sample: sample.at_s)
    span_s = newest.at_s - oldest.at_s
    if span_s <= 0:
        return None
    rate = (oldest.pool - newest.pool) / span_s
    if rate <= 0:
        return None
    return timedelta(seconds=newest.pool / rate)


@dataclass(frozen=True)
class Step:
    """One step of an ordered policy: ``account`` while every row that counts is below ``until``."""

    account: str
    until: int


@dataclass(frozen=True)
class Policy:
    """`policy` in settings.json.

    No steps is the original rule: leave the live account once it reaches
    ``Settings.threshold``, for the account with the most room. With steps, the first
    step whose account has room is where Claude Code should be, and when none has, the
    account that gets room back first.
    """

    steps: tuple[Step, ...] = ()
    # Models whose own weekly row holds an account back like its session and weekly rows.
    # None by default: one model's cap leaves the account usable for the others.
    block_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    """`~/.vibemaxxing/settings.json`. The defaults are also what an invalid file means."""

    auto_switch: bool = False
    threshold: int = DEFAULT_THRESHOLD
    cooldown_s: int = DEFAULT_COOLDOWN_S
    excluded: tuple[str, ...] = ()
    policy: Policy = Policy()


@dataclass(frozen=True)
class LastSwitch:
    """`~/.vibemaxxing/auto.json`: the last switch, whoever made it.

    ``until_s`` is when the rows that limited ``from_alias`` have all reset, or None
    when that was not known. The account just left stays out of the running until then.
    """

    from_alias: str
    to_alias: str
    at_s: float
    reason: str
    until_s: float | None
    # The limit ``from_alias`` was left at. An ordered policy uses one account at two
    # limits (85% first, 99% later), and the fence only holds at this one and below.
    # None, from a record that predates it or an undo, fences at every limit.
    limit: int | None = None


@dataclass(frozen=True)
class Reading:
    """One account as `decide` sees it. ``rows`` is None when there is no usable answer."""

    alias: str
    state: AccountState
    rows: tuple[Row, ...] | None
    updated_at: float | None
    # False for an account stored without an uuid. After landing on one, ~/.claude.json
    # still names the account that was left, so vibe could not tell who is live; it is
    # never chosen as a target on its own.
    identified: bool = True


@dataclass(frozen=True)
class Decision:
    action: Literal["stay", "switch"]
    target: str | None
    reason: str
    # The limit the live account was over when it is left; what LastSwitch.limit records.
    limit: int | None = None


def _stay(reason: str) -> Decision:
    return Decision("stay", None, reason)


def parse_stamp(stamp: str) -> float | None:
    """A timestamp the server sent, as epoch seconds; None when it is not one."""
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _reset_s(row: Row) -> float | None:
    # An unreadable reset is an unknown one, which callers treat as "has not happened yet".
    return None if row.resets_at is None else parse_stamp(row.resets_at)


def _counts(row: Row, models: Sequence[str]) -> bool:
    """Whether ``row`` can hold an account back: session, weekly_all, and named models."""
    if row.kind in TRIGGER_KINDS:
        return True
    if row.kind != SCOPED_KIND or not models or not row.label.startswith(SCOPED_LABEL_PREFIX):
        return False
    model = row.label.removeprefix(SCOPED_LABEL_PREFIX).casefold()
    return model in {name.casefold() for name in models}


def _over(rows: Iterable[Row], threshold: int, models: Sequence[str] = ()) -> list[Row]:
    return [
        row
        for row in rows
        if _counts(row, models) and row.percent is not None and row.percent >= threshold
    ]


def blocking(
    rows: Iterable[Row], threshold: int, now_s: float, *, models: Sequence[str] = ()
) -> tuple[Row, ...]:
    """The trigger rows at or over ``threshold`` whose window has not reset yet.

    A reading is a floor until its own row resets: usage only falls when the window
    rolls over. So 99% with a reset still ahead is true however old the reading is,
    and one whose reset has passed says nothing about now.
    """
    found: list[Row] = []
    for row in _over(rows, threshold, models):
        reset_s = _reset_s(row)
        if reset_s is None or reset_s > now_s:
            found.append(row)
    return tuple(found)


def limited_until(
    rows: Iterable[Row], threshold: int, now_s: float, *, models: Sequence[str] = ()
) -> float | None:
    """When every row holding the account over ``threshold`` has reset; None if unknown."""
    resets = [_reset_s(row) for row in blocking(rows, threshold, now_s, models=models)]
    known = [reset_s for reset_s in resets if reset_s is not None]
    if not known or len(known) != len(resets):
        return None
    return max(known)


def trigger_percent(rows: Iterable[Row], models: Sequence[str] = ()) -> int | None:
    """The fullest of the rows that count; None when any is unknown or none exist."""
    percents = [row.percent for row in rows if _counts(row, models)]
    known = [percent for percent in percents if percent is not None]
    if not known or len(known) != len(percents):
        return None
    return max(known)


def _fullness(
    reading: Reading, *, settings: Settings, live: str, now_s: float, last: LastSwitch | None
) -> int | None:
    """How full a candidate is, or None when it is not one.

    State ok is what makes the credential usable: envelope.collect has already refused
    a lapsed login and refreshed an expired token before it hands over a reading.
    """
    if reading.alias == live or reading.alias in settings.excluded or not reading.identified:
        return None
    if reading.state is not AccountState.OK or reading.rows is None or reading.updated_at is None:
        return None
    if not 0 <= now_s - reading.updated_at < USAGE_FRESH_S:
        return None
    if _over(reading.rows, settings.threshold):
        return None
    if (
        last is not None
        and last.from_alias == reading.alias
        and last.until_s is not None
        and now_s < last.until_s
    ):
        return None
    return trigger_percent(reading.rows)


def _ranked(
    readings: Sequence[Reading],
    *,
    settings: Settings,
    live: str,
    now_s: float,
    last: LastSwitch | None,
) -> list[tuple[int, str]]:
    ranked: list[tuple[int, str]] = []
    for reading in readings:
        fullness = _fullness(reading, settings=settings, live=live, now_s=now_s, last=last)
        if fullness is not None:
            ranked.append((fullness, reading.alias))
    # Percentages are compared across accounts only here, and only the worse of the two
    # rows that matter; ties go to the alias that sorts first.
    return sorted(ranked)


def next_candidate(
    readings: Sequence[Reading],
    *,
    settings: Settings,
    live: str,
    now_s: float,
    last: LastSwitch | None,
) -> str | None:
    """Where a switch would go if one were due, trigger or not."""
    if settings.policy.steps:
        _, following = ordered_steps(readings, settings=settings, live=live, now_s=now_s, last=last)
        return None if following is None else settings.policy.steps[following].account
    ranked = _ranked(readings, settings=settings, live=live, now_s=now_s, last=last)
    return ranked[0][1] if ranked else None


def decide(
    readings: Sequence[Reading],
    *,
    settings: Settings,
    live: str | None,
    now_s: float,
    last: LastSwitch | None,
) -> Decision:
    if not settings.auto_switch:
        return _stay(REASON_OFF)
    if live is None:
        return _stay(REASON_LIVE_UNKNOWN)
    if settings.policy.steps:
        return _decide_ordered(readings, settings=settings, live=live, now_s=now_s, last=last)
    current = next((reading for reading in readings if reading.alias == live), None)
    if current is None or current.rows is None:
        return _stay(f"no fresh numbers for {live}")
    if not _over(current.rows, settings.threshold):
        return _stay(f"{live} is below {settings.threshold}%")
    blocked = blocking(current.rows, settings.threshold, now_s)
    if not blocked:
        return _stay(f"no fresh numbers for {live}")
    # abs: a few seconds of clock skew still counts as just switched, while a record that
    # names a time far ahead (a hand edit, a wrong clock) is no cooldown rather than one
    # that lasts until then.
    if last is not None and abs(now_s - last.at_s) < settings.cooldown_s:
        return _stay(f"cooldown: last switch {int(abs(now_s - last.at_s))}s ago")
    ranked = _ranked(readings, settings=settings, live=live, now_s=now_s, last=last)
    if not ranked:
        return _stay("no account with headroom")
    fullness, target = ranked[0]
    worst = max(blocked, key=lambda row: row.percent or 0)
    return Decision(
        "switch",
        target,
        f"{live} {worst.kind} at {worst.percent}%; {target} is at {fullness}%",
        settings.threshold,
    )


# --- the ordered policy -------------------------------------------------------------


def _fresh(reading: Reading, now_s: float) -> bool:
    return reading.updated_at is not None and 0 <= now_s - reading.updated_at < USAGE_FRESH_S


def _under(reading: Reading, limit: int, models: Sequence[str], now_s: float) -> int | None:
    """How full ``reading`` is when fresh numbers put every row that counts below ``limit``.

    Only fresh numbers say an account is below a limit: a reading is a floor, so an old
    one that was below may not be any more.
    """
    if reading.rows is None or not _fresh(reading, now_s) or _over(reading.rows, limit, models):
        return None
    return trigger_percent(reading.rows, models)


def _fenced(alias: str, limit: int, now_s: float, last: LastSwitch | None) -> bool:
    """Whether ``alias`` was left at ``limit`` or lower and what limited it has not reset."""
    return (
        last is not None
        and last.from_alias == alias
        and last.until_s is not None
        and now_s < last.until_s
        and (last.limit is None or limit <= last.limit)
    )


def _usable(reading: Reading, *, settings: Settings, live: str | None) -> bool:
    # State ok is the login check: envelope.collect has already refused a lapsed login and
    # refreshed an expired token of every account but the live one.
    return (
        reading.alias != live
        and reading.alias not in settings.excluded
        and reading.identified
        and reading.state is AccountState.OK
    )


def _qualifies(
    reading: Reading | None,
    step: Step,
    *,
    settings: Settings,
    live: str | None,
    now_s: float,
    last: LastSwitch | None,
) -> int | None:
    """How full a step's account is when it may be switched to at that step; None if not."""
    if reading is None or not _usable(reading, settings=settings, live=live):
        return None
    if _fenced(reading.alias, step.until, now_s, last):
        return None
    return _under(reading, step.until, settings.policy.block_models, now_s)


def ordered_steps(
    readings: Sequence[Reading],
    *,
    settings: Settings,
    live: str | None,
    now_s: float,
    last: LastSwitch | None,
) -> tuple[int | None, int | None]:
    """The step the policy is on now, and the first step on another account that has room.

    0-based indexes into ``settings.policy.steps``. The first is where Claude Code should
    be; the second is where a switch would go once the live account is spent.
    """
    by_alias = {reading.alias: reading for reading in readings}
    models = settings.policy.block_models
    current: int | None = None
    following: int | None = None
    for index, step in enumerate(settings.policy.steps):
        reading = by_alias.get(step.account)
        if step.account == live:
            room = reading is not None and _under(reading, step.until, models, now_s) is not None
            if current is None and room:
                current = index
            continue
        if _qualifies(reading, step, settings=settings, live=live, now_s=now_s, last=last) is None:
            continue
        current = index if current is None else current
        following = index if following is None else following
    return current, following


def _spent(row: Row) -> str:
    if row.kind == "session":
        return f"{row.percent}% session"
    if row.kind == WEEKLY_ALL_KIND:
        return f"{row.percent}% weekly"
    return f"{row.percent}% weekly {row.label.removeprefix(SCOPED_LABEL_PREFIX)}"


def _decide_ordered(
    readings: Sequence[Reading],
    *,
    settings: Settings,
    live: str,
    now_s: float,
    last: LastSwitch | None,
) -> Decision:
    """Walk the steps; the first whose account has room is where Claude Code should be.

    The live account's numbers decide whether it has room at its own steps, and nothing
    is switched on a guess: no usable answer for it, or an old one below the limit, is a
    stay. Another account needs fresh numbers below the step's limit, a usable login, and
    not to be the account just left at that limit before what limited it has reset.
    """
    policy = settings.policy
    models = policy.block_models
    by_alias = {reading.alias: reading for reading in readings}
    current = by_alias.get(live)
    if current is None or current.rows is None:
        return _stay(f"no fresh numbers for {live}")
    own = [step.until for step in policy.steps if step.account == live]
    if not own:
        # A person put Claude Code on an account the policy does not name; that is theirs.
        return _stay(f"{live} is not in the policy")
    # The limits of its own steps that the live account is over, with the row that says so.
    over = [limit for limit in own if blocking(current.rows, limit, now_s, models=models)]
    total = len(policy.steps)
    for index, step in enumerate(policy.steps):
        if step.account == live:
            if step.until in over:
                continue
            if _under(current, step.until, models, now_s) is None:
                return _stay(f"no fresh numbers for {live}")
            return _stay(f"{live} is below {step.until}% (step {index + 1} of {total})")
        reading = by_alias.get(step.account)
        fullness = _qualifies(reading, step, settings=settings, live=live, now_s=now_s, last=last)
        if fullness is None:
            continue
        if over:
            # Leaving an account at or over one of its limits is never delayed.
            limit = max(over)
            worst = max(blocking(current.rows, limit, now_s, models=models), key=_percent)
            return Decision("switch", step.account, f"{live} at {_spent(worst)}", limit)
        # The live account still has room; this one outranks it. The cooldown applies.
        if last is not None and abs(now_s - last.at_s) < settings.cooldown_s:
            return _stay(f"cooldown: last switch {int(abs(now_s - last.at_s))}s ago")
        return Decision(
            "switch",
            step.account,
            f"{step.account} is at {fullness}%, below its {step.until}% step",
            min(own),
        )
    return _earliest_reset(by_alias, settings=settings, live=live, now_s=now_s, last=last)


def _percent(row: Row) -> int:
    return row.percent or 0


def _earliest_reset(
    by_alias: Mapping[str, Reading],
    *,
    settings: Settings,
    live: str,
    now_s: float,
    last: LastSwitch | None,
) -> Decision:
    """No step has room: be on the account that gets room back first.

    Each account's moment is when every row holding it over its highest limit has
    reset, the floors its numbers prove; the account just left waits for its fence
    as well. An account whose moment is unknown is not a place to go.
    """
    policy = settings.policy
    models = policy.block_models
    best: tuple[float, str] | None = None
    for account in dict.fromkeys(step.account for step in policy.steps):
        reading = by_alias.get(account)
        if reading is None or reading.rows is None:
            continue
        if account != live and not _usable(reading, settings=settings, live=live):
            continue
        limit = max(step.until for step in policy.steps if step.account == account)
        ready = limited_until(reading.rows, limit, now_s, models=models)
        if account != live and _fenced(account, limit, now_s, last) and last is not None:
            ready = max(ready or 0.0, last.until_s or 0.0)
        if ready is not None and (best is None or ready < best[0]):
            best = (ready, account)
    if best is None:
        return _stay("no account with headroom")
    target = best[1]
    reason = f"every account is at its limit; {target} resets first"
    if target == live:
        return _stay(reason)
    limit = max(step.until for step in policy.steps if step.account == live)
    return Decision("switch", target, reason, limit)
