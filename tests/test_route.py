"""Routing by need: every branch of both orders. Routing reads only what each account reports."""

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


def main_(health: Health = Health.OK) -> Candidate:
    return Candidate("main", "claude", health, "x")


def extra(health: Health = Health.OK) -> Candidate:
    return Candidate("extra", "claude", health, "extra's numbers")


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


# --- a run that needs connectors: main -> cursor -> extra -> main --------------


def test_needs_main_with_room_goes_to_main_on_any_day() -> None:
    # No pacing budget: only what main itself reports decides, so every weekday is the same.
    for day in (berlin(2026, 11, 2, 0, 0), NOON_TUE, berlin(2026, 11, 8, 23, 59)):
        chosen = pick([main_(), cursor_(), extra()], now_s=day)
        assert (chosen.alias, chosen.warnings, chosen.reason) == ("main", (), "x")


def test_needs_cursor_spent_goes_to_extra_without_connectors_and_warns() -> None:
    chosen = pick([main_(Health.OUT), cursor_(Health.OUT), extra()])

    assert chosen.alias == "extra"
    assert chosen.warnings == ("extra has no atlassian, slack: this session runs without it",)


def test_needs_main_spent_for_now_skips_to_cursor() -> None:
    chosen = pick([main_(Health.OUT), cursor_(), extra()])

    assert chosen.alias == "cursor"
    assert any(line.startswith("main: spent") for line in chosen.skipped)


def test_needs_unknown_usage_is_unavailable_and_says_so() -> None:
    unknown_main = Candidate("main", "claude", Health.UNKNOWN, "no numbers")
    chosen = pick([unknown_main, cursor_(), extra()])

    assert chosen.alias == "cursor"
    assert any("main: usage unknown, so not used" in line for line in chosen.skipped)
    unknown_cursor = Candidate("cursor", "cursor", Health.UNKNOWN, "usage unknown (HTTP 500)")
    chosen = pick([main_(Health.OUT), unknown_cursor, extra()])
    assert chosen.alias == "extra"
    assert any(
        "cursor: usage unknown, so not used (usage unknown (HTTP 500))" in x for x in chosen.skipped
    )


def test_a_logged_out_cursor_is_skipped() -> None:
    dead = Candidate("cursor", "cursor", Health.DEAD, "not logged in: cursor-agent login")

    chosen = pick([main_(Health.OUT), dead, extra()])

    assert chosen.alias == "extra"
    assert any("cursor: not logged in" in line for line in chosen.skipped)


def test_needs_m365_can_only_go_to_main() -> None:
    chosen = pick([main_(Health.OUT), cursor_(), extra()], needs=("m365",))

    assert chosen.alias == "main"  # cursor lacks m365; extra may not degrade past it
    assert chosen.reason.startswith("last resort")
    assert any("cursor: has no m365" in line for line in chosen.skipped)
    assert any("extra: has no m365" in line for line in chosen.skipped)
    assert pick([main_(), cursor_(), extra()], needs=("m365",)).alias == "main"
    # m365 together with slack: still main only
    assert pick([main_(Health.OUT), cursor_(), extra()], needs=("slack", "m365")).alias == "main"


def test_needs_gcal_is_cursors_and_not_mains() -> None:
    chosen = pick([main_(), cursor_(), extra()], needs=("gcal",))

    assert chosen.alias == "cursor"  # main has no gcal connector


def test_claude_only_never_picks_cursor() -> None:
    chosen = pick([main_(Health.OUT), cursor_(), extra()], claude_only=True)

    assert chosen.alias == "extra"
    assert any("cursor: the program is Claude-only" in line for line in chosen.skipped)
    assert pick([main_(), cursor_(), extra()], claude_only=True).alias == "main"


def test_the_last_resort_runs_on_unknown_or_spent_main_with_a_warning() -> None:
    unknown = Candidate("main", "claude", Health.UNKNOWN, "no numbers")
    chosen = pick([unknown, cursor_(Health.OUT), extra(Health.OUT)])
    assert chosen.alias == "main"
    assert chosen.warnings == ("main: usage unknown (no numbers)",)

    spent = main_(Health.OUT)
    chosen = pick([spent, cursor_(Health.OUT), extra(Health.OUT)])
    assert chosen.alias == "main" and "this may be refused" in chosen.warnings[0]


