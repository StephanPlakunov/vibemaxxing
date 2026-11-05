"""A policy step whose weekly limit is today's budget (`"weekly_budget": true`).

`main until 99% (weekly: today's budget) -> extra until 99% -> main until 99%, then earliest reset`
is the policy for one shared login: main while its week is within the budget `vibe run auto`
routes by (Monday..Sunday 20/40/60/80/100/100/100, Europe/Berlin), extra next, main again when
extra is spent. The days below are Berlin days, whatever this machine's local time is.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.test_auto import World, cache, payload, world
from tests.test_policy import HOUR, reading
from vibemaxxing import auto, cli, pool, route, store
from vibemaxxing.pool import (
    Budget,
    Decision,
    LastSwitch,
    Policy,
    Reading,
    Settings,
    Step,
    decide,
    limited_until,
    weekly_limit,
)

BERLIN = ZoneInfo("Europe/Berlin")
BUDGETED = Policy((Step("main", 99, weekly_budget=True), Step("extra", 99), Step("main", 99)))
ON = Settings(auto_switch=True, policy=BUDGETED)
DOCUMENT = {
    "mode": "ordered",
    "steps": [
        {"account": "main", "until": 99, "weekly_budget": True},
        {"account": "extra", "until": 99},
        {"account": "main", "until": 99},
    ],
    "then": "earliest_reset",
}


def berlin(day: int, hour: int = 12, minute: int = 0) -> float:
    """2026-10-<day> in Berlin: Mon 26, Tue 27, Wed 28, Thu 29 (budget 80), Fri 30 (100)."""
    return datetime(2026, 10, day, hour, minute, tzinfo=BERLIN).timestamp()


THU, FRI = berlin(29), berlin(30)


@pytest.fixture(autouse=True)
def local_time_is_not_berlin(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The budget's days are the routing's timezone's; local midnight must not be what counts.
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def decision(*readings: Reading, live: str, now: float, last: LastSwitch | None = None) -> Decision:
    return decide(list(readings), settings=ON, live=live, now_s=now, last=last)


# --- the decision ---------------------------------------------------------------------------------


def test_main_within_todays_budget_stays_on_main() -> None:
    got = decision(
        reading("main", 10, 75, now=THU), reading("extra", 5, 5, now=THU), live="main", now=THU
    )

    assert (got.action, got.reason) == (
        "stay",
        "main is below 99% and today's 80% weekly budget (step 1 of 3)",
    )


def test_main_over_todays_budget_goes_to_extra_and_is_recorded_at_the_budget() -> None:
    got = decision(
        reading("main", 10, 82, now=THU), reading("extra", 5, 5, now=THU), live="main", now=THU
    )

    assert (got.action, got.target) == ("switch", "extra")
    assert got.reason == "main at 82% weekly, over today's 80% weekly budget"
    # The fence is as wide as the budget: main's later 99% step stays open.
    assert (got.limit, got.weekly, got.threshold, got.zone) == (80, 80, 99, "Europe/Berlin")


def test_on_friday_the_budget_is_above_the_step_and_not_named() -> None:
    got = decision(
        reading("main", 10, 90, now=FRI), reading("extra", 5, 5, now=FRI), live="main", now=FRI
    )

    assert got.reason == "main is below 99% (step 1 of 3)"


def test_the_session_row_still_uses_the_steps_until() -> None:
    got = decision(
        reading("main", 99, 30, now=THU), reading("extra", 5, 5, now=THU), live="main", now=THU
    )

    assert (got.action, got.target, got.reason) == ("switch", "extra", "main at 99% session")
    assert (got.limit, got.threshold, got.zone) == (99, None, None)


def test_a_session_just_below_99_is_within_the_step() -> None:
    got = decision(
        reading("main", 98, 30, now=THU), reading("extra", 5, 5, now=THU), live="main", now=THU
    )

    assert got.action == "stay"


def left_on_thursday(at: float) -> LastSwitch:
    """What the tick records when it leaves main at 82% on Thursday (see the tick test)."""
    rows = reading("main", 10, 82, now=at).rows
    assert rows is not None
    until = limited_until(rows, 99, at, weekly=80, timezone="Europe/Berlin")
    return LastSwitch("main", "extra", at, "main at 82% weekly", until, 80)


def test_on_friday_the_budget_brings_main_back_after_the_cooldown() -> None:
    last = left_on_thursday(berlin(29, 23, 55))
    assert last.until_s == berlin(30, 0, 0)  # Berlin midnight, not the weekly reset

    def at(now: float) -> Decision:
        return decision(
            reading("main", 10, 82, now=now),
            reading("extra", 5, 5, now=now),
            live="extra",
            now=now,
            last=last,
        )

    assert at(berlin(29, 23, 59)).reason == "extra is below 99% (step 2 of 3)"  # still Thursday
    assert at(berlin(30, 0, 1)).reason == "cooldown: last switch 360s ago"
    back = at(berlin(30, 0, 11))
    assert (back.action, back.target) == ("switch", "main")
    assert back.reason == "main is at 82%, below its 99% step"


def test_main_left_over_the_budget_is_still_there_when_extra_is_spent() -> None:
    # Thursday: main left at 82%; an hour later extra hits 99%. main's 99% step is open: its week
    # is only over the budget, which the later step does not count.
    last = left_on_thursday(THU - HOUR)

    got = decision(
        reading("main", 10, 82, now=THU),
        reading("extra", 99, 50, now=THU),
        live="extra",
        now=THU,
        last=last,
    )

    assert (got.action, got.target, got.reason) == ("switch", "main", "extra at 99% session")


def test_both_spent_go_to_the_earliest_reset() -> None:
    main = reading("main", 99, 50, session_reset_s=4 * HOUR, now=THU)
    extra = reading("extra", 100, 60, session_reset_s=1 * HOUR, now=THU)

    on_main = decision(main, extra, live="main", now=THU)
    on_extra = decision(main, extra, live="extra", now=THU)

    assert (on_main.action, on_main.target) == ("switch", "extra")
    assert on_main.reason == "every account is at its limit; extra resets first"
    assert (on_extra.action, on_extra.reason) == (
        "stay",
        "every account is at its limit; extra resets first",
    )


def test_with_no_later_step_main_over_the_budget_is_back_at_berlin_midnight() -> None:
    only = Settings(
        auto_switch=True, policy=Policy((Step("main", 99, weekly_budget=True), Step("extra", 99)))
    )
    late = berlin(29, 22)  # 22:00 Berlin: two hours to Friday's budget
    main = reading("main", 10, 82, now=late)
    extra = reading("extra", 99, 10, session_reset_s=5 * HOUR, now=late)

    got = decide([main, extra], settings=only, live="extra", now_s=late, last=None)

    assert (got.action, got.target) == ("switch", "main")
    assert got.reason == "every account is at its limit; main resets first"


def test_the_budget_follows_the_routing_and_its_timezone() -> None:
    custom = Policy(
        (Step("main", 99, weekly_budget=True),), budget=Budget((5, 5, 5, 50, 5, 5, 5), "Asia/Tokyo")
    )
    step = custom.steps[0]
    # Thursday 20:00 Berlin is Friday 03:00 in Tokyo.
    assert weekly_limit(step, berlin(29, 20), custom.budget) == 5
    assert weekly_limit(step, berlin(29, 12), custom.budget) == 50
    assert weekly_limit(Step("main", 70, weekly_budget=True), FRI) == 70  # never above until


def test_the_budget_defaults_are_the_routings() -> None:
    assert pool.Budget() == Budget(route.DEFAULT_BUDGET, route.DEFAULT_TIMEZONE)
    routing = route.Routing()
    sunday = datetime(2026, 11, 1, 23, 59, tzinfo=BERLIN).timestamp()
    for now in (berlin(26, 0, 0), THU, FRI, berlin(31, 23, 59), sunday):
        assert Budget().today(now) == route.today_budget(routing, now)


# --- settings.json --------------------------------------------------------------------------------


def _write(root: Path, document: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    auto.settings_path(root).write_text(json.dumps(document))


def test_the_flag_reads_and_writes_back(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    _write(root, {"schema": 1, "auto_switch": True, "policy": DOCUMENT})

    settings = auto.read_settings(root)
    auto.write_settings(root, settings)

    assert settings == ON
    assert json.loads(auto.settings_path(root).read_text())["policy"] == DOCUMENT


def test_the_budget_is_read_from_the_routing(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    routing = {"weekday_budget": [10, 20, 30, 40, 50, 60, 70], "timezone": "UTC"}
    _write(root, {"schema": 1, "auto_switch": True, "policy": DOCUMENT, "routing": routing})

    settings = auto.read_settings(root)
    auto.write_settings(root, settings)

    assert settings.policy.budget == Budget((10, 20, 30, 40, 50, 60, 70), "UTC")
    saved = json.loads(auto.settings_path(root).read_text())
    assert saved["routing"] == routing and "budget" not in saved["policy"]


@pytest.mark.parametrize("flag", ["yes", 1, None, [True]])
def test_a_flag_that_is_not_a_boolean_means_the_defaults(tmp_home: Path, flag: object) -> None:
    root = tmp_home / ".vibemaxxing"
    document = json.loads(json.dumps(DOCUMENT))
    document["steps"][0]["weekly_budget"] = flag
    _write(root, {"schema": 1, "auto_switch": True, "policy": document})

    assert auto.read_settings(root) == Settings()


def test_a_budget_and_a_weekly_cap_on_one_step_mean_the_defaults(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    document = json.loads(json.dumps(DOCUMENT))
    document["steps"][0]["weekly_cap"] = [50] * 7
    _write(root, {"schema": 1, "auto_switch": True, "policy": document})

    assert auto.read_settings(root) == Settings()


def test_a_wrong_routing_under_a_budget_step_means_the_defaults(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    wrong = {"weekday_budget": [80]}
    _write(root, {"schema": 1, "auto_switch": True, "policy": DOCUMENT, "routing": wrong})

    assert auto.read_settings(root) == Settings()


def test_a_wrong_routing_does_not_touch_a_policy_without_a_budget_step(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    plain = {**DOCUMENT, "steps": [{"account": "main", "until": 85}]}
    _write(root, {"schema": 1, "auto_switch": True, "policy": plain, "routing": {"x": 1}})

    assert auto.read_settings(root).policy.steps == (Step("main", 85),)


# --- the tick and the status ----------------------------------------------------------------------


def budget_world(
    tmp_home: Path, main: tuple[int, int], extra: tuple[int, int], *, live: str, now: float
) -> World:
    w = world(tmp_home, {"main": main, "extra": extra}, live=live)
    auto.write_settings(w.root, ON)
    for alias, (session, weekly) in (("main", main), ("extra", extra)):
        cache(w.root, alias, payload(session, weekly, now=now), fetched_at=now - 10.0)
    return w


def test_the_tick_leaves_main_over_the_budget_and_fences_until_berlin_midnight(
    tmp_home: Path,
) -> None:
    now = berlin(29, 23, 55)
    w = budget_world(tmp_home, (10, 82), (5, 5), live="main", now=now)

    result = w.tick(now)

    assert (
        result.line == "switched main -> extra: main at 82% weekly, over today's 80% weekly budget"
    )
    last = auto.read_last(w.root)
    assert last is not None
    assert (last.limit, last.until_s) == (80, berlin(30, 0, 0))

    for minute, line in ((6, "stay: cooldown: last switch 660s ago"), (11, None)):
        later = berlin(30, 0, minute)
        for alias, weekly in (("main", 82), ("extra", 5)):
            cache(
                w.root,
                alias,
                payload(10 if alias == "main" else 5, weekly, now=later),
                fetched_at=later - 10.0,
            )
        got = w.tick(later)
        if line is not None:
            assert got.line == line
        else:
            assert got.line == "switched extra -> main: main is at 82%, below its 99% step"
    assert store.read_active(w.root) == "main"


def test_status_names_todays_budget(tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    w = budget_world(tmp_home, (10, 10), (5, 5), live="main", now=THU)
    ctx = w.context()
    ctx.now_s = THU

    assert cli.main(["auto", "status"], context=ctx) == 0

    out = capsys.readouterr().out
    assert (
        "policy       ordered: main until 99% (weekly: today's budget 80%) -> extra until 99%"
        " -> main until 99%, then earliest reset\n"
    ) in out
    assert "step         1 of 3: main until 99% (weekly: today's budget 80%)\n" in out

    assert cli.main(["auto", "status", "--json"], context=ctx) == 0
    assert json.loads(capsys.readouterr().out)["policy"] == DOCUMENT
