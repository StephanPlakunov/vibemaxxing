"""Cursor on every surface: the status line, status.json and `vibe usage`.

The status line and the TUI show cached numbers only. Nothing here reaches the real Keychain, a
real cursor-agent or the network: stubs and fakes throughout.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.cursor_fakes import StubAgent, StubToken, usage_answer
from tests.fakes import FakeHttpClient, FakeKeychain
from vibemaxxing import auto, cli, cursor, launch, statusline, store
from vibemaxxing.cursor_usage import CursorUsage, availability
from vibemaxxing.errors import NetworkError

NOW_S = 1_757_930_000.0
CYCLE_END_S = NOW_S + 5 * 86_400


def seat(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "alias": "cursor",
        "kind": "cursor",
        "fetched_at": NOW_S - 30,
        "included_percent": 0.096,
        "on_demand_used_cents": 0,
        "on_demand_cap_cents": 25_000,
        "cycle_end_s": CYCLE_END_S,
        "available": True,
    }
    return {**base, **kw}


class Rig:
    def __init__(self, tmp_home: Path, **agent: object) -> None:
        self.root = tmp_home / ".vibemaxxing"
        self.agent = StubAgent(**agent)  # type: ignore[arg-type]
        self.client = FakeHttpClient()
        self.cursor_client = FakeHttpClient()
        self.port = FakeKeychain()
        self.token = StubToken()
        self.now_s = NOW_S
        cursor.write_marker(self.root, "cursor", added_at=NOW_S - 1000)

    def ctx(self) -> cli.Context:
        return cli.Context(
            root=self.root,
            client=self.client,
            port=self.port,
            now_s=self.now_s,
            clock=lambda: self.now_s,
            prompt=lambda _: "",
            browser=lambda _: True,
            agent=self.agent,
            cursor_client=self.cursor_client,
            cursor_token=self.token,
        )

    def queue(self, **kw: int | float) -> None:
        self.cursor_client.queue_json(200, usage_answer(**kw))
        self.cursor_client.queue_json(200, {"hardLimit": 250})

    def status(self) -> dict[str, object]:
        document = statusline.read(self.root)
        assert document is not None
        return document


@pytest.fixture
def rig(tmp_home: Path) -> Rig:
    return Rig(tmp_home)


# --- the status line ----------------------------------------------------------------------------


def line(**kw: object) -> str | None:
    return statusline._cursor(seat(**kw), NOW_S)


def test_the_status_line_segment_is_the_included_percent() -> None:
    assert line() == "cursor: 0%"
    assert line(included_percent=42.4, on_demand_used_cents=1250) == "cursor: 42%"
    # Included usage used up: its reset, as on every other account.
    full = line(included_percent=100.0, on_demand_used_cents=500)
    assert full is not None and full.startswith("cursor: 100% (resets ")


def test_unknown_and_old_numbers_get_a_question_mark() -> None:
    assert line(fetched_at=None) == "cursor: ?"
    assert line(fetched_at=NOW_S - 1000) == "cursor: 0%?"  # older than 15 minutes
    assert line(cycle_end_s=NOW_S - 1) == "cursor: ?"  # the cycle those numbers describe is over
    assert line(available=None, on_demand_used_cents=None) == "cursor: 0%"
    assert line(included_percent=None, on_demand_used_cents=None, available=None) == "cursor: ?"
    assert statusline._cursor({"fetched_at": NOW_S}, NOW_S) is None  # no alias


def test_a_seat_with_its_on_demand_spent_still_shows_its_included_percent() -> None:
    # Included usage is still usable with on-demand gone: the percent stays the included one.
    assert line(included_percent=33.1, available=False, on_demand_used_cents=25_017) == (
        "cursor: 33% (on-demand spent)"
    )
    both = line(included_percent=100.0, available=False, on_demand_used_cents=25_000)
    assert both is not None and both.startswith("cursor: 100% (on-demand spent, resets ")
    # Below the cap on-demand is not mentioned, whatever routing thinks.
    assert line(included_percent=33.1, available=False, on_demand_used_cents=24_500) == (
        "cursor: 33%"
    )


def test_the_line_has_the_claude_accounts_then_the_seat() -> None:
    document = {
        "schema": 1,
        "live": "main",
        "accounts": [
            {
                "alias": "main",
                "state": "ok",
                "fetched_at": NOW_S - 10,
                "rows": [{"kind": "session", "percent": 5, "resets_at": NOW_S + 3600}],
            }
        ],
        "cursor": [seat()],
    }

    rendered = statusline.render(document, now_s=NOW_S)

    assert rendered.startswith("▶ main · main: 5%")
    assert rendered.endswith(" · cursor: 0%")
    only_seat = statusline.render({"schema": 1, "accounts": [], "cursor": [seat()]}, now_s=NOW_S)
    assert only_seat == "cursor: 0%"
    assert "cursor" not in statusline.render(
        {"schema": 1, "accounts": [], "cursor": []}, now_s=NOW_S
    )
    assert statusline.render({"schema": 1, "cursor": "junk", "accounts": []}, now_s=NOW_S) == (
        statusline.NO_STATUS
    )


def test_the_status_line_module_stays_light_and_reads_only_the_file(tmp_path: Path) -> None:
    """The line runs on every prompt redraw: no Keychain, no network, no heavy import."""
    (tmp_path / ".vibemaxxing").mkdir()
    document = {"schema": 1, "written_at": 1.0, "live": None, "accounts": [], "cursor": [seat()]}
    (tmp_path / ".vibemaxxing" / "status.json").write_text(json.dumps(document))
    probe = textwrap.dedent(
        """
        import sys
        from vibemaxxing import entry
        sys.argv = ["vibe", "statusline"]
        entry.main()
        heavy = [m for m in ("vibemaxxing.cli", "vibemaxxing.cursor", "vibemaxxing.cursor_usage",
                             "vibemaxxing.launch", "textual", "urllib.request", "subprocess")
                 if m in sys.modules]
        print("HEAVY", heavy)
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert "cursor: ?" in done.stdout or "cursor:" in done.stdout
    assert "HEAVY []" in done.stdout