def test_a_dead_main_login_stops_even_the_last_resort() -> None:
    dead = Candidate("main", "claude", Health.DEAD, "the login for main has lapsed")

    with pytest.raises(UsageError) as caught:
        pick([dead, cursor_(Health.OUT), extra(Health.OUT)])

    assert "no account can take this run" in caught.value.message
    assert "main: the login for main has lapsed" in caught.value.message


# --- a run that needs nothing: extra -> main -> cursor -> main -----------------


def test_no_needs_extra_first() -> None:
    assert pick([main_(), cursor_(), extra()], needs=()).alias == "extra"


def test_no_needs_extra_spent_main_with_room() -> None:
    chosen = pick([main_(), cursor_(), extra(Health.OUT)], needs=())

    assert chosen.alias == "main"


def test_no_needs_extra_spent_main_spent_goes_to_cursor() -> None:
    chosen = pick([main_(Health.OUT), cursor_(), extra(Health.OUT)], needs=())

    assert chosen.alias == "cursor"


def test_no_needs_everything_spent_goes_to_main_as_the_last_resort() -> None:
    chosen = pick([main_(Health.OUT), cursor_(Health.OUT), extra(Health.OUT)], needs=())

    assert chosen.alias == "main" and chosen.reason.startswith("last resort")


def test_no_needs_unknown_extra_is_skipped() -> None:
    chosen = pick([main_(), cursor_(), extra(Health.UNKNOWN)], needs=())

    assert chosen.alias == "main"
    assert any("extra: usage unknown" in line for line in chosen.skipped)


def test_no_needs_claude_only_skips_cursor() -> None:
    chosen = pick([main_(Health.OUT), cursor_(), extra(Health.OUT)], needs=(), claude_only=True)

    assert chosen.alias == "main" and chosen.reason.startswith("last resort")


def test_an_alias_the_order_names_but_nobody_has_is_skipped_by_name() -> None:
    chosen = pick([main_()], needs=())

    assert chosen.alias == "main"
    assert "extra: no such account" in chosen.skipped


# --- the removed pacing budget ---------------------------------------------------------------


def test_the_removed_budget_settings_are_still_read_and_change_nothing(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "routing": {
                "timezone": "UTC",
                "weekday_budget": [1, 2, 3, 4, 5, 6, 7],
                "order": {"needs": [{"account": "main", "budget": True}, {"account": "extra"}]},
            }
        },
    )

    routing = route.read_routing(tmp_path)

    assert routing.order["needs"] == (route.Step("main"), route.Step("extra"))
    assert not hasattr(routing, "weekday_budget") and not hasattr(route, "today_budget")


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
                "default_needs": ["gcal"],
                "connectors": {"main": ["slack"], "work": ["slack", "gcal"]},
                "order": {
                    "needs": [{"account": "work"}, {"account": "main", "last_resort": True}],
                    "no_needs": [{"account": "main", "degraded": True}],
                },
                "spent_percent": 95,
            }
        },
    )

    routing = route.read_routing(tmp_path)

    assert routing.default_needs == ("gcal",)
    assert routing.connectors["work"] == ("slack", "gcal") and routing.connectors["main"] == (
        "slack",
    )
    assert routing.connectors["cursor"] == ("slack", "atlassian", "gcal")  # untouched default
    assert routing.order["needs"] == (route.Step("work"), route.Step("main", last_resort=True))
    assert routing.order["no_needs"] == (route.Step("main", degraded=True),)
    assert routing.spent_percent == 95


