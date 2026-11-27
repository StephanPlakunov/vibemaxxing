"""One row shape for every account type, in `vibe usage` and on the status line."""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime

import pytest

from vibemaxxing import cli, display, launch, statusline
from vibemaxxing.codex_usage import CodexUsage, Spend, Window
from vibemaxxing.cursor_usage import CursorUsage, availability
from vibemaxxing.envelope import AccountView
from vibemaxxing.models import AccountState
from vibemaxxing.usage import Row, Summary

NOW = 1_795_000_000.0
DAY = 86_400.0
# label, bar, percent used, then the amount and/or the reset: the same for every type.
ROW = re.compile(
    r"^    (?P<label>\S.*?)\s+(?P<bar>[█░]{20}) +(?P<percent>\d+)%(?:  (?P<tail>.+))?$"
)


def _iso(at_s: float) -> str:
    return datetime.fromtimestamp(at_s, UTC).isoformat(timespec="seconds")


def claude_view(*rows: Row, alias: str = "main") -> AccountView:
    from vibemaxxing.credentials import EMPTY_IDENTITY

    return AccountView(
        alias, False, AccountState.OK, None, EMPTY_IDENTITY, "max 20x", Summary(rows, ()), NOW
    )


def cursor_reading(included: float, used: int, cap: int) -> launch.CursorReading:
    usage = CursorUsage(included, used, cap, NOW + 6 * DAY)
    return launch.CursorReading("cursor", True, None, None, usage, "", availability(usage))


def codex_reading(usage: CodexUsage) -> launch.CodexReading:
    return launch.CodexReading("codex", "ok", None, "plus", usage, "")


def agy_entry() -> dict[str, object]:
    return {
        "alias": "agy",
        "kind": "antigravity",
        "message": None,
        "fetched_at": NOW,
        "state": "ok",
        "windows": (
            {
                "group": "Gemini",
                "period": "wk",
                "remaining_percent": 1.0,
                "resets_at": _iso(NOW + 6 * DAY),
            },
            {
                "group": "Gemini",
                "period": "5h",
                "remaining_percent": 12.0,
                "resets_at": _iso(NOW + 35 * 60),
            },
            {
                "group": "Claude/GPT",
                "period": "wk",
                "remaining_percent": 0.0,
                "resets_at": _iso(NOW + 6 * DAY),
            },
        ),
    }


def every_row() -> list[str]:
    lines = cli._render_accounts(
        [
            claude_view(
                Row(
                    "monthly_spend",
                    "Monthly spend $125.03 of $125",
                    100,
                    "critical",
                    None,
                    125.03,
                    125.0,
                    "USD",
                ),
                alias="claude",
            ),
            claude_view(
                Row("weekly_all", "Weekly · all models", 98, "critical", _iso(NOW + 2 * DAY)),
                Row("weekly_scoped", "Weekly · Fable", 0, "normal", _iso(NOW + 2 * DAY)),
            ),
        ],
        now_s=NOW,
        dry=None,
    ).splitlines()
    lines += cli._render_cursor(cursor_reading(33.06, 75_017, 75_000), now_s=NOW, colour=False)
    lines += cli._render_codex(
        codex_reading(
            CodexUsage(
                "plus",
                (Window("5h", 12.0, 300, NOW + 3600),),
                spend=Spend("credit", 5100.0, 5100.0, 100.0, NOW + 3 * DAY),
            )
        ),
        now_s=NOW,
        colour=False,
    )
    lines += cli._render_antigravity(agy_entry(), now_s=NOW, colour=False)  # type: ignore[arg-type]
    return lines


def test_every_limit_of_every_type_is_one_row_of_the_same_shape() -> None:
    lines = every_row()
    rows = [line for line in lines if "█" in line or "░" in line]

    assert len(rows) == 10
    for line in rows:
        assert ROW.match(line), line
    # The bars start in the same column whatever the type.
    assert len({line.index(next(c for c in line if c in "█░")) for line in rows}) == 1
    text = "\n".join(lines)
    assert "Monthly spend" in text and " 100%  $125.03 of $125" in text
    assert "On-demand" in text and " 100%  $750.17 of $750  resets in 6d" in text
    assert "Included + bonus" in text and "  33%  resets in 6d" in text
    assert "Credits" in text and " 100%  5,100 of 5,100 credits  resets in 3d" in text
    assert "Weekly · all models" in text and "  98%  resets in 2d" in text


