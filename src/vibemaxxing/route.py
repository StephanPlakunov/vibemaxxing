"""Which account a new run starts on. Pure: no clock, no file, no network in here.

This is the routing of `vibe run auto`. It is decided once, when a run starts, and never
again for that run: a running Claude Code session keeps the login it started with. The
global switch of `vibe auto` (Keychain swap) stays between Claude accounts, because the
Keychain login cannot point at Cursor.

The defaults are the fork owner's (settings.json ``routing`` overrides every part):

* a run that **needs connectors** (Slack, Atlassian, ...): main while its weekly use is within
  today's budget, else Cursor while it has usage left, else extra (which has no connectors:
  the run goes without them and says so), else main whatever the budget;
* a run that needs **none**: extra, else main within budget, else Cursor, else main whatever
  the budget.

An account whose usage is unknown is unavailable, and the answer says so. The last step is
the exception: it is the last resort, so it runs on an unknown reading, and on a spent one with
a warning, and only a dead login stops it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from vibemaxxing import store
from vibemaxxing.cursor import KIND_CLAUDE, KIND_CURSOR
from vibemaxxing.errors import UsageError

DEFAULT_TIMEZONE: Final = "Europe/Berlin"
# Monday..Sunday: the share of main's weekly usage that may be spent by the END of that day.
DEFAULT_BUDGET: Final = (20, 40, 60, 80, 100, 100, 100)
DEFAULT_NEEDS: Final = ("slack", "atlassian")
# A run that needs one of these has nowhere to degrade to: only an account that has it will do.
DEFAULT_NO_DEGRADE: Final = ("m365",)
DEFAULT_CONNECTORS: Final[Mapping[str, tuple[str, ...]]] = {
    "main": ("slack", "atlassian", "m365"),
    "cursor": ("slack", "atlassian", "gcal"),
    "extra": (),
}
CONNECTORS: Final = ("slack", "atlassian", "m365", "gcal")
# The end of the warning for a run that goes without a connector it needs.
DEGRADED_SUFFIX: Final = ": this session runs without it"
# The three roles an order names. `routing.roles` maps each to the alias it is stored under;
# without it a role is the alias of the same name.
ROLES: Final = ("main", "extra", "cursor")
CLAUDE_ROLES: Final = ("main", "extra")
NEEDS_CLASS: Final = "needs"
NO_NEEDS_CLASS: Final = "no_needs"


class Health(StrEnum):
    OK = "ok"  # usable and has room
    OUT = "out"  # usable but spent until a reset
    UNKNOWN = "unknown"  # no fresh numbers
    DEAD = "dead"  # the login is gone


@dataclass(frozen=True)
class Step:
    """One rung of an order. ``account`` is a role (main, extra, cursor), else an alias."""

    account: str
    # Only while main's weekly use is within today's budget (Claude accounts).
    budget: bool = False
    # Allowed to lack the connectors the run needs, with a warning (never for no_degrade ones).
    degraded: bool = False
    # The last resort: runs even on a spent or unknown reading. Only a dead login stops it.
    last_resort: bool = False


DEFAULT_ORDER: Final[Mapping[str, tuple[Step, ...]]] = {
    NEEDS_CLASS: (
        Step("main", budget=True),
        Step("cursor"),
        Step("extra", degraded=True),
        Step("main", last_resort=True),
    ),
    NO_NEEDS_CLASS: (
        Step("extra"),
        Step("main", budget=True),
        Step("cursor"),
        Step("main", last_resort=True),
    ),
}


@dataclass(frozen=True)
class Routing:
    timezone: str = DEFAULT_TIMEZONE
    weekday_budget: tuple[int, ...] = DEFAULT_BUDGET
    default_needs: tuple[str, ...] = DEFAULT_NEEDS
    no_degrade: tuple[str, ...] = DEFAULT_NO_DEGRADE
    connectors: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_CONNECTORS)
    )
    order: Mapping[str, tuple[Step, ...]] = field(default_factory=lambda: dict(DEFAULT_ORDER))
    # role -> alias. The order and the connectors are written in roles, so renaming an account
    # means changing this one member, not the order.
    roles: Mapping[str, str] = field(default_factory=lambda: {role: role for role in ROLES})
    # The most Cursor on-demand spend vibe will ever allow, in cents, whatever Cursor reports.
    cursor_cap_cents: int = 25_000
    # Cursor on-demand is spent to within this many cents of the personal cap.
    cursor_margin_cents: int = 1000
    # The percent at which a Claude account counts as spent (session or weekly).
    spent_percent: int = 99
    # The `bridge` member, validated by vibe-bridge (bridge.py) where it is used.
    bridge: object = None
    # One shared login: every Claude run that vibe routes goes on Claude Code's own login, which
    # `vibe auto tick` moves between the accounts, so a running session follows the switch.
    shared_login: bool = False


@dataclass(frozen=True)
class Candidate:
    """One account as routing sees it."""

    alias: str
    kind: str
    health: Health
    why: str
    # Main's weekly percent; None when unknown. Only read for a step with a budget.
    weekly_percent: int | None = None
    # Whether ``weekly_percent`` comes from numbers young enough to say "within budget".
    weekly_fresh: bool = False


@dataclass(frozen=True)
class Route:
    alias: str
    kind: str
    reason: str
    warnings: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    # The reason in a few words, for the one line `vibe run auto` prints ("75% < 80%",
    # "main spent"); empty when the first choice simply had room.
    short: str = ""


# --- settings.json `routing` ---------------------------------------------------------


def _fail(path: Path, why: str) -> UsageError:
    return UsageError(f"{path}: routing {why}", f"edit {path} (or delete its routing member)")


def _strings(raw: object) -> tuple[str, ...] | None:
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        return None
    return tuple(str(item) for item in raw)


def _int(raw: object) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def parse_routing(raw: object, path: Path) -> Routing:
    """The `routing` member of settings.json; UsageError for one that is wrong anywhere.

    A wrong routing is refused, not read as the defaults: `vibe run auto` would otherwise send a
    session to an account the owner never chose.
    """
    if raw is None:
        return Routing()
    if not isinstance(raw, dict):
        raise _fail(path, "must be an object")
    known = {
        "timezone",
        "weekday_budget",
        "default_needs",
        "no_degrade",
        "connectors",
        "order",
        "cursor_margin_cents",
        "spent_percent",
        "roles",
        "cursor_cap_cents",
        "bridge",
        "shared_login",
    }
    unknown = sorted(set(raw) - known)
    if unknown:
        raise _fail(path, f"has unknown members: {', '.join(unknown)}")
    tz = raw.get("timezone", DEFAULT_TIMEZONE)
    if not isinstance(tz, str):
        raise _fail(path, "timezone must be a name such as Europe/Berlin")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise _fail(path, f"timezone {tz!r} is not known on this machine") from None
    budget_raw = raw.get("weekday_budget", list(DEFAULT_BUDGET))
    budget = [_int(item) for item in budget_raw] if isinstance(budget_raw, list) else [None]
    if len(budget) != 7 or any(item is None or not 0 <= item <= 100 for item in budget):
        raise _fail(path, "weekday_budget must be seven percents, Monday to Sunday")
    needs = _strings(raw.get("default_needs", list(DEFAULT_NEEDS)))
    if needs is None or any(item not in CONNECTORS for item in needs):
        raise _fail(path, f"default_needs must be a list of {', '.join(CONNECTORS)}")
    no_degrade = _strings(raw.get("no_degrade", list(DEFAULT_NO_DEGRADE)))
    if no_degrade is None:
        raise _fail(path, "no_degrade must be a list of connector names")
    connectors: dict[str, tuple[str, ...]] = dict(DEFAULT_CONNECTORS)
    given = raw.get("connectors")
    if given is not None:
        if not isinstance(given, dict):
            raise _fail(path, "connectors must map an account to its connector names")
        for alias, names in given.items():
            parsed = _strings(names)
            if parsed is None or any(name not in CONNECTORS for name in parsed):
                raise _fail(path, f"connectors.{alias} must be a list of {', '.join(CONNECTORS)}")
            connectors[str(alias)] = parsed
    roles = {role: role for role in ROLES}
    given_roles = raw.get("roles")
    if given_roles is not None:
        if not isinstance(given_roles, dict) or set(given_roles) - set(ROLES):
            raise _fail(path, f"roles must map only {', '.join(ROLES)} to an account alias")
        for role, alias in given_roles.items():
            if not isinstance(alias, str) or not store.valid_alias(alias):
                raise _fail(path, f"roles.{role} must be an account alias")
            roles[str(role)] = alias
        if len(set(roles.values())) != len(roles):
            raise _fail(path, "two roles name the same account")
    order: dict[str, tuple[Step, ...]] = dict(DEFAULT_ORDER)
    given_order = raw.get("order")
    if given_order is not None:
        if not isinstance(given_order, dict) or set(given_order) - {NEEDS_CLASS, NO_NEEDS_CLASS}:
            raise _fail(path, f"order must have only {NEEDS_CLASS} and {NO_NEEDS_CLASS}")
        for cls, steps in given_order.items():
            order[str(cls)] = _parse_steps(steps, path, str(cls))
    cap = _int(raw.get("cursor_cap_cents", 25_000))
    if cap is None or cap < 0:
        raise _fail(path, "cursor_cap_cents must be a number of cents, 0 or more")
    margin = _int(raw.get("cursor_margin_cents", 1000))
    spent = _int(raw.get("spent_percent", 99))
    if margin is None or margin < 0:
        raise _fail(path, "cursor_margin_cents must be a number of cents, 0 or more")
    if spent is None or not 50 <= spent <= 100:
        raise _fail(path, "spent_percent must be between 50 and 100")
    shared = raw.get("shared_login", False)
    if not isinstance(shared, bool):
        raise _fail(path, "shared_login must be true or false")
    return Routing(
        timezone=tz,
        weekday_budget=tuple(item for item in budget if item is not None),
        default_needs=needs,
        no_degrade=no_degrade,
        connectors=connectors,
        order=order,
        roles=roles,
        cursor_cap_cents=cap,
        cursor_margin_cents=margin,
        spent_percent=spent,
        bridge=raw.get("bridge"),
        shared_login=shared,
    )


def _parse_steps(raw: object, path: Path, cls: str) -> tuple[Step, ...]:
    if not isinstance(raw, list) or not raw:
        raise _fail(path, f"order.{cls} must be a non-empty list of steps")
    steps: list[Step] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("account"), str):
            raise _fail(path, f'every step of order.{cls} needs an "account"')
        extra = set(item) - {"account", "budget", "degraded", "last_resort"}
        flags = [item.get(key, False) for key in ("budget", "degraded", "last_resort")]
        if extra or not all(isinstance(flag, bool) for flag in flags):
            raise _fail(path, f"order.{cls} has a step with a wrong member")
        if flags[2] and str(item["account"]) == "cursor":
            # The last resort runs on a spent or unknown reading; Cursor's would cost money.
            raise _fail(path, f"order.{cls}: a Cursor step cannot be the last resort")
        steps.append(Step(str(item["account"]), bool(flags[0]), bool(flags[1]), bool(flags[2])))
    return tuple(steps)


def read_routing(root: Path) -> Routing:
    """The routing in settings.json; the defaults when there is no file or no routing member."""
    path = root / "settings.json"
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Routing()
    except (OSError, ValueError):
        raise _fail(path, "cannot be read: the file is not valid JSON") from None
    member = parsed.get("routing") if isinstance(parsed, dict) else None
    return parse_routing(member, path)


# --- the budget ----------------------------------------------------------------------


def today_budget(routing: Routing, now_s: float) -> int:
    """The share of main's weekly usage allowed by the end of today, in the routing timezone."""
    day = datetime.fromtimestamp(now_s, ZoneInfo(routing.timezone)).weekday()
    return routing.weekday_budget[day]