@pytest.mark.parametrize(
    "bad",
    [
        "text",
        {"default_needs": ["teams"]},
        {"default_needs": "slack"},
        {"connectors": {"main": ["teams"]}},
        {"connectors": ["main"]},
        {"order": {"always": []}},
        {"order": {"needs": []}},
        {"order": {"needs": [{"budget": True}]}},
        {"order": {"needs": [{"account": "main", "colour": "red"}]}},
        {"order": {"needs": [{"account": "main", "budget": "yes"}]}},
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
    write(tmp_path, {"routing": {"default_needs": ["gcal"]}})

    auto.write_settings(tmp_path, Settings(auto_switch=True))

    assert json.loads((tmp_path / "settings.json").read_text())["routing"] == {
        "default_needs": ["gcal"]
    }
    assert auto.read_settings(tmp_path).auto_switch is True  # auto-switch ignores the member
    assert route.read_routing(tmp_path).default_needs == ("gcal",)


# --- roles: routing never depends on the literal names main / extra / cursor ---------------------


def cand(alias: str, kind: str = "claude", health: Health = Health.OK) -> Candidate:
    return Candidate(alias, kind, health, "x")


ROLES = Routing(roles={"main": "stephan", "extra": "shared", "cursor": "zvoove"})


def test_roles_map_the_order_onto_whatever_the_accounts_are_called() -> None:
    cands = [cand("stephan"), cand("shared"), cand("zvoove", "cursor")]

    assert pick(cands, needs=(), routing=ROLES).alias == "shared"  # the extra role first
    assert pick(cands, needs=NEEDS, routing=ROLES).alias == "stephan"  # main first
    over = [cand("stephan", health=Health.OUT), cand("shared"), cand("zvoove", "cursor")]
    chosen = pick(over, needs=NEEDS, routing=ROLES)
    assert chosen.alias == "zvoove" and chosen.kind == "cursor"


def test_connectors_follow_the_role_not_the_alias() -> None:
    cands = [cand("stephan", health=Health.OUT), cand("shared"), cand("zvoove", "cursor")]

    # m365 belongs to the main role: the alias "stephan" has it, "zvoove" (cursor role) does not
    assert pick(cands, needs=("m365",), routing=ROLES).alias == "stephan"
    assert pick(cands, needs=("gcal",), routing=ROLES).alias == "zvoove"


def test_a_misnamed_account_never_falls_through_to_cursor() -> None:
    """Review finding 4: accounts `stephan` and `shared`, default roles, one Cursor account."""
    cands = [cand("stephan"), cand("shared"), cand("cursor", "cursor")]

    for needs in ((), NEEDS):
        with pytest.raises(UsageError) as caught:
            pick(cands, needs=needs)  # main/extra name nothing; the order would end on Cursor
        message = caught.value.message
        assert "main -> 'main'" in message or "extra -> 'extra'" in message
        assert "naming mismatch" in message and "not starting it" in message
        assert caught.value.recovery is not None and "routing.roles" in caught.value.recovery


def test_with_the_roles_set_the_same_accounts_route_normally() -> None:
    cands = [cand("stephan"), cand("shared"), cand("cursor", "cursor")]

    assert (
        pick(
            cands,
            needs=(),
            routing=Routing(roles={"main": "stephan", "extra": "shared", "cursor": "cursor"}),
        ).alias
        == "shared"
    )


def test_a_missing_extra_role_is_skipped_but_cursor_is_still_refused_while_it_is_missing() -> None:
    # main is named right and has room: the run goes there; the missing extra is only reported
    no_extra = Routing(roles={"main": "stephan", "extra": "nobody", "cursor": "zvoove"})
    cands = [cand("stephan"), cand("zvoove", "cursor")]

    chosen = pick(cands, needs=(), routing=no_extra)
    assert chosen.alias == "stephan"
    assert any("extra (alias 'nobody'): no such account" in line for line in chosen.skipped)
    # main spent, so the order would reach Cursor: refused, because a Claude role is missing
    over = [cand("stephan", health=Health.OUT), cand("zvoove", "cursor")]
    with pytest.raises(UsageError, match="extra -> 'nobody'"):
        pick(over, needs=(), routing=no_extra)


def test_a_missing_cursor_role_just_skips_the_step() -> None:
    no_cursor = Routing(roles={"main": "stephan", "extra": "shared", "cursor": "nothing"})
    cands = [cand("stephan", health=Health.OUT), cand("shared", health=Health.OUT)]

    chosen = pick(cands, needs=NEEDS, routing=no_cursor)

    assert chosen.alias == "stephan" and chosen.reason.startswith("last resort")
    assert any("cursor (alias 'nothing'): no such account" in line for line in chosen.skipped)


def test_roles_are_validated(tmp_path: Path) -> None:
    for bad in (
        {"roles": "main"},
        {"roles": {"boss": "x"}},
        {"roles": {"main": 3}},
        {"roles": {"main": "Bad Alias"}},
        {"roles": {"main": "same", "extra": "same"}},
        {"order": {"needs": [{"account": "cursor", "last_resort": True}]}},
    ):
        write(tmp_path, {"routing": bad})
        with pytest.raises(UsageError, match="routing"):
            route.read_routing(tmp_path)
    # The removed cap and margin settings are still read, and change nothing.
    write(
        tmp_path,
        {
            "routing": {
                "roles": {"main": "stephan"},
                "cursor_cap_cents": 20_000,
                "cursor_margin_cents": 5,
            }
        },
    )
    routing = route.read_routing(tmp_path)
    assert routing.roles == {"main": "stephan", "extra": "extra", "cursor": "cursor"}
    assert not hasattr(routing, "cursor_cap_cents")