def test_antigravity_shows_the_share_used_with_its_short_window_first() -> None:
    lines = cli._render_antigravity(agy_entry(), now_s=NOW, colour=False)  # type: ignore[arg-type]

    assert lines[0] == "  agy  Antigravity"
    assert [ROW.match(line).group("label", "percent", "tail") for line in lines[1:]] == [  # type: ignore[union-attr]
        ("5h · Gemini", "88", "resets in 35m"),
        ("Weekly · Gemini", "99", "resets in 6d"),
        ("Weekly · Claude/GPT", "100", "resets in 6d"),
    ]
    assert "left" not in "\n".join(lines)


def test_the_level_comes_from_the_percent_for_every_type() -> None:
    assert display.level(display.Limit("x", 79.0)) == "ok"
    assert display.level(display.Limit("x", 80.0)) == "high"
    assert display.level(display.Limit("x", 100.0)) == "full"
    assert display.level(display.Limit("x", 10.0, flagged=True)) == "high"
    assert display.level(display.Limit("x", None)) == "ok"


def test_colour_only_on_a_terminal_and_never_with_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert display.use_colour(Tty()) is True
    assert display.use_colour(io.StringIO()) is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert display.use_colour(Tty()) is False

    full = display.Limit("x", 100.0)
    assert display.coloured(full, "row", colour=True) == "\x1b[31mrow\x1b[0m"
    assert display.coloured(display.Limit("x", 85.0), "row", colour=True) == "\x1b[33mrow\x1b[0m"
    assert display.coloured(display.Limit("x", 5.0), "row", colour=True) == "row"
    assert display.coloured(full, "row", colour=False) == "row"


def test_money_drops_empty_cents() -> None:
    assert display.money(750.0) == "$750"
    assert display.money(750.17) == "$750.17"
    assert display.money(1250.5) == "$1,250.50"
    assert display.money(3.0, "EUR") == "EUR 3"


# --- the status line: one cell shape for every account ------------------------------------------


CELL = re.compile(
    r"^[\w.-]+: \d+%\??(?: \((?:on-demand spent|resets [^)]+)(?:, resets [^)]+)?\))?$"
)


def status_document() -> dict[str, object]:
    return {
        "schema": 1,
        "accounts": [
            {
                "alias": "main",
                "state": "ok",
                "fetched_at": NOW - 30,
                "rows": [
                    {"kind": "session", "percent": 40, "resets_at": NOW + 3600},
                    {"kind": "weekly_all", "percent": 97, "resets_at": NOW + 2 * DAY},
                ],
            },
            {
                "alias": "claude",
                "state": "ok",
                "fetched_at": NOW - 30,
                "rows": [
                    {
                        "kind": "monthly_spend",
                        "percent": 100,
                        "spend_used": 125.03,
                        "spend_limit": 125.0,
                        "resets_at": None,
                    },
                ],
            },
        ],
        "cursor": [
            {
                "alias": "cursor",
                "fetched_at": NOW - 30,
                "included_percent": 33.06,
                "on_demand_used_cents": 75_017,
                "on_demand_cap_cents": 75_000,
                "cycle_end_s": NOW + 6 * DAY,
                "available": False,
            },
        ],
        "codex": [
            {
                "alias": "codex",
                "fetched_at": NOW - 30,
                "windows": [{"label": "5h", "percent": 12, "resets_at_s": NOW + 3600}],
                "spend": {
                    "used": 5100.0,
                    "limit": 5100.0,
                    "percent": 100.0,
                    "resets_at_s": NOW + 3 * DAY,
                },
            },
        ],
        "antigravity": [{**agy_entry(), "windows": list(agy_entry()["windows"])}],  # type: ignore[call-overload]
    }