# --- the decision --------------------------------------------------------------------


def needs_for(routing: Routing, *, needs: Sequence[str] | None, no_needs: bool) -> tuple[str, ...]:
    """The connectors a run needs: the flag, none for --no-needs, else the configured default."""
    if no_needs:
        return ()
    chosen = tuple(needs) if needs is not None else routing.default_needs
    unknown = [name for name in chosen if name not in CONNECTORS]
    if unknown:
        raise UsageError(
            f"unknown connector {', '.join(unknown)}; known: {', '.join(CONNECTORS)}",
            "vibe run auto --needs slack,atlassian -- claude",
        )
    return tuple(dict.fromkeys(chosen))


def decide(
    candidates: Sequence[Candidate] | Callable[[str], Candidate | None],
    *,
    routing: Routing,
    needs: Sequence[str],
    claude_only: bool,
    now_s: float,
) -> Route:
    cls = NEEDS_CLASS if needs else NO_NEEDS_CLASS
    if callable(candidates):
        lookup = candidates
    else:
        by_alias = {candidate.alias: candidate for candidate in candidates}
        lookup = by_alias.get
    budget = today_budget(routing, now_s)
    skipped: list[str] = []
    # The same reasons in a few words; the first one is why the pick is not the first choice.
    brief: list[str] = []
    wanted = set(needs)
    exclusive = wanted & set(routing.no_degrade)

    # Claude roles (main, extra) that name no stored account. If the order then ends on Cursor,
    # the run is refused: a misnamed account must never quietly become a company-billed run.
    unresolved: list[str] = []

    for step in routing.order[cls]:
        alias = routing.roles.get(step.account, step.account)
        candidate = lookup(alias)
        if candidate is None:
            what = step.account if alias == step.account else f"{step.account} (alias {alias!r})"
            skipped.append(f"{what}: no such account")
            brief.append(f"no account {alias}")
            if step.account in CLAUDE_ROLES:
                unresolved.append(f"{step.account} -> {alias!r}")
            continue
        if claude_only and candidate.kind == KIND_CURSOR:
            skipped.append(f"{candidate.alias}: the program is Claude-only")
            continue
        if candidate.health is Health.DEAD:
            skipped.append(f"{candidate.alias}: {candidate.why}")
            brief.append(f"{candidate.alias} logged out")
            continue
        have = set(routing.connectors.get(step.account, routing.connectors.get(alias, ())))
        missing = sorted(wanted - have)
        warnings: list[str] = []
        if missing:
            if not step.degraded or exclusive - have:
                gone = ", ".join(missing)
                skipped.append(f"{candidate.alias}: has no {gone}")
                brief.append(f"{candidate.alias} has no {gone}")
                continue
            warnings.append(f"{candidate.alias} has no {', '.join(missing)}{DEGRADED_SUFFIX}")
        if candidate.kind == KIND_CURSOR and unresolved:
            raise UsageError(
                f"{', '.join(unresolved)} is not a stored account, so this run would have gone "
                "to Cursor only because of a naming mismatch; not starting it",
                "set routing.roles in ~/.vibemaxxing/settings.json to your account aliases",
            )
        if step.last_resort:
            return _chosen(candidate, step, skipped, routing, budget, warnings, brief)
        if candidate.health is Health.UNKNOWN:
            skipped.append(f"{candidate.alias}: usage unknown, so not used ({candidate.why})")
            brief.append(f"{candidate.alias} usage unknown")
            continue
        if candidate.health is Health.OUT:
            skipped.append(f"{candidate.alias}: spent ({candidate.why})")
            brief.append(f"{candidate.alias} spent")
            continue
        if step.budget and candidate.kind == KIND_CLAUDE:
            if candidate.weekly_percent is None or not candidate.weekly_fresh:
                skipped.append(f"{candidate.alias}: weekly usage unknown, so the budget is unknown")
                brief.append(f"{candidate.alias} usage unknown")
                continue
            if candidate.weekly_percent > budget:
                skipped.append(
                    f"{candidate.alias}: weekly {candidate.weekly_percent}% is over today's "
                    f"{budget}% budget"
                )
                brief.append(f"{candidate.alias} {candidate.weekly_percent}% > {budget}%")
                continue
        return _chosen(candidate, step, skipped, routing, budget, warnings, brief)
    reasons = "; ".join(skipped) or "no step in the order"
    raise UsageError(f"no account can take this run ({reasons})", "vibe list")


