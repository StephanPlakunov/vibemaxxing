"""The ordered failover policy: settings, `pool.decide` over it, the tick and the status.

`main until 85% -> extra until 99% -> main until 99%, then earliest reset` is the policy
the cases are written against; it is the one this fork was made for.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient, make_credential, seed_account
from tests.test_auto import NOW_S, World, cache, payload, world
from vibemaxxing import auto, cli, credentials, store, usage
from vibemaxxing.models import AccountState
from vibemaxxing.pool import (
    LastSwitch,
    Policy,
    Reading,
    Settings,
    Step,
    decide,
    next_candidate,
    ordered_steps,
)
from vibemaxxing.usage import Row

NOW = 1_800_000_000.0
HOUR = 3600.0
DAY = 86_400.0
POLICY = Policy((Step("main", 85), Step("extra", 99), Step("main", 99)))
ON = Settings(auto_switch=True, policy=POLICY)
BRIEF = {
    "mode": "ordered",
    "steps": [
        {"account": "main", "until": 85},
        {"account": "extra", "until": 99},
        {"account": "main", "until": 99},
    ],
    "then": "earliest_reset",
}


def _iso(at_s: float) -> str:
    return datetime.fromtimestamp(at_s, UTC).isoformat()


def reading(
    alias: str,
    session: int | None = 0,
    weekly: int | None = 0,
    *,
    fable: int | None = None,
    age_s: float = 10.0,
    session_reset_s: float = 3 * HOUR,
    weekly_reset_s: float = 2 * DAY,
    state: AccountState = AccountState.OK,
    identified: bool = True,
    now: float = NOW,
) -> Reading:
    found = [
        Row("session", "Session", session, "normal", _iso(now + session_reset_s)),
        Row("weekly_all", "Weekly · all models", weekly, "normal", _iso(now + weekly_reset_s)),
    ]
    if fable is not None:
        found.append(Row("weekly_scoped", "Weekly · Fable", fable, "normal", _iso(now + DAY)))
    return Reading(alias, state, tuple(found), now - age_s, identified)


def run(
    *readings: Reading,
    live: str | None,
    last: LastSwitch | None = None,
    now: float = NOW,
    settings: Settings = ON,
) -> tuple[str, str | None, str, int | None]:
    decision = decide(list(readings), settings=settings, live=live, now_s=now, last=last)
    return decision.action, decision.target, decision.reason, decision.limit


# --- the cases the fork was asked for --------------------------------------------


def test_main_at_50_stays_on_main() -> None:
    assert run(reading("main", 50, 50), reading("extra", 10, 10), live="main")[:3] == (
        "stay",
        None,
        "main is below 85% (step 1 of 3)",
    )


@pytest.mark.parametrize(
    ("session", "weekly", "why"), [(10, 85, "85% weekly"), (85, 40, "85% session")]
)
def test_main_at_85_goes_to_extra(session: int, weekly: int, why: str) -> None:
    action, target, reason, limit = run(
        reading("main", session, weekly), reading("extra", 10, 10), live="main"
    )

    assert (action, target, limit) == ("switch", "extra", 85)
    assert reason == f"main at {why}", "the notification reads 'Switched to extra: <reason>'"


def test_main_at_90_and_extra_at_99_is_main() -> None:
    on_main = run(reading("main", 10, 90), reading("extra", 99, 50), live="main")
    on_extra = run(reading("main", 10, 90), reading("extra", 99, 50), live="extra")

    assert on_main[:3] == ("stay", None, "main is below 99% (step 3 of 3)")
    assert on_extra == ("switch", "main", "extra at 99% session", 99)


def test_both_at_99_or_more_go_to_the_earliest_reset() -> None:
    main = reading("main", 99, 50, session_reset_s=4 * HOUR)
    extra = reading("extra", 100, 60, session_reset_s=1 * HOUR)

    assert run(main, extra, live="main") == (
        "switch",
        "extra",
        "every account is at its limit; extra resets first",
        99,
    )
    assert run(main, extra, live="extra")[:3] == (
        "stay",
        None,
        "every account is at its limit; extra resets first",
    )
    later = reading("extra", 100, 60, session_reset_s=5 * HOUR)
    assert run(main, later, live="extra")[:2] == ("switch", "main")


def test_the_earliest_reset_waits_for_every_row_holding_an_account_back() -> None:
    # main's session resets in an hour, but its week is spent for two more days.
    main = reading("main", 100, 99, session_reset_s=HOUR, weekly_reset_s=2 * DAY)
    extra = reading("extra", 99, 10, session_reset_s=3 * HOUR)

    assert run(main, extra, live="main")[:2] == ("switch", "extra")


def test_main_back_to_10_while_on_extra_returns_after_the_cooldown() -> None:
    # main was left at 85% a moment ago; what limited it has since reset.
    last = LastSwitch("main", "extra", NOW - 100.0, "main at 85% session", NOW - 10.0, 85)

    soon = run(reading("main", 10, 10), reading("extra", 50, 50), live="extra", last=last)
    later_s = NOW + 800.0
    later = run(
        reading("main", 10, 10, now=later_s),
        reading("extra", 50, 50, now=later_s),
        live="extra",
        last=last,
        now=later_s,
    )

    assert soon[:3] == ("stay", None, "cooldown: last switch 100s ago")
    assert later[:3] == ("switch", "main", "main is at 10%, below its 85% step")


def test_extra_login_lapsed_means_main() -> None:
    # envelope.collect reads an account whose refresh token lapsed as needs_login.
    dead = reading("extra", 0, 0, state=AccountState.NEEDS_LOGIN)

    assert run(reading("main", 10, 90), dead, live="main")[:3] == (
        "stay",
        None,
        "main is below 99% (step 3 of 3)",
    )
    assert run(reading("main", 10, 50), dead, live="main")[:2] == ("stay", None)


@pytest.mark.parametrize(
    "unusable",
    [
        reading("extra", state=AccountState.ERROR),
        reading("extra", identified=False),
        reading("extra", session=None),
    ],
)
def test_extra_is_never_a_target_without_a_usable_login_and_known_numbers(
    unusable: Reading,
) -> None:
    assert run(reading("main", 10, 90), unusable, live="main")[:2] == ("stay", None)


def test_an_excluded_account_is_never_a_target() -> None:
    settings = Settings(auto_switch=True, excluded=("extra",), policy=POLICY)

    decision = run(reading("main", 10, 90), reading("extra"), live="main", settings=settings)

    assert decision[:3] == ("stay", None, "main is below 99% (step 3 of 3)")


def test_stale_numbers_stay() -> None:
    # 400 s is past USAGE_FRESH_S: 50% then says nothing about main now.
    stale_live = run(reading("main", 50, 50, age_s=400.0), reading("extra"), live="main")
    no_answer = run(Reading("main", AccountState.OK, None, None), reading("extra"), live="main")
    stale_extra = run(reading("main", 10, 90), reading("extra", age_s=400.0), live="main")

    assert stale_live[:3] == ("stay", None, "no fresh numbers for main")
    assert no_answer[:3] == ("stay", None, "no fresh numbers for main")
    assert stale_extra[:3] == ("stay", None, "main is below 99% (step 3 of 3)")


def test_an_old_reading_over_the_limit_still_counts() -> None:
    # A reading is a floor until its row resets: 90% two days before the reset is true now.
    assert run(reading("main", 10, 90, age_s=1800.0), reading("extra"), live="main")[:2] == (
        "switch",
        "extra",
    )


# --- the cooldown and the fence ------------------------------------------------


def test_the_cooldown_never_delays_leaving_an_account_over_its_limit() -> None:
    last = LastSwitch("extra", "main", NOW - 60.0, "main is at 10%, below its 85% step", None)

    assert run(reading("main", 85, 10), reading("extra", 10, 10), live="main", last=last)[:2] == (
        "switch",
        "extra",
    )
    assert run(reading("main", 10, 10), reading("extra", 99, 10), live="extra", last=last)[:2] == (
        "switch",
        "main",
    ), "extra at its last limit leaves at once, even for a step that outranks it"


def test_the_fence_holds_only_at_the_limit_an_account_was_left_at() -> None:
    # main was left at 85% two days before its week resets.
    last = LastSwitch("main", "extra", NOW - 5000.0, "main at 85% weekly", NOW + 2 * DAY, 85)

    fresh_low = run(reading("main", 10, 10), reading("extra", 50, 50), live="extra", last=last)
    extra_spent = run(reading("main", 10, 90), reading("extra", 99, 50), live="extra", last=last)

    assert fresh_low[:3] == ("stay", None, "extra is below 99% (step 2 of 3)"), (
        "fresh numbers do not override the memory of why main was left at 85%"
    )
    assert extra_spent[:2] == ("switch", "main"), "the 99% step is not fenced"


def test_a_record_without_a_limit_fences_every_limit() -> None:
    last = LastSwitch("main", "extra", NOW - 5000.0, "undo", NOW + 2 * DAY)
    extra = reading("extra", 99, 50, session_reset_s=HOUR)

    assert run(reading("main", 10, 90), extra, live="extra", last=last)[:3] == (
        "stay",
        None,
        "every account is at its limit; extra resets first",
    )


# --- the rest of the rules ----------------------------------------------------


def test_an_account_outside_the_policy_is_left_alone() -> None:
    assert run(reading("work", 100, 100), reading("main"), live="work")[:3] == (
        "stay",
        None,
        "work is not in the policy",
    )


def test_a_model_row_counts_only_when_the_policy_names_it() -> None:
    main = reading("main", 10, 10, fable=90)
    named = Settings(auto_switch=True, policy=Policy(POLICY.steps, block_models=("fable",)))

    assert run(main, reading("extra"), live="main")[:2] == ("stay", None)
    assert run(main, reading("extra"), live="main", settings=named) == (
        "switch",
        "extra",
        "main at 90% weekly Fable",
        85,
    )


def test_off_and_unknown_live_still_stay() -> None:
    off = Settings(auto_switch=False, policy=POLICY)

    assert run(reading("main", 100), reading("extra"), live="main", settings=off)[2] == (
        "auto-switch is off"
    )
    assert run(reading("main", 100), reading("extra"), live=None)[2] == "live account unknown"


def test_the_default_rule_records_its_threshold_as_the_limit() -> None:
    decision = decide(
        [reading("a", 99), reading("b")],
        settings=Settings(auto_switch=True),
        live="a",
        now_s=NOW,
        last=None,
    )

    assert (decision.target, decision.limit) == ("b", 99)


def test_ordered_steps_name_the_step_now_and_the_one_a_switch_goes_to() -> None:
    fine = [reading("main", 50, 50), reading("extra", 10, 10)]
    late = [reading("main", 10, 90), reading("extra", 99, 50)]

    assert ordered_steps(fine, settings=ON, live="main", now_s=NOW, last=None) == (0, 1)
    assert next_candidate(fine, settings=ON, live="main", now_s=NOW, last=None) == "extra"
    assert ordered_steps(late, settings=ON, live="main", now_s=NOW, last=None) == (2, None)
    assert next_candidate(late, settings=ON, live="main", now_s=NOW, last=None) is None


# --- settings.json ------------------------------------------------------------


def _write(root: Path, document: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    auto.settings_path(root).write_text(json.dumps(document))


def test_the_policy_reads_and_writes_back_in_the_documented_shape(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    _write(root, {"schema": 1, "auto_switch": True, "policy": BRIEF})

    settings = auto.read_settings(root)
    auto.write_settings(root, settings)

    assert settings == ON
    assert json.loads(auto.settings_path(root).read_text())["policy"] == BRIEF
    assert auto.read_settings(root) == ON


def test_block_models_and_a_missing_then_are_read(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    policy = {"mode": "ordered", "steps": BRIEF["steps"], "block_models": ["Fable"]}
    _write(root, {"schema": 1, "policy": policy})

    assert auto.read_settings(root).policy == Policy(POLICY.steps, ("Fable",))


@pytest.mark.parametrize(
    "policy",
    [
        {"mode": "most_room", "steps": BRIEF["steps"]},
        {"steps": BRIEF["steps"]},
        {"mode": "ordered", "steps": []},
        {"mode": "ordered"},
        {"mode": "ordered", "steps": BRIEF["steps"], "then": "stay"},
        {"mode": "ordered", "steps": [{"account": "main"}]},
        {"mode": "ordered", "steps": [{"account": "main", "until": 0}]},
        {"mode": "ordered", "steps": [{"account": "main", "until": 101}]},
        {"mode": "ordered", "steps": [{"account": "main", "until": True}]},
        {"mode": "ordered", "steps": [{"account": "main", "until": 85.5}]},
        {"mode": "ordered", "steps": [{"account": "../main", "until": 85}]},
        {"mode": "ordered", "steps": [["main", 85]]},
        {"mode": "ordered", "steps": BRIEF["steps"], "block_models": "Fable"},
        {"mode": "ordered", "steps": BRIEF["steps"], "block_models": [""]},
        "ordered",
    ],
)
def test_a_policy_wrong_anywhere_means_the_defaults(tmp_home: Path, policy: object) -> None:
    # Wrong anywhere leaves auto-switch off, rather than running a policy nobody wrote.
    root = tmp_home / ".vibemaxxing"
    _write(root, {"schema": 1, "auto_switch": True, "policy": policy})

    assert auto.read_settings(root) == Settings()


def test_the_commands_that_edit_settings_keep_the_policy(tmp_home: Path) -> None:
    w = world(tmp_home, {"main": (10, 10), "extra": (10, 10)}, live="main", on=False)
    auto.write_settings(w.root, Settings(policy=POLICY))

    for argv in (["on"], ["exclude", "extra"], ["include", "extra"]):
        assert cli.main(["auto", *argv], context=w.context()) == 0, argv

    assert auto.read_settings(w.root) == ON


def test_renaming_an_account_renames_its_steps(tmp_home: Path) -> None:
    w = world(tmp_home, {"main": (10, 10), "extra": (10, 10)}, live="main")
    auto.write_settings(w.root, ON)

    assert cli.main(["alias", "main", "primary"], context=w.context()) == 0

    steps = auto.read_settings(w.root).policy.steps
    assert steps == (Step("primary", 85), Step("extra", 99), Step("primary", 99))


def test_the_last_switch_keeps_its_limit(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    auto.write_last(root, LastSwitch("main", "extra", NOW_S, "r", NOW_S + DAY, 85))

    assert auto.read_last(root) == LastSwitch("main", "extra", NOW_S, "r", NOW_S + DAY, 85)
    auto.last_path(root).write_text(
        '{"schema": 1, "from": "a", "to": "b", "at": 1757930000, "reason": "r", "until": null}'
    )
    last = auto.read_last(root)
    assert last is not None and last.limit is None, "a record from 0.3.0 has no limit"


# --- one tick -----------------------------------------------------------------


def ordered_world(
    tmp_home: Path, main: tuple[int, int], extra: tuple[int, int], live: str
) -> World:
    w = world(tmp_home, {"main": main, "extra": extra}, live=live)
    auto.write_settings(w.root, ON)
    return w


def test_tick_follows_the_policy_and_says_so(tmp_home: Path) -> None:
    w = ordered_world(tmp_home, (40, 85), (10, 10), live="main")

    result = w.tick()

    assert result.line == "switched main -> extra: main at 85% weekly"
    assert w.notes == ["Switched to extra: main at 85% weekly"]
    assert store.read_active(w.root) == "extra"
    assert w.port.blob is not None
    assert credentials.parse_blob(w.port.blob).access_token.reveal() == "extra-file"
    last = auto.read_last(w.root)
    assert last is not None and (last.limit, last.until_s) == (85, NOW_S + 2 * DAY)
    assert w.client.requests == []


def test_tick_stays_on_main_when_extras_login_lapsed(tmp_home: Path) -> None:
    w = ordered_world(tmp_home, (10, 90), (0, 0), live="main")
    extra = store.read_account(w.root, "extra")
    store.write_account(
        w.root,
        store.Account(
            "extra",
            make_credential(refresh_expires_at_ms=1),
            extra.identity,
            AccountState.OK,
            None,
            extra.added_at,
        ),
    )

    assert w.tick().line == "stay: main is below 99% (step 3 of 3)"
    assert store.read_active(w.root) == "main" and w.notes == []


def test_tick_goes_back_to_main_once_the_cooldown_is_over(tmp_home: Path) -> None:
    w = ordered_world(tmp_home, (10, 10), (50, 50), live="extra")
    auto.write_last(
        w.root, LastSwitch("main", "extra", NOW_S - 100.0, "main at 85% session", None, 85)
    )

    assert w.tick().line == "stay: cooldown: last switch 100s ago"
    later = NOW_S + 801.0
    for alias, (session, weekly) in (("main", (10, 10)), ("extra", (50, 50))):
        cache(w.root, alias, payload(session, weekly, now=later), fetched_at=later - 10.0)
    result = w.tick(later)

    assert result.line == "switched extra -> main: main is at 10%, below its 85% step"
    assert store.read_active(w.root) == "main"


def _usage_requests(client: FakeHttpClient, token: str) -> int:
    return sum(
        1
        for request in client.requests
        if request.url == usage.USAGE_URL and request.headers["Authorization"] == f"Bearer {token}"
    )


@pytest.mark.parametrize(("weekly", "asked"), [(78, 2), (86, 1), (92, 2)])
def test_tick_polls_main_every_minute_just_below_each_of_its_limits(
    tmp_home: Path, weekly: int, asked: int
) -> None:
    w = world(tmp_home, {"main": (10, weekly), "extra": (99, 10)}, live="main", seed_cache=False)
    auto.write_settings(w.root, ON)
    cache(w.root, "extra", payload(99, 10), fetched_at=NOW_S - 10.0)
    for _ in range(2):
        w.client.queue_json(200, payload(10, weekly))

    w.tick(NOW_S)
    w.tick(NOW_S + 61.0)

    # 76..84 is near the 85% step, 90..98 near the 99% one; 86 is past one and far from the other.
    assert _usage_requests(w.client, "main-live") == asked


# --- vibe auto status ---------------------------------------------------------


def test_status_shows_the_policy_and_the_steps(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = ordered_world(tmp_home, (50, 50), (10, 10), live="main")

    assert cli.main(["auto", "status"], context=w.context()) == 0
    assert capsys.readouterr().out == (
        "auto-switch  on\n"
        "policy       ordered: main until 85% -> extra until 99% -> main until 99%,"
        " then earliest reset\n"
        "cooldown     900s\n"
        "excluded     none\n"
        "live         main\n"
        "step         1 of 3: main until 85%\n"
        "last switch  none\n"
        "next step    2 of 3: extra until 99%\n"
    )

    assert cli.main(["auto", "status", "--json"], context=w.context()) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["policy"], out["step"], out["next_step"], out["next"]) == (BRIEF, 1, 2, "extra")


def test_status_when_no_step_has_room(tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    w = ordered_world(tmp_home, (100, 50), (100, 50), live="main")

    assert cli.main(["auto", "status"], context=w.context()) == 0

    out = capsys.readouterr().out
    assert "step         none has room - earliest reset\n" in out
    assert out.endswith("next step    none has room - earliest reset\n")


def test_status_without_a_policy_is_unchanged(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world(tmp_home, {"a": (10, 10), "b": (40, 40)})

    assert cli.main(["auto", "status", "--json"], context=w.context()) == 0

    out = json.loads(capsys.readouterr().out)
    assert (out["policy"], out["step"], out["next_step"]) == (None, None, None)


def test_status_does_not_guess_the_step_while_the_pointer_is_wrong(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = ordered_world(tmp_home, (50, 50), (10, 10), live="extra")
    store.write_active(w.root, "main")

    assert cli.main(["auto", "status"], context=w.context()) == 0

    out = capsys.readouterr().out
    assert "step         unknown\n" in out and "next step    unknown\n" in out
    assert w.client.requests == []


def test_an_account_seeded_without_uuid_is_unidentified(tmp_home: Path) -> None:
    # The tick can only follow the policy for accounts it can tell apart afterwards.
    w = ordered_world(tmp_home, (40, 85), (10, 10), live="main")
    seed_account(w.root, "extra", credential=make_credential(access="extra-file"))

    assert w.tick().line == "stay: main is below 99% (step 3 of 3)"
