"""Routing by need: every branch of both orders, the weekday budget and its day boundaries."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from vibemaxxing import auto, route
from vibemaxxing.errors import UsageError
from vibemaxxing.pool import Settings
from vibemaxxing.route import Candidate, Health, Routing, decide

BERLIN = ZoneInfo("Europe/Berlin")
# Tuesday 3 Nov 2026, 12:00 Berlin: the budget is 40%.
NOON_TUE = datetime(2026, 11, 3, 12, 0, tzinfo=BERLIN).timestamp()
NEEDS = ("slack", "atlassian")


def berlin(*args: int) -> float:
    return datetime(*args, tzinfo=BERLIN).timestamp()  # type: ignore[arg-type]


def main_(weekly: int | None = 30, health: Health = Health.OK) -> Candidate:
    return Candidate("main", "claude", health, "x", weekly, weekly is not None)


def extra(health: Health = Health.OK) -> Candidate:
    return Candidate("extra", "claude", health, "extra's numbers", 10, True)


def cursor_(health: Health = Health.OK) -> Candidate:
    return Candidate("cursor", "cursor", health, "included usage left")


def pick(
    cands: list[Candidate],
    *,
    needs: tuple[str, ...] = NEEDS,
    now_s: float = NOON_TUE,
    claude_only: bool = False,
    routing: Routing | None = None,
) -> route.Route:
    return decide(
        cands,
        routing=routing or Routing(),
        needs=needs,
        claude_only=claude_only,
        now_s=now_s,
    )


# --- a run that needs connectors: main (budget) -> cursor -> extra -> main --------------


def test_needs_main_within_budget_goes_to_main() -> None:
    chosen = pick([main_(40), cursor_(), extra()])  # 40% <= Tuesday's 40%

    assert (chosen.alias, chosen.warnings) == ("main", ())
    assert "within today's 40% budget" in chosen.reason


def test_needs_main_over_budget_goes_to_cursor() -> None:
    chosen = pick([main_(41), cursor_(), extra()])

    assert chosen.alias == "cursor"
    assert any("main: weekly 41% is over today's 40% budget" in line for line in chosen.skipped)


def test_needs_cursor_spent_goes_to_extra_without_connectors_and_warns() -> None:
    chosen = pick([main_(41), cursor_(Health.OUT), extra()])

    assert chosen.alias == "extra"
    assert chosen.warnings == ("extra has no atlassian, slack: this session runs without it",)


def test_needs_extra_spent_too_goes_to_main_regardless_of_budget() -> None:
    chosen = pick([main_(41), cursor_(Health.OUT), extra(Health.OUT)])

    assert chosen.alias == "main"
    assert chosen.reason.startswith("last resort")
    assert chosen.warnings == ()


def test_needs_main_spent_for_now_skips_to_cursor() -> None:
    chosen = pick([main_(10, Health.OUT), cursor_(), extra()])

    assert chosen.alias == "cursor"
    assert any(line.startswith("main: spent") for line in chosen.skipped)


def test_needs_unknown_usage_is_unavailable_and_says_so() -> None:
    unknown_main = Candidate("main", "claude", Health.UNKNOWN, "no numbers")
    chosen = pick([unknown_main, cursor_(), extra()])

    assert chosen.alias == "cursor"
    assert any("main: usage unknown, so not used" in line for line in chosen.skipped)
    unknown_cursor = Candidate("cursor", "cursor", Health.UNKNOWN, "usage unknown (HTTP 500)")
    chosen = pick([main_(90), unknown_cursor, extra()])
    assert chosen.alias == "extra"
    assert any(
        "cursor: usage unknown, so not used (usage unknown (HTTP 500))" in x for x in chosen.skipped
    )


def test_main_with_unknown_weekly_has_an_unknown_budget() -> None:
    chosen = pick([Candidate("main", "claude", Health.OK, "x", None, False), cursor_(), extra()])

    assert chosen.alias == "cursor"
    assert any("weekly usage unknown, so the budget is unknown" in line for line in chosen.skipped)
    stale = Candidate("main", "claude", Health.OK, "x", 10, False)
    assert pick([stale, cursor_(), extra()]).alias == "cursor"


def test_a_logged_out_cursor_is_skipped() -> None:
    dead = Candidate("cursor", "cursor", Health.DEAD, "not logged in: cursor-agent login")

    chosen = pick([main_(90), dead, extra()])

    assert chosen.alias == "extra"
    assert any("cursor: not logged in" in line for line in chosen.skipped)


def test_needs_m365_can_only_go_to_main() -> None:
    chosen = pick([main_(90), cursor_(), extra()], needs=("m365",))

    assert chosen.alias == "main"  # cursor lacks m365; extra may not degrade past it
    assert chosen.reason.startswith("last resort")
    assert any("cursor: has no m365" in line for line in chosen.skipped)
    assert any("extra: has no m365" in line for line in chosen.skipped)
    assert pick([main_(10), cursor_(), extra()], needs=("m365",)).alias == "main"
    # m365 together with slack: still main only
    assert pick([main_(90), cursor_(), extra()], needs=("slack", "m365")).alias == "main"


def test_needs_gcal_is_cursors_and_not_mains() -> None:
    chosen = pick([main_(10), cursor_(), extra()], needs=("gcal",))

    assert chosen.alias == "cursor"  # main has no gcal connector


def test_claude_only_never_picks_cursor() -> None:
    chosen = pick([main_(90), cursor_(), extra()], claude_only=True)

    assert chosen.alias == "extra"
    assert any("cursor: the program is Claude-only" in line for line in chosen.skipped)
    assert pick([main_(10), cursor_(), extra()], claude_only=True).alias == "main"


def test_the_last_resort_runs_on_unknown_or_spent_main_with_a_warning() -> None:
    unknown = Candidate("main", "claude", Health.UNKNOWN, "no numbers")
    chosen = pick([unknown, cursor_(Health.OUT), extra(Health.OUT)])
    assert chosen.alias == "main"
    assert chosen.warnings == ("main: usage unknown (no numbers)",)

    spent = main_(100, Health.OUT)
    chosen = pick([spent, cursor_(Health.OUT), extra(Health.OUT)])
    assert chosen.alias == "main" and "this may be refused" in chosen.warnings[0]


def test_a_dead_main_login_stops_even_the_last_resort() -> None:
    dead = Candidate("main", "claude", Health.DEAD, "the login for main has lapsed")

    with pytest.raises(UsageError) as caught:
        pick([dead, cursor_(Health.OUT), extra(Health.OUT)])

    assert "no account can take this run" in caught.value.message
    assert "main: the login for main has lapsed" in caught.value.message


# --- a run that needs nothing: extra -> main (budget) -> cursor -> main -----------------


def test_no_needs_extra_first() -> None:
    assert pick([main_(10), cursor_(), extra()], needs=()).alias == "extra"


def test_no_needs_extra_spent_main_within_budget() -> None:
    chosen = pick([main_(40), cursor_(), extra(Health.OUT)], needs=())

    assert chosen.alias == "main"


def test_no_needs_extra_spent_main_over_budget_goes_to_cursor() -> None:
    chosen = pick([main_(41), cursor_(), extra(Health.OUT)], needs=())

    assert chosen.alias == "cursor"


def test_no_needs_cursor_spent_too_goes_to_main_regardless() -> None:
    chosen = pick([main_(95), cursor_(Health.OUT), extra(Health.OUT)], needs=())

    assert chosen.alias == "main" and chosen.reason.startswith("last resort")


def test_no_needs_unknown_extra_is_skipped() -> None:
    chosen = pick([main_(10), cursor_(), extra(Health.UNKNOWN)], needs=())

    assert chosen.alias == "main"
    assert any("extra: usage unknown" in line for line in chosen.skipped)


def test_no_needs_claude_only_skips_cursor() -> None:
    chosen = pick([main_(95), cursor_(), extra(Health.OUT)], needs=(), claude_only=True)

    assert chosen.alias == "main" and chosen.reason.startswith("last resort")


def test_an_alias_the_order_names_but_nobody_has_is_skipped_by_name() -> None:
    chosen = pick([main_(10)], needs=())

    assert chosen.alias == "main"
    assert "extra: no such account" in chosen.skipped


# --- the weekday budget and its day boundaries ----------------------------------------------


@pytest.mark.parametrize(
    ("when", "budget"),
    [
        (berlin(2026, 11, 2, 0, 0), 20),  # Monday 00:00
        (berlin(2026, 11, 2, 23, 59), 20),  # Monday 23:59 still Monday's 20
        (berlin(2026, 11, 3, 0, 0), 40),  # Tuesday 00:00: the next share opens
        (berlin(2026, 11, 3, 23, 59), 40),
        (berlin(2026, 11, 4, 0, 0), 60),
        (berlin(2026, 11, 5, 12, 0), 80),
        (berlin(2026, 11, 6, 12, 0), 100),
        (berlin(2026, 11, 7, 12, 0), 100),  # Saturday
        (berlin(2026, 11, 8, 23, 59), 100),  # Sunday
        (berlin(2026, 11, 9, 0, 0), 20),  # the new week
    ],
)
def test_the_budget_follows_the_berlin_day(when: float, budget: int) -> None:
    assert route.today_budget(Routing(), when) == budget


def test_the_day_is_berlins_not_utc() -> None:
    # 23:30 UTC on Monday is 00:30 Tuesday in Berlin (CET, +1).
    utc_monday_late = datetime(2026, 11, 2, 23, 30, tzinfo=ZoneInfo("UTC")).timestamp()

    assert route.today_budget(Routing(), utc_monday_late) == 40
    # And a Berlin summer evening: 23:30 CEST Monday is 21:30 UTC Monday.
    assert route.today_budget(Routing(), berlin(2026, 7, 6, 23, 30)) == 20  # Monday


def test_a_day_boundary_flips_the_routing() -> None:
    cands = [main_(30), cursor_(), extra()]

    late_monday = berlin(2026, 11, 2, 23, 59)
    early_tuesday = berlin(2026, 11, 3, 0, 0)

    assert pick(cands, now_s=late_monday).alias == "cursor"  # 30% > Monday's 20%
    assert pick(cands, now_s=early_tuesday).alias == "main"  # 30% <= Tuesday's 40%


def test_dst_changes_do_not_shift_the_day() -> None:
    weekdays = Routing(weekday_budget=(10, 20, 30, 40, 50, 60, 70))
    # Spring forward: Sunday 29 Mar 2026, 02:00 CET -> 03:00 CEST.
    assert route.today_budget(weekdays, berlin(2026, 3, 28, 23, 59)) == 60  # Saturday
    assert route.today_budget(weekdays, berlin(2026, 3, 29, 0, 0)) == 70  # Sunday
    assert route.today_budget(weekdays, berlin(2026, 3, 29, 3, 30)) == 70  # after the jump
    assert route.today_budget(weekdays, berlin(2026, 3, 29, 23, 59)) == 70
    assert route.today_budget(weekdays, berlin(2026, 3, 30, 0, 0)) == 10  # Monday
    # Fall back: Sunday 25 Oct 2026, 03:00 CEST -> 02:00 CET (02:30 happens twice).
    first = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=0).timestamp()
    second = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=1).timestamp()
    assert second - first == 3600
    assert route.today_budget(weekdays, first) == route.today_budget(weekdays, second) == 70
    assert route.today_budget(weekdays, berlin(2026, 10, 25, 23, 59)) == 70
    assert route.today_budget(weekdays, berlin(2026, 10, 26, 0, 0)) == 10  # Monday, 23h later


def test_the_budget_list_and_timezone_are_configurable() -> None:
    custom = Routing(timezone="America/New_York", weekday_budget=(5, 5, 5, 5, 5, 5, 5))

    assert route.today_budget(custom, NOON_TUE) == 5
    new_york_late = datetime(2026, 11, 3, 3, 0, tzinfo=ZoneInfo("UTC")).timestamp()  # Mon 22:00 NY
    mixed = Routing(timezone="America/New_York", weekday_budget=(11, 22, 33, 44, 55, 66, 77))
    assert route.today_budget(mixed, new_york_late) == 11


# --- needs ---------------------------------------------------------------------------


def test_needs_come_from_the_flag_the_default_or_none() -> None:
    routing = Routing()

    assert route.needs_for(routing, needs=None, no_needs=False) == ("slack", "atlassian")
    assert route.needs_for(routing, needs=["m365", "m365"], no_needs=False) == ("m365",)
    assert route.needs_for(routing, needs=None, no_needs=True) == ()
    with pytest.raises(UsageError, match="unknown connector teams"):
        route.needs_for(routing, needs=["teams"], no_needs=False)
    custom = Routing(default_needs=())
    assert route.needs_for(custom, needs=None, no_needs=False) == ()


# --- settings.json routing -----------------------------------------------------------------


def write(root: Path, document: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text(json.dumps({"schema": 1, **document}))


def test_a_missing_file_or_member_is_the_defaults(tmp_path: Path) -> None:
    assert route.read_routing(tmp_path / "none") == Routing()
    write(tmp_path, {"auto_switch": True})
    assert route.read_routing(tmp_path) == Routing()


def test_routing_is_configurable_per_part(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "routing": {
                "timezone": "UTC",
                "weekday_budget": [10, 20, 30, 40, 50, 60, 70],
                "default_needs": ["gcal"],
                "connectors": {"main": ["slack"], "work": ["slack", "gcal"]},
                "order": {
                    "needs": [{"account": "work"}, {"account": "main", "last_resort": True}],
                    "no_needs": [{"account": "main", "budget": True, "degraded": True}],
                },
                "cursor_margin_cents": 500,
                "spent_percent": 95,
            }
        },
    )

    routing = route.read_routing(tmp_path)

    assert routing.timezone == "UTC" and routing.weekday_budget == (10, 20, 30, 40, 50, 60, 70)
    assert routing.default_needs == ("gcal",)
    assert routing.connectors["work"] == ("slack", "gcal") and routing.connectors["main"] == (
        "slack",
    )
    assert routing.connectors["cursor"] == ("slack", "atlassian", "gcal")  # untouched default
    assert routing.order["needs"] == (route.Step("work"), route.Step("main", last_resort=True))
    assert routing.order["no_needs"] == (route.Step("main", budget=True, degraded=True),)
    assert (routing.cursor_margin_cents, routing.spent_percent) == (500, 95)


@pytest.mark.parametrize(
    "bad",
    [
        "text",
        {"timezone": "Mars/Base"},
        {"timezone": 3},
        {"weekday_budget": [1, 2, 3]},
        {"weekday_budget": [20, 40, 60, 80, 100, 100, 101]},
        {"weekday_budget": [20, 40, 60, 80, 100, 100, True]},
        {"default_needs": ["teams"]},
        {"default_needs": "slack"},
        {"connectors": {"main": ["teams"]}},
        {"connectors": ["main"]},
        {"order": {"always": []}},
        {"order": {"needs": []}},
        {"order": {"needs": [{"budget": True}]}},
        {"order": {"needs": [{"account": "main", "colour": "red"}]}},
        {"order": {"needs": [{"account": "main", "budget": "yes"}]}},
        {"cursor_margin_cents": -1},
        {"spent_percent": 10},
        {"surprise": 1},
    ],
)
def test_a_wrong_routing_is_refused_not_guessed(tmp_path: Path, bad: object) -> None:
    write(tmp_path, {"routing": bad})

    with pytest.raises(UsageError, match="routing"):
        route.read_routing(tmp_path)


def test_a_file_that_is_not_json_is_refused(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{nope")

    with pytest.raises(UsageError, match="not valid JSON"):
        route.read_routing(tmp_path)


def test_toggling_auto_switch_keeps_the_routing(tmp_path: Path) -> None:
    write(tmp_path, {"routing": {"weekday_budget": [1, 2, 3, 4, 5, 6, 7]}})

    auto.write_settings(tmp_path, Settings(auto_switch=True))

    assert json.loads((tmp_path / "settings.json").read_text())["routing"] == {
        "weekday_budget": [1, 2, 3, 4, 5, 6, 7]
    }
    assert auto.read_settings(tmp_path).auto_switch is True  # auto-switch ignores the member
    assert route.read_routing(tmp_path).weekday_budget == (1, 2, 3, 4, 5, 6, 7)
