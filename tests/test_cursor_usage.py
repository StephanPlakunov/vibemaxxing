"""The Cursor seat's usage: the request, the parse, "unknown" on any shape change, no token out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.cursor_fakes import StubToken, usage_answer
from tests.fakes import FakeHttpClient
from vibemaxxing import cursor_usage, httpclient
from vibemaxxing.cursor_usage import CursorUsage, availability, fetch, parse_usage
from vibemaxxing.errors import NetworkError
from vibemaxxing.httpclient import CURSOR_HOSTS, HTTPError, UrllibClient

TOKEN = "CURSORTOKEN0000000000"


def test_the_request_is_the_agents_own_rpc_with_the_bearer_token() -> None:
    client = FakeHttpClient()
    client.queue_json(200, usage_answer())
    client.queue_json(200, {"hardLimit": 250})

    usage, why = fetch(client, StubToken(TOKEN))

    assert why == "" and usage is not None
    first, second = client.requests
    assert first.method == "POST"
    assert first.url == "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
    assert second.url.endswith("/aiserver.v1.DashboardService/GetHardLimit")
    assert first.headers["Authorization"] == f"Bearer {TOKEN}"
    assert first.headers["Connect-Protocol-Version"] == "1"
    assert first.headers["Content-Type"] == "application/json"
    assert first.body == b"{}"


def test_the_agents_answer_is_read_in_dollars_and_percent() -> None:
    parsed = parse_usage(
        usage_answer(percent=42.5, on_demand_used=300, on_demand_limit=25_000), None
    )

    assert parsed == CursorUsage(
        included_percent=42.5,
        on_demand_used_cents=300,
        on_demand_limit_cents=25_000,
        cycle_end_s=1_762_209_900.0,
    )


def test_int64_fields_arrive_as_strings() -> None:
    answer = usage_answer()
    spend = answer["spendLimitUsage"]
    assert isinstance(spend, dict)
    spend["individualUsed"] = "1250"
    spend["individualLimit"] = "25000"

    parsed = parse_usage(answer, None)

    assert parsed is not None and parsed.on_demand_used_cents == 1250


def test_the_web_apis_shape_is_read_too() -> None:
    web = {
        "membershipType": "enterprise",
        "billingCycleEnd": "1762209900000",
        "individualUsage": {
            "plan": {"used": 240, "limit": 2000, "totalPercentUsed": 12},
            "onDemand": {"enabled": True, "used": 0, "limit": 25_000, "remaining": 25_000},
        },
    }

    parsed = parse_usage(web, None)

    assert parsed == CursorUsage(12, 0, 25_000, 1_762_209_900.0, "enterprise")


def test_the_cap_falls_back_to_the_hard_limit() -> None:
    answer = usage_answer()
    spend = answer["spendLimitUsage"]
    assert isinstance(spend, dict)
    del spend["individualLimit"]

    assert parse_usage(answer, {"hardLimit": 250}) == parse_usage(usage_answer(), None)
    off = parse_usage(answer, {"noUsageBasedAllowed": True})
    unlimited = parse_usage(answer, {"hardLimit": 2_147_483_647})
    assert off is not None and off.on_demand_limit_cents == 0
    assert unlimited is not None and unlimited.on_demand_limit_cents is None


@pytest.mark.parametrize(
    "answer",
    [None, {}, {"unrelated": 1}, {"planUsage": "text", "spendLimitUsage": [1]}],
)
def test_a_shape_change_is_unknown_not_a_crash(answer: dict[str, object] | None) -> None:
    assert parse_usage(answer, None) is None
    assert availability(None, margin_cents=1000).ok is None


def test_garbage_numbers_are_not_numbers() -> None:
    answer = usage_answer(percent=float("nan"))
    spend = answer["spendLimitUsage"]
    assert isinstance(spend, dict)
    spend["individualUsed"] = True  # a bool is a shape error, not 1 cent
    spend["individualLimit"] = "lots"

    parsed = parse_usage(answer, None)

    assert parsed is not None
    assert parsed.included_percent == 12.0  # NaN is skipped; derived from spend / limit
    assert parsed.on_demand_used_cents is None and parsed.on_demand_limit_cents is None
    assert availability(parsed, margin_cents=1000).ok is None


# --- the rule: used until it is used up -------------------------------------------------


def avail(used: int | None, cap: int | None, margin: int = 1000) -> cursor_usage.Availability:
    return availability(CursorUsage(50.0, used, cap, None), margin_cents=margin)


def test_included_usage_and_bonus_count_as_room_while_on_demand_is_untouched() -> None:
    assert avail(0, 25_000).ok is True
    assert avail(0, None).ok is True  # no cap named: still on the included allowance
    assert avail(0, 0).ok is True


def test_on_demand_is_used_up_to_the_cap_minus_the_margin() -> None:
    assert avail(5_000, 25_000).ok is True
    assert avail(23_999, 25_000).ok is True
    assert avail(24_000, 25_000).ok is False  # within the $10 margin of the cap
    assert avail(25_000, 25_000).ok is False
    assert avail(30_000, 25_000).ok is False
    assert avail(24_500, 25_000, margin=0).ok is True


def test_on_demand_off_or_unnamed_once_it_has_started() -> None:
    assert avail(100, 0).ok is False
    assert avail(100, None).ok is None
    assert avail(None, 25_000).ok is None


def test_the_reason_is_carried_when_there_is_no_answer() -> None:
    assert availability(None, margin_cents=1000, reason="usage unknown (HTTP 401)").why == (
        "usage unknown (HTTP 401)"
    )


# --- failure is unknown, and never carries the token ------------------------------------


def test_no_token_is_unknown() -> None:
    usage, why = fetch(FakeHttpClient(), StubToken(None))

    assert usage is None and "no Cursor login token" in why


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_says_to_log_in_again(status: int) -> None:
    client = FakeHttpClient()
    client.queue(HTTPError(status, b"{}"))

    usage, why = fetch(client, StubToken(TOKEN))

    assert usage is None
    assert f"HTTP {status}" in why and "cursor-agent login" in why
    assert TOKEN not in why


def test_a_server_error_and_a_network_error_are_unknown() -> None:
    client = FakeHttpClient()
    client.queue(HTTPError(500, b"boom"))
    assert fetch(client, StubToken(TOKEN))[0] is None

    client = FakeHttpClient()
    client.queue(NetworkError(f"could not reach api2.cursor.sh: {TOKEN}", "x"))
    usage, why = fetch(client, StubToken(TOKEN))
    assert usage is None and TOKEN not in why  # scrubbed: the token is a registered Secret


def test_an_answer_in_an_unknown_shape_is_unknown() -> None:
    client = FakeHttpClient()
    client.queue_json(200, {"something": "else"})
    client.queue_json(200, {})

    usage, why = fetch(client, StubToken(TOKEN))

    assert usage is None and "not in a shape vibe knows" in why


def test_a_body_that_is_not_json_is_unknown() -> None:
    client = FakeHttpClient()
    client.queue(httpclient.HttpResponse(200, b"<html>"))

    usage, why = fetch(client, StubToken(TOKEN))

    assert usage is None and "usage unknown" in why


def test_a_missing_hard_limit_call_does_not_lose_the_answer() -> None:
    client = FakeHttpClient()
    client.queue_json(200, usage_answer())
    client.queue(HTTPError(404, b""))

    usage, _ = fetch(client, StubToken(TOKEN))

    assert usage is not None and usage.on_demand_limit_cents == 25_000


# --- the client that may talk to Cursor ---------------------------------------------------


def test_the_cursor_client_talks_to_cursor_only_and_the_claude_client_never_to_cursor() -> None:
    cursor_client = UrllibClient(CURSOR_HOSTS)
    with pytest.raises(NetworkError, match=r"refusing to contact 'claude\.ai'"):
        cursor_client.request("GET", "https://claude.ai/x", headers={})
    with pytest.raises(NetworkError, match=r"refusing to contact 'cursor\.com'"):
        cursor_client.request("GET", "https://cursor.com/api/usage-summary", headers={})
    with pytest.raises(NetworkError, match=r"refusing to contact 'api2\.cursor\.sh'"):
        UrllibClient().request("POST", "https://api2.cursor.sh/x", headers={})


# --- where the token is read from, without reading it ------------------------------------


def test_the_file_fallback_reads_access_token_only(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    path = tmp_home / ".config" / "cursor" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"accessToken": TOKEN, "refreshToken": "REFRESHTOKEN00000000"}))

    secret = cursor_usage.AgentTokenStore().read()

    assert secret is not None and secret.reveal() == TOKEN
    assert TOKEN not in repr(secret) and TOKEN not in str(secret)
    path.write_text("{not json")
    assert cursor_usage.AgentTokenStore().read() is None


def test_the_keychain_item_names_are_the_agents_own() -> None:
    assert cursor_usage.KEYCHAIN_SERVICE == "cursor-access-token"
    assert cursor_usage.KEYCHAIN_ACCOUNT == "cursor-user"


# --- the real answer (shape read from the live endpoint on 3 Nov 2026, numbers kept) -----------


def real_answer(**spend: object) -> dict[str, object]:
    return {
        "billingCycleStart": "1791067517000",
        "billingCycleEnd": "1793745917000",
        "planUsage": {
            "totalSpend": 24,
            "includedSpend": 24,
            "remaining": 1976,
            "limit": 2000,
            "remainingBonus": False,
            "autoPercentUsed": 0.1142857142857143,
            "apiPercentUsed": 0,
            "totalPercentUsed": 0.096,
        },
        # proto3 JSON leaves out a field that is zero: there is no individualUsed here
        "spendLimitUsage": {
            "totalSpend": 1587827,
            "pooledUsed": 1587827,
            "individualLimit": 25000,
            "individualRemaining": 25000,
            "limitType": "team",
            **spend,
        },
        "displayMessage": "You've used 0% of your included usage",
    }


def test_the_real_answer_has_no_used_field_when_nothing_is_used() -> None:
    parsed = parse_usage(real_answer(), {"hardLimit": 2_147_483_647})

    assert parsed is not None
    assert parsed.on_demand_used_cents == 0 and parsed.on_demand_limit_cents == 25_000
    assert parsed.included_percent == pytest.approx(0.096)
    assert availability(parsed, margin_cents=1000).ok is True


def test_used_is_derived_from_the_remaining_when_it_is_left_out() -> None:
    parsed = parse_usage(real_answer(individualRemaining=20_000), None)

    assert parsed is not None and parsed.on_demand_used_cents == 5_000
    spent = parse_usage(real_answer(individualRemaining=500), None)
    assert spent is not None and availability(spent, margin_cents=1000).ok is False


def test_with_neither_used_nor_remaining_the_figure_is_unknown() -> None:
    answer = real_answer()
    spend = answer["spendLimitUsage"]
    assert isinstance(spend, dict)
    del spend["individualRemaining"]

    parsed = parse_usage(answer, None)

    assert parsed is not None and parsed.on_demand_used_cents is None
    assert availability(parsed, margin_cents=1000).ok is None  # never guessed as zero
    spend["individualLimit"] = 0
    assert parse_usage(answer, None).on_demand_used_cents == 0  # type: ignore[union-attr]


def test_the_pooled_team_spend_is_not_mistaken_for_ours() -> None:
    parsed = parse_usage(real_answer(), None)

    assert parsed is not None and parsed.on_demand_used_cents == 0  # pooledUsed is 1587827