def _chosen(
    candidate: Candidate,
    step: Step,
    skipped: Sequence[str],
    routing: Routing,
    budget: int,
    warnings: Sequence[str],
    brief: Sequence[str] = (),
) -> Route:
    short = brief[0] if brief else ""
    if step.last_resort:
        reason = f"last resort: every other choice was unavailable ({'; '.join(skipped)})"
        if candidate.health is Health.OUT:
            warnings = [
                *warnings,
                f"{candidate.alias} is spent ({candidate.why}): this may be refused",
            ]
        elif candidate.health is Health.UNKNOWN:
            warnings = [*warnings, f"{candidate.alias}: usage unknown ({candidate.why})"]
    elif step.budget and candidate.kind == KIND_CLAUDE:
        reason = (
            f"weekly {candidate.weekly_percent}% is within today's {budget}% budget"
            f" ({routing.timezone})"
        )
        short = short or budget_text(candidate.weekly_percent, budget)
    else:
        reason = candidate.why or "has room"
    if step.last_resort:
        short = "last resort" + (f", {short}" if short else "")
    return Route(candidate.alias, candidate.kind, reason, tuple(warnings), tuple(skipped), short)


def budget_text(percent: int | None, budget: int) -> str:
    """Main's weekly use against today's budget in a few words: ``75% < 80%``."""
    if percent is None:
        return f"budget {budget}%"
    sign = "<" if percent < budget else "=" if percent == budget else ">"
    return f"{percent}% {sign} {budget}%"
