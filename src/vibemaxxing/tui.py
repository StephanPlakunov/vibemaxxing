"""The Textual dashboard: one group per account, one pooled footer.

The widget tree is built from the account *shape* — which aliases are on disk,
and what each one puts on its own lines — and rebuilt only when that changes.
Every refresh after that writes new values into the widgets that already exist,
because this is a window left open for days and a tree rebuilt every minute is a
leak with a nice render (AC14).

Colour comes from each row's ``severity``: ``"normal"`` is calm and everything
else is elevated. The set is open-ended, so no percentage threshold is invented
here and no row layout is hardcoded — the rows are whatever the payload carried.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, ClassVar, Final

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.content import Content
from textual.widgets import Static

from vibemaxxing import antigravity, auto, cursor, display, envelope, history, launch, store
from vibemaxxing.envelope import AccountView
from vibemaxxing.poll import DASHBOARD_INTERVAL_S
from vibemaxxing.pool import dry_in
from vibemaxxing.redact import scrub
from vibemaxxing.usage import Row

if TYPE_CHECKING:  # cli imports tui; the annotation must not import it back.
    from vibemaxxing.cli import Context

# The floor is 60 s; the cadence is not. See poll.DASHBOARD_INTERVAL_S.
REFRESH_S: Final = DASHBOARD_INTERVAL_S
BAR_WIDTH: Final = display.BAR_WIDTH
LABEL_WIDTH: Final = display.LABEL_WIDTH

_CALM: Final = ""
_ELEVATED: Final = "$warning"
_WINDOW_S: Final = history.RETENTION_DAYS * 86_400.0
_EMPTY: Final = "no accounts yet — run: vibe add"


_STYLES: Final = {"ok": _CALM, "high": _ELEVATED, "full": "$error"}


def _relative(resets_at: str | None, now_s: float) -> str:
    return display.relative(display.iso_to_s(resets_at), now_s)


def _bar(percent: int | None) -> str:
    return display.bar(percent)


def _limit_line(limit: display.Limit, now_s: float) -> Content:
    return Content.styled(
        display.row_text(limit, now_s, indent="  "), _STYLES[display.level(limit)]
    )


def _row_line(row: Row, now_s: float) -> tuple[str, str]:
    flagged = row.severity not in (None, "normal")
    limit = display.Limit(row.label, row.percent, None, display.iso_to_s(row.resets_at), flagged)
    return display.row_text(limit, now_s, indent="  "), _STYLES[display.level(limit)]


def _who(view: AccountView) -> str:
    return view.identity.email or view.identity.display_name or view.alias


def _header(alias: str, rest: str, *, active: bool = False) -> Content:
    return Content.assemble(
        ("● " if active else "  ", _CALM),
        (scrub(alias), "bold"),
        (f"  {scrub(rest)}", "dim"),
    )


def account_content(view: AccountView, *, now_s: float) -> Content:
    lines: list[Content] = [
        _header(view.alias, f"{_who(view)}  {view.plan or '—'}", active=view.active)
    ]
    if view.message is not None:
        # Already carries its literal recovery command; never recomposed here.
        lines.append(Content.styled(f"  {scrub(view.message)}", _ELEVATED))
    summary = view.summary
    if summary is not None and not summary.rows:
        lines.append(Content.styled("  no limits reported", "dim"))
    lines += [_limit_line(limit, now_s) for limit in display.claude_limits(view)]
    return Content("\n").join(lines)


def cursor_content(reading: launch.CursorReading, *, now_s: float) -> Content:
    """A Cursor seat like an account: who, then its included and on-demand rows."""
    team = f" · team {reading.team}" if reading.team else ""
    lines: list[Content] = [_header(reading.alias, f"{reading.email or '—'}  Cursor{team}")]
    if not reading.logged_in:
        lines.append(Content.styled(f"  not logged in — run: {cursor.AGENT_LOGIN}", _ELEVATED))
        return Content("\n").join(lines)
    if reading.usage is None:
        lines.append(
            Content.styled(
                scrub(f"  usage: unknown ({reading.note}); see cursor.com/dashboard"), _ELEVATED
            )
        )
        return Content("\n").join(lines)
    if reading.note:
        lines.append(Content.styled(f"  {scrub(reading.note)}", _ELEVATED))
    lines += [_limit_line(limit, now_s) for limit in display.cursor_limits(reading)]
    return Content("\n").join(lines)


def codex_content(reading: launch.CodexReading, *, now_s: float) -> Content:
    """A Codex account like an account: who, the plan, one row per window and credit pool."""
    plan = f" · {reading.plan}" if reading.plan else ""
    lines: list[Content] = [_header(reading.alias, f"{reading.email or '—'}  Codex{plan}")]
    usage = reading.usage
    if usage is None:
        text = (
            reading.note
            if reading.state in ("needs_login", "api_key")
            else (f"usage: unknown ({reading.note})")
        )
        lines.append(Content.styled(f"  {scrub(text)}", _ELEVATED))
        return Content("\n").join(lines)
    if reading.note:
        lines.append(Content.styled(f"  {scrub(reading.note)}", _ELEVATED))
    limits = display.codex_limits(reading)
    lines += [_limit_line(limit, now_s) for limit in limits]
    if not limits:
        lines.append(Content.styled("  no limits reported", "dim"))
    return Content("\n").join(lines)


def antigravity_content(entry: antigravity.Entry, *, now_s: float) -> Content:
    """An Antigravity account like the others: one row per model group and window, share used."""
    lines: list[Content] = [_header(entry["alias"], antigravity.NAME)]
    if entry["message"]:
        lines.append(Content.styled(f"  {scrub(entry['message'])}", _ELEVATED))
    lines += [_limit_line(limit, now_s) for limit in display.antigravity_limits(entry)]
    return Content("\n").join(lines)


def _codex_shape(reading: launch.CodexReading) -> tuple[str, bool, tuple[str, ...] | None]:
    usage = reading.usage
    # A note, no usage or no windows adds a line, which is a different panel height.
    return (
        reading.alias,
        bool(reading.note) or usage is None,
        None
        if usage is None
        else (
            *(w.label for w in usage.windows),
            *(() if usage.spend is None else ("spend",)),
        ),
    )


def _cursor_shape(reading: launch.CursorReading) -> tuple[str, bool, tuple[str, ...] | None]:
    usage = reading.usage
    rows: list[str] = []
    if reading.logged_in and usage is not None:
        if usage.included_percent is not None:
            rows.append("included")
        if usage.on_demand_used_cents is not None:
            rows.append("on-demand")
    # A note or a missing login adds a line, which is a different panel height.
    return (
        reading.alias,
        bool(reading.note) or not reading.logged_in or usage is None,
        tuple(rows),
    )


def _shape(view: AccountView) -> tuple[str, bool, tuple[str, ...] | None]:
    """What a panel puts on its own lines: any change here needs a new layout."""
    summary = view.summary
    return (
        view.alias,
        view.message is not None,
        None if summary is None else tuple(row.label for row in summary.rows),
    )


def footer_content(*, weeks: float, accounts: int, forecast: timedelta | None) -> Content:
    text = f"pool {weeks} account-weeks  ·  {accounts} accounts"
    if forecast is not None:
        text += f"  ·  dry in {forecast.total_seconds() / 3600:.1f}h"
    return Content(text)


class Dashboard(App[None]):
    CSS = """
    #accounts { height: 1fr; padding: 1 2; }
    #empty { display: none; }
    #footer { dock: bottom; height: 1; padding: 0 2; background: $panel; }
    .account { margin-bottom: 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "quit"),
        Binding("r", "refresh_now", "refresh"),
    ]

    def __init__(self, ctx: Context) -> None:
        super().__init__()
        self._ctx = ctx
        self.panels: dict[str, Static] = {}
        # None until the first cycle lands: an empty store and a store nobody
        # has read yet say different things, and only one of them is "vibe add".
        self._shapes: list[tuple[str, bool, tuple[str, ...] | None]] | None = None
        # None until on_mount succeeds: a failure to open it must not turn every
        # later error into an AttributeError that replaces the real cause.
        self._conn: sqlite3.Connection | None = None
        # At most one fan-out in flight. Textual's message queue is unbounded, so
        # a held `r` would otherwise queue an arbitrary number of complete
        # network fan-outs and the account would draw 429s from its own UI.
        self._busy = False
        # Our own executor, not asyncio's shared default: its threads are
        # non-daemon and asyncio.run joins them at teardown, so `q` would leave
        # the process alive and unresponsive for the rest of an in-flight fetch.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vibe-collect")
        # Set once the first cycle has painted. The first refresh is deliberately
        # off the message pump, so "mounted" and "showing something" are two
        # different moments and a caller that needs the second must wait for it.
        self.first_paint = asyncio.Event()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="accounts"):
            yield Static(_EMPTY, id="empty")
        yield Static(id="footer")

    async def on_mount(self) -> None:
        self._conn = history.connect(store.history_path(self._ctx.root))
        # The timer first, then the first fetch off the message pump: awaiting the
        # whole fan-out here leaves a blank window that queues every keystroke,
        # `q` and ctrl-c included, for as long as the slowest account takes.
        self.set_interval(REFRESH_S, self._tick)
        self.call_after_refresh(self._tick)

    def on_unmount(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self._conn is not None:
            self._conn.close()

    async def _tick(self) -> None:
        # The only clock read. refresh_cycle takes its time from the Context, so a
        # test drives it as fast as it likes without sleeping.
        self._ctx.now_s = self._ctx.clock()
        await self._refresh_or_report()

    async def _refresh_or_report(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            await self.refresh_cycle()
        except Exception as exc:
            # The window stays up for days and the usage endpoint is documented
            # to answer 429 under sustained polling, so one failed poll reports
            # itself and the next one is 60 s away. Anything collect() already
            # classifies arrives as a per-account message instead.
            self.query_one("#footer", Static).update(
                Content(scrub(f"refresh failed ({type(exc).__name__}) — run: vibe list")),
                layout=False,
            )
        finally:
            self._busy = False
            self.first_paint.set()

    async def action_refresh_now(self) -> None:
        await self._tick()

    async def refresh_cycle(self) -> None:
        ctx = self._ctx
        now_s = ctx.now_s
        # collect() refreshes tokens and fetches usage: off the event loop, or
        # the dashboard stops answering keys for the length of a timeout.
        loop = asyncio.get_running_loop()
        views = await loop.run_in_executor(
            self._pool,
            partial(
                envelope.collect,
                ctx.root,
                client=ctx.client,
                port=ctx.port,
                now_s=now_s,
                port_for=ctx.port_for,
            ),
        )
        # Cursor seats: cached for two minutes inside read_cursor, so a one-minute refresh makes
        # a request at most every other cycle. Before the publish, so status.json has them.
        readings = await loop.run_in_executor(self._pool, partial(ctx.cursor_readings, now_s=now_s))
        codex_seen = await loop.run_in_executor(
            self._pool, partial(ctx.codex_readings, now_s=now_s)
        )
        agy_seen = await loop.run_in_executor(
            self._pool, partial(ctx.antigravity_entries, now_s=now_s)
        )
        # It reads the Keychain to say who is live, so off the event loop as well.
        await loop.run_in_executor(
            self._pool, partial(auto.publish, ctx.root, port=ctx.port, now_s=now_s)
        )
        weeks = envelope.pool_weeks(views)
        forecast = None
        if self._conn is not None:
            # A cycle where an account failed to fetch under-reports the pool;
            # storing it would read back as a real collapse and poison dry_in.
            if views and all(view.summary is not None for view in views):
                history.record(self._conn, at_s=now_s, pool=weeks)
            forecast = dry_in(history.samples(self._conn, since_s=now_s - _WINDOW_S))
        rebuilt = await self._reshape(views, readings, codex_seen, agy_seen)
        # A panel's height follows its shape, so a cycle that only changes
        # values — a bar, a percent, a reset time — needs no layout pass.
        for view in views:
            self.panels[view.alias].update(account_content(view, now_s=now_s), layout=rebuilt)
        for reading in readings:
            self.panels[reading.alias].update(cursor_content(reading, now_s=now_s), layout=rebuilt)
        for seen in codex_seen:
            self.panels[seen.alias].update(codex_content(seen, now_s=now_s), layout=rebuilt)
        for entry in agy_seen:
            content = antigravity_content(entry, now_s=now_s)
            self.panels[entry["alias"]].update(content, layout=rebuilt)
        self.query_one("#footer", Static).update(
            footer_content(
                weeks=weeks,
                accounts=len(views),
                forecast=forecast,
            ),
            layout=False,
        )

    async def _reshape(
        self,
        views: Sequence[AccountView],
        readings: Sequence[launch.CursorReading],
        codex_seen: Sequence[launch.CodexReading] = (),
        agy_seen: Sequence[antigravity.Entry] = (),
    ) -> bool:
        shapes = (
            [_shape(view) for view in views]
            + [_cursor_shape(r) for r in readings]
            + [_codex_shape(r) for r in codex_seen]
            + [
                (
                    entry["alias"],
                    False,
                    tuple(w["group"] + w["period"] for w in entry["windows"]),
                )
                for entry in agy_seen
            ]
        )
        if shapes == self._shapes:
            return False
        self._shapes = shapes
        for panel in self.panels.values():
            await panel.remove()
        aliases = (
            [view.alias for view in views]
            + [r.alias for r in readings]
            + [r.alias for r in codex_seen]
            + [entry["alias"] for entry in agy_seen]
        )
        self.panels = {alias: Static(classes="account") for alias in aliases}
        self.query_one("#empty", Static).display = not aliases
        await self.query_one("#accounts", VerticalScroll).mount_all(self.panels.values())
        return True


def run(ctx: Context) -> int:
    Dashboard(ctx).run()
    return 0
