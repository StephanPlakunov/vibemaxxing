"""settings.json `routing`: the connectors each account has, and the bridge's settings.

vibe picks no account: `vibe run <account>` reads only which connectors that account has.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibemaxxing import route
from vibemaxxing.errors import UsageError
from vibemaxxing.route import Routing


def write(root: Path, document: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text(json.dumps({"schema": 1, **document}))


def test_a_missing_file_or_member_is_the_defaults(tmp_path: Path) -> None:
    assert route.read_routing(tmp_path / "none") == Routing()
    write(tmp_path, {"broker": {"on": True}})
    assert route.read_routing(tmp_path) == Routing()


def test_the_connectors_are_configurable_per_account(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "routing": {
                "connectors": {"main": ["slack"], "work": ["slack", "gcal"]},
                "bridge": {"max_calls_per_hour": 10},
            }
        },
    )

    routing = route.read_routing(tmp_path)

    assert routing.connectors["work"] == ("slack", "gcal")
    assert routing.connectors["main"] == ("slack",)
    assert routing.connectors["cursor"] == ("slack", "atlassian", "gcal")  # untouched default
    assert routing.bridge == {"max_calls_per_hour": 10}
    assert route.connectors_of(routing, "work") == {"slack", "gcal"}
    assert route.connectors_of(routing, "nobody") == set()


def test_the_members_of_the_removed_routing_are_read_and_change_nothing(tmp_path: Path) -> None:
    old = {
        "default_needs": ["slack", "atlassian"],
        "no_degrade": ["m365"],
        "order": {"needs": [{"account": "main"}], "no_needs": [{"account": "work"}]},
        "roles": {"main": "main"},
        "spent_percent": 99,
        "shared_login": True,
        "timezone": "UTC",
        "weekday_budget": [1, 2, 3, 4, 5, 6, 7],
        "bridge": {"max_calls_per_hour": 1000},
    }
    write(tmp_path, {"auto_switch": True, "threshold": 99, "cooldown_s": 900, "routing": old})

    routing = route.read_routing(tmp_path)

    assert routing == Routing(bridge={"max_calls_per_hour": 1000})
    assert not hasattr(routing, "order") and not hasattr(routing, "shared_login")


def test_an_old_roles_member_keeps_the_connectors_with_the_account_it_named(tmp_path: Path) -> None:
    # Before, connectors were looked up by role first: with main -> "stephan", the account
    # "stephan" had main's connectors. That still holds, for the defaults and for given ones.
    write(
        tmp_path,
        {
            "routing": {
                "roles": {"main": "stephan", "cursor": "cursor-work", "spare": "solo"},
                "connectors": {"cursor": ["gcal"]},
            }
        },
    )

    routing = route.read_routing(tmp_path)

    assert route.connectors_of(routing, "stephan") == {"slack", "atlassian", "m365"}
    assert route.connectors_of(routing, "cursor-work") == {"gcal"}
    assert route.connectors_of(routing, "solo") == set()


@pytest.mark.parametrize("roles", [["main"], {"main": ""}, {"main": 1}])
def test_a_wrong_roles_member_is_refused(tmp_path: Path, roles: object) -> None:
    write(tmp_path, {"routing": {"roles": roles}})
    with pytest.raises(UsageError, match="roles must map"):
        route.read_routing(tmp_path)


def test_the_default_connectors_name_no_account_that_does_not_exist() -> None:
    assert set(route.DEFAULT_CONNECTORS) == {"main", "cursor"}


def test_needs_are_checked_against_the_known_connectors() -> None:
    assert route.check_needs(["m365", "m365", "slack"]) == ("m365", "slack")
    assert route.check_needs([]) == ()
    with pytest.raises(UsageError, match="unknown connector teams"):
        route.check_needs(["teams"])


@pytest.mark.parametrize(
    "bad",
    [
        "text",
        {"connectors": {"main": ["teams"]}},
        {"connectors": ["main"]},
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