def test_every_account_on_the_status_line_has_the_same_shape() -> None:
    cells = statusline.render(status_document(), now_s=NOW).split(" · ")

    for cell in cells:
        assert CELL.match(cell), cell
    by_alias = dict(cell.split(": ", 1) for cell in cells)
    assert by_alias["main"].startswith("97% (resets ")  # the weekly window is the binding one
    assert by_alias["agy"].startswith("99% (resets ")  # Gemini still has room; Claude/GPT none
    assert by_alias["cursor"] == "33% (on-demand spent)"  # included, not "spent"
    assert by_alias["codex"].startswith("100% (resets ")
    assert by_alias["claude"] == "100%"  # monthly spend reports no reset
    assert [cell.split(":")[0] for cell in cells] == ["main", "agy", "cursor", "codex", "claude"]


def test_a_codex_window_that_rolled_over_is_left_out() -> None:
    doc = status_document()
    codex = doc["codex"][0]  # type: ignore[index]
    codex["spend"] = None
    codex["windows"] = [
        {"label": "5h", "percent": 100, "resets_at_s": NOW - 1},
        {"label": "weekly", "percent": 20, "resets_at_s": NOW + DAY},
    ]

    assert "codex: 20%" in statusline.render(doc, now_s=NOW).split(" · ")


def test_a_codex_spend_pool_that_rolled_over_is_left_out() -> None:
    doc = status_document()
    codex = doc["codex"][0]  # type: ignore[index]
    assert isinstance(codex, dict)
    codex["spend"] = {
        "percent": 100,
        "used": 5100,
        "limit": 5100,
        "resets_at_s": NOW - 1,
    }
    codex["windows"] = [{"label": "weekly", "percent": 20, "resets_at_s": NOW + DAY}]

    assert "codex: 20%" in statusline.render(doc, now_s=NOW).split(" · ")


def test_on_demand_off_is_flagged_when_routing_refuses_the_seat() -> None:
    usage = CursorUsage(33, 100, 0, NOW + 6 * DAY)
    reading = launch.CursorReading("cursor", True, None, None, usage, "", availability(usage))
    on_demand = next(l for l in display.cursor_limits(reading) if l.label == "On-demand")

    assert on_demand.percent is None
    assert on_demand.flagged
    assert on_demand.amount is not None and "(off)" in on_demand.amount


def test_relative_countdowns_floor_partial_units() -> None:
    assert display.relative(NOW + 5400, NOW) == "resets in 1h"
    assert display.relative(NOW + 3599, NOW) == "resets in 59m"
    assert display.relative(NOW + 2 * DAY + DAY // 2, NOW) == "resets in 2d"
    assert display.relative(NOW + 86_400 - 1, NOW) == "resets in 23h"


def test_antigravity_with_every_group_used_up_shows_the_first_reset_of_the_best_group() -> None:
    entry = {
        "alias": "agy",
        "fetched_at": NOW - 30,
        "windows": [
            {
                "group": "Gemini",
                "period": "wk",
                "remaining_percent": 0.0,
                "resets_at": _iso(NOW + 6 * DAY),
            },
            {
                "group": "Claude/GPT",
                "period": "5h",
                "remaining_percent": 0.0,
                "resets_at": _iso(NOW + 2 * 3600),
            },
        ],
    }

    text = statusline._antigravity(entry, NOW)

    assert text is not None and text.startswith("agy: 100% (resets ")


def test_tied_antigravity_groups_pick_the_earliest_reset() -> None:
    hour = 3600
    entry = {
        "alias": "agy",
        "fetched_at": NOW - 30,
        "windows": [
            {
                "group": "Gemini",
                "period": "wk",
                "remaining_percent": 0.0,
                "resets_at": _iso(NOW + 6 * DAY),
            },
            {
                "group": "Gemini",
                "period": "5h",
                "remaining_percent": 0.0,
                "resets_at": _iso(NOW + 5 * hour),
            },
            {
                "group": "Claude/GPT",
                "period": "wk",
                "remaining_percent": 100.0,
                "resets_at": _iso(NOW + 6 * DAY),
            },
            {
                "group": "Claude/GPT",
                "period": "5h",
                "remaining_percent": 0.0,
                "resets_at": _iso(NOW + hour),
            },
        ],
    }

    text = statusline._antigravity(entry, NOW)

    assert text is not None
    assert f"resets {statusline._when(NOW + hour, NOW)}" in text
