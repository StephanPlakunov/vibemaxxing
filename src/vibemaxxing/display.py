"""One shape for every limit row, whatever the account type.

Every limit `vibe usage` shows (a Claude window or monthly spend, Cursor's included usage and
on-demand budget, a Codex window or credit pool, an Antigravity window) becomes a `Limit`, and
every `Limit` is drawn the same way::

    Weekly · all models     ███████████████████░  98%  resets in 2d
    On-demand               ████████████████████ 100%  $750.17 of $750  resets in 6d

label, bar, percent USED, then the amount (for a dollar or credit limit) and the reset. The
level (`ok`, `high`, `full`) colours the row and is decided here once, from the percent, for
every type: the terminal and the dashboard colour by it alike.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from vibemaxxing import codex_usage
from vibemaxxing.redact import scrub

if TYPE_CHECKING:  # launch and envelope import much more; only their types are needed.
    from vibemaxxing.antigravity import Entry
    from vibemaxxing.envelope import AccountView
    from vibemaxxing.launch import CodexReading, CursorReading

BAR_WIDTH: Final = 20
LABEL_WIDTH: Final = 24
# At or above this share a limit reads as nearly used up.
HIGH_PERCENT: Final = 80.0
FULL_PERCENT: Final = 100.0

Level = Literal["ok", "high", "full"]
_ANSI: Final[dict[str, str]] = {"ok": "", "high": "\x1b[33m", "full": "\x1b[31m"}
_RESET: Final = "\x1b[0m"
_PERIOD_LABELS: Final = {"wk": "Weekly", "5h": "5h"}


@dataclass(frozen=True)
class Limit:
    """One row: how much of a limit is used (0-100+), what it is in money or credits, and when
    it resets (epoch seconds)."""

    label: str
    percent: float | None
    amount: str | None = None
    resets_at_s: float | None = None
    # The provider's own verdict (Claude's "critical", Codex's "limit reached"): never lowers
    # the level the percent gives, only raises it.
    flagged: bool = False


def level(limit: Limit) -> Level:
    percent = limit.percent
    if percent is not None and percent >= FULL_PERCENT:
        return "full"
    if limit.flagged or (percent is not None and percent >= HIGH_PERCENT):
        return "high"
    return "ok"


def relative(resets_at_s: float | None, now_s: float) -> str:
    """``resets in 3d`` / ``resets in 5h`` / ``resets in 12m``; "" when unknown."""
    if resets_at_s is None:
        return ""
    left_s = resets_at_s - now_s
    if left_s <= 0:
        return "resets now"
    if left_s < 3600:
        return f"resets in {max(1, round(left_s / 60))}m"
    if left_s < 86_400:
        return f"resets in {round(left_s / 3600)}h"
    return f"resets in {round(left_s / 86_400)}d"


def iso_to_s(value: str | None) -> float | None:
    """An ISO timestamp from the network as epoch seconds; None for anything else."""
    if value is None:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def bar(percent: float | None) -> str:
    if percent is None:
        return "─" * BAR_WIDTH
    filled = round(min(max(percent, 0.0), 100.0) / 100 * BAR_WIDTH)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def percent_text(percent: float | None) -> str:
    return "—" if percent is None else f"{round(percent)}%"


def row_text(limit: Limit, now_s: float, *, indent: str = "    ") -> str:
    """The row, uncoloured: label, bar, percent used, amount, reset."""
    label = scrub(limit.label)
    width = max(LABEL_WIDTH, len(label) + 1)
    tail = "  ".join(
        part for part in (scrub(limit.amount or ""), relative(limit.resets_at_s, now_s)) if part
    )
    text = f"{indent}{label:<{width}}{bar(limit.percent)} {percent_text(limit.percent):>4}"
    return f"{text}  {tail}" if tail else text


def use_colour(stream: object) -> bool:
    """ANSI colour only on a terminal, and never with NO_COLOR set (no-color.org)."""
    if os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def coloured(limit: Limit, text: str, *, colour: bool) -> str:
    code = _ANSI[level(limit)]
    return f"{code}{text}{_RESET}" if colour and code else text


def money(amount: float, currency: str | None = "USD") -> str:
    """``$750.17``, ``$750``: cents only when there are any."""
    sign = "$" if currency in (None, "", "USD") else f"{currency} "
    text = f"{amount:,.2f}"
    return sign + (text[:-3] if text.endswith(".00") else text)


# --- each account type's limits ------------------------------------------------


def claude_limits(view: AccountView) -> list[Limit]:
    summary = view.summary
    limits: list[Limit] = []
    for row in summary.rows if summary is not None else ():
        flagged = row.severity not in (None, "normal")
        if row.kind == "monthly_spend" and row.used is not None and row.limit is not None:
            amount = f"{money(row.used, row.currency)} of {money(row.limit, row.currency)}"
            limits.append(Limit("Monthly spend", row.percent, amount, None, flagged))
            continue
        limits.append(Limit(row.label, row.percent, None, iso_to_s(row.resets_at), flagged))
    return limits


def cursor_limits(reading: CursorReading) -> list[Limit]:
    usage = reading.usage
    if usage is None:
        return []
    resets = usage.cycle_end_s
    limits: list[Limit] = []
    if usage.included_percent is not None:
        limits.append(Limit("Included + bonus", usage.included_percent, None, resets))
    used, cap = usage.on_demand_used_cents, usage.on_demand_limit_cents
    if used is not None:
        if cap:
            amount = f"{money(used / 100)} of {money(cap / 100)}"
            share: float | None = used / cap * 100
        else:
            amount = f"{money(used / 100)}" + (" (off)" if cap == 0 else " (no limit reported)")
            share = None
        # Routing counts the seat as spent: say so even if the share rounds below 100.
        spent = reading.availability.ok is False and share is not None
        limits.append(Limit("On-demand", share, amount, resets, spent))
    return limits


def codex_limits(reading: CodexReading) -> list[Limit]:
    usage = reading.usage
    if usage is None:
        return []
    reached = bool(usage.limit_reached)
    limits = [
        Limit(window.label, window.used_percent, None, window.resets_at_s, reached)
        for window in usage.windows
    ]
    spend = usage.spend
    if spend is not None:
        unit = spend.unit.strip().casefold()
        plural = unit if unit.endswith("s") else f"{unit}s"
        amount = (
            f"{codex_usage.number_text(spend.used)} of {codex_usage.number_text(spend.limit)}"
            f" {plural}"
        )
        limits.append(
            Limit(
                plural.capitalize(),
                codex_usage.spend_percent(spend),
                amount,
                spend.resets_at_s,
                reached,
            )
        )
    return limits


def antigravity_limits(entry: Entry) -> list[Limit]:
    """Antigravity reports what is LEFT per model group and window; shown as the share used,
    the short window before the weekly one."""
    groups: list[str] = []
    for window in entry["windows"]:
        if window["group"] not in groups:
            groups.append(window["group"])
    windows = sorted(
        entry["windows"], key=lambda w: (groups.index(w["group"]), w["period"] == "wk")
    )
    return [
        Limit(
            f"{_PERIOD_LABELS.get(w['period'], w['period'])} · {w['group']}",
            100.0 - w["remaining_percent"],
            None,
            iso_to_s(w["resets_at"]),
        )
        for w in windows
    ]


def block(
    header: str,
    notes: Sequence[str],
    limits: Sequence[Limit],
    *,
    now_s: float,
    colour: bool,
    empty: str | None = "no limits reported",
) -> list[str]:
    """One account for `vibe usage`: its header, its notes, its rows (or why there are none)."""
    lines = [header, *(f"    {scrub(note)}" for note in notes)]
    lines += [coloured(limit, row_text(limit, now_s), colour=colour) for limit in limits]
    if not limits and empty is not None:
        lines.append(f"    {empty}")
    return lines