# --- status.json --------------------------------------------------------------------------------


def test_list_writes_the_seat_into_status_json_with_its_fresh_numbers(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.queue(percent=42.4, on_demand_used=1250, on_demand_limit=25_000)

    assert cli.main(["list"], context=rig.ctx()) == 0
    capsys.readouterr()

    (entry,) = rig.status()["cursor"]  # type: ignore[misc]
    assert entry["alias"] == "cursor" and entry["included_percent"] == 42.4
    assert entry["on_demand_used_cents"] == 1250 and entry["on_demand_cap_cents"] == 25_000
    assert entry["available"] is True and entry["fetched_at"] == NOW_S
    assert statusline.render(rig.status(), now_s=NOW_S).endswith("cursor: 42%")


def test_status_json_holds_the_limit_cursor_reports(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.queue(on_demand_used=100, on_demand_limit=75_000)  # Cursor says $750
    assert cli.main(["list"], context=rig.ctx()) == 0
    capsys.readouterr()

    entry = cursor.status_entry(rig.root, "cursor")

    assert entry["on_demand_cap_cents"] == 75_000
    assert statusline.render(rig.status(), now_s=NOW_S).endswith("cursor: 12%")


def test_a_seat_nobody_has_read_has_no_figures_and_the_line_says_so(rig: Rig) -> None:
    auto.publish_status(rig.root, live=None, now_s=NOW_S)

    (entry,) = rig.status()["cursor"]  # type: ignore[misc]
    assert entry["fetched_at"] is None and entry["included_percent"] is None
    assert statusline.render(rig.status(), now_s=NOW_S).endswith("cursor: ?")


def test_a_cache_of_another_login_is_not_this_seats_numbers(rig: Rig) -> None:
    store.write_usage_cache(
        rig.root,
        "cursor",
        store.UsageCache(1.0, NOW_S, {"included_percent": 99.0}, None, 0, None),  # other added_at
    )

    entry = cursor.status_entry(rig.root, "cursor")

    assert entry["included_percent"] is None


def test_publishing_status_makes_no_request_and_starts_no_process(rig: Rig) -> None:
    auto.publish_status(rig.root, live=None, now_s=NOW_S)

    assert rig.cursor_client.requests == [] and rig.agent.calls == []


def test_a_wrong_routing_does_not_blank_the_status_line(rig: Rig) -> None:
    (rig.root / "settings.json").write_text(json.dumps({"schema": 1, "routing": {"surprise": 1}}))

    auto.publish_status(rig.root, live=None, now_s=NOW_S)

    assert len(rig.status()["cursor"]) == 1  # type: ignore[arg-type]


# --- the background refresh (the tick) ----------------------------------------------------------


def test_the_tick_refreshes_a_stale_seat_and_leaves_a_fresh_one_alone(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.queue(percent=5.0)
    assert cli.main(["auto", "tick"], context=rig.ctx()) == 0
    assert len(rig.cursor_client.requests) == 2  # no cache yet: read
    assert statusline.render(rig.status(), now_s=NOW_S).endswith("cursor: 5%")

    rig.now_s = NOW_S + 100  # inside three minutes: nothing is requested
    cli.main(["auto", "tick"], context=rig.ctx())
    assert len(rig.cursor_client.requests) == 2

    rig.now_s = NOW_S + 200
    rig.queue(percent=6.0)
    cli.main(["auto", "tick"], context=rig.ctx())
    assert len(rig.cursor_client.requests) == 4
    assert statusline.render(rig.status(), now_s=rig.now_s).endswith("cursor: 6%")
    capsys.readouterr()


def test_a_failing_refresh_never_fails_the_tick_and_the_old_numbers_age_into_a_question_mark(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.queue(percent=5.0)
    cli.main(["auto", "tick"], context=rig.ctx())
    rig.now_s = NOW_S + 400
    rig.cursor_client.queue(NetworkError("offline", "x"))

    assert cli.main(["auto", "tick"], context=rig.ctx()) == 0

    assert statusline.render(rig.status(), now_s=rig.now_s).endswith("cursor: 5%")
    late = NOW_S + 2000
    assert statusline.render(rig.status(), now_s=late).endswith("cursor: 5%?")
    capsys.readouterr()


def test_the_tick_with_no_seat_does_nothing_extra(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    client = FakeHttpClient()
    ctx = cli.Context(root=root, client=client, port=FakeKeychain(), now_s=NOW_S)

    ctx.refresh_cursor_cache()

    assert client.requests == []


# --- the TUI ------------------------------------------------------------------------------------


def _panel(reading: launch.CursorReading, *, now_s: float, colour: bool = False) -> str:
    """The account's block in `vibe usage`, as text."""
    return "\n".join(cli._render_cursor(reading, now_s=now_s, colour=colour))


def reading(**kw: object) -> launch.CursorReading:
    usage = CursorUsage(
        included_percent=42.4,
        on_demand_used_cents=1250,
        on_demand_limit_cents=25_000,
        cycle_end_s=CYCLE_END_S,
    )
    base: dict[str, object] = {
        "alias": "cursor",
        "logged_in": True,
        "email": "dev@example.com",
        "team": "Zvoove",
        "usage": usage,
        "note": "",
        "availability": availability(usage),
    }
    return launch.CursorReading(**{**base, **kw})  # type: ignore[arg-type]


def test_the_cursor_panel_shows_both_bars_the_reset_and_the_routing_state() -> None:
    text = _panel(reading(), now_s=NOW_S)

    assert "cursor" in text and "dev@example.com" in text and "Cursor · team Zvoove" in text
    assert "Included + bonus" in text and "42%" in text and "in 5d" in text
    assert "On-demand" in text and "$12.50 of $250" in text and "5%" in text
    assert "billed to the company" not in text and "routing:" not in text


def test_the_cursor_panel_for_unknown_logged_out_and_spent() -> None:
    unknown = _panel(
        reading(
            usage=None,
            note="usage unknown (HTTP 500)",
            availability=availability(None),
        ),
        now_s=NOW_S,
    )
    assert "usage: unknown (usage unknown (HTTP 500)); see cursor.com/dashboard" in unknown
    assert "%" not in unknown and "$" not in unknown  # never a made-up number

    out = _panel(reading(logged_in=False, usage=None), now_s=NOW_S)
    assert "not logged in — run: cursor-agent login" in out

    spent_usage = CursorUsage(100.0, 25_000, 25_000, CYCLE_END_S)
    spent = _panel(
        reading(usage=spent_usage, availability=availability(spent_usage)),
        now_s=NOW_S,
    )
    assert "routing:" not in spent
    coloured = _panel(
        reading(usage=spent_usage, availability=availability(spent_usage)),
        now_s=NOW_S,
        colour=True,
    )
    red = next(line for line in coloured.splitlines() if "On-demand" in line)
    assert red.startswith("\x1b[31m")  # the spent line stands out


def test_the_panel_scrubs_what_the_note_might_carry() -> None:
    from vibemaxxing.redact import Secret

    Secret("CURSORTOKEN0000000000")  # registers it, as a real read would
    text = _panel(
        reading(
            usage=None,
            note="boom CURSORTOKEN0000000000",
            availability=availability(None),
        ),
        now_s=NOW_S,
    )

    assert "CURSORTOKEN0000000000" not in text
