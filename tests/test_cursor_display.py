"""Cursor on every surface: the status line, status.json, the TUI and the web dashboard.

The status line and the TUI show cached numbers only. Nothing here reaches the real Keychain, a
real cursor-agent or the network: stubs and fakes throughout.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from textual.widgets import Static

from tests.cursor_fakes import StubAgent, StubToken, usage_answer
from tests.fakes import FakeHttpClient, FakeKeychain, fixture_usage, make_credential, seed_account
from vibemaxxing import auto, cli, cursor, launch, statusline, store, tui, web
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


def test_the_status_line_segment_is_compact() -> None:
    assert line() == "cursor: 0.1% $0/$250"
    assert line(included_percent=42.4, on_demand_used_cents=1250) == "cursor: 42% $12.5/$250"
    assert line(included_percent=100.0, on_demand_used_cents=500) == "cursor: 100% $5/$250"


def test_unknown_and_old_numbers_get_a_question_mark() -> None:
    assert line(fetched_at=None) == "cursor: ?"
    assert line(fetched_at=NOW_S - 1000) == "cursor: 0.1% $0/$250?"  # older than 15 minutes
    assert line(cycle_end_s=NOW_S - 1) == "cursor: ?"  # the cycle those numbers describe is over
    assert line(available=None, on_demand_used_cents=None) == "cursor: 0.1%?"
    assert line(included_percent=None, on_demand_used_cents=None, available=None) == "cursor: -?"
    assert statusline._cursor({"fetched_at": NOW_S}, NOW_S) is None  # no alias


def test_a_spent_seat_says_so_and_when_it_resets() -> None:
    text = line(available=False, on_demand_used_cents=24_500)

    assert text is not None and text.startswith("cursor: spent (resets ")
    assert text.endswith(")")


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

    assert rendered.startswith("▶ main · main: 5h 5%")
    assert rendered.endswith(" · cursor: 0.1% $0/$250")
    only_seat = statusline.render({"schema": 1, "accounts": [], "cursor": [seat()]}, now_s=NOW_S)
    assert only_seat == "cursor: 0.1% $0/$250"
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
    assert statusline.render(rig.status(), now_s=NOW_S).endswith("cursor: 42% $12.5/$250")


def test_status_json_holds_the_lower_of_cursors_cap_and_vibes_ceiling(rig: Rig) -> None:
    rig.queue(on_demand_used=100, on_demand_limit=100_000)  # Cursor says $1000
    rig.ctx().cursor_readings()

    entry = cursor.status_entry(rig.root, "cursor", margin_cents=1000, ceiling_cents=25_000)

    assert entry["on_demand_cap_cents"] == 25_000


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

    entry = cursor.status_entry(rig.root, "cursor", margin_cents=1000, ceiling_cents=25_000)

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
    assert statusline.render(rig.status(), now_s=NOW_S).endswith("cursor: 5% $0/$250")

    rig.now_s = NOW_S + 100  # inside three minutes: nothing is requested
    cli.main(["auto", "tick"], context=rig.ctx())
    assert len(rig.cursor_client.requests) == 2

    rig.now_s = NOW_S + 200
    rig.queue(percent=6.0)
    cli.main(["auto", "tick"], context=rig.ctx())
    assert len(rig.cursor_client.requests) == 4
    assert statusline.render(rig.status(), now_s=rig.now_s).endswith("cursor: 6% $0/$250")
    capsys.readouterr()


def test_a_failing_refresh_never_fails_the_tick_and_the_old_numbers_age_into_a_question_mark(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.queue(percent=5.0)
    cli.main(["auto", "tick"], context=rig.ctx())
    rig.now_s = NOW_S + 400
    rig.cursor_client.queue(NetworkError("offline", "x"))

    assert cli.main(["auto", "tick"], context=rig.ctx()) == 0

    assert statusline.render(rig.status(), now_s=rig.now_s).endswith("cursor: 5% $0/$250")
    late = NOW_S + 2000
    assert statusline.render(rig.status(), now_s=late).endswith("cursor: 5% $0/$250?")
    capsys.readouterr()


def test_the_tick_with_no_seat_does_nothing_extra(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    client = FakeHttpClient()
    ctx = cli.Context(root=root, client=client, port=FakeKeychain(), now_s=NOW_S)

    ctx.refresh_cursor_cache()

    assert client.requests == []


# --- the TUI ------------------------------------------------------------------------------------


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
        "availability": availability(usage, margin_cents=1000),
    }
    return launch.CursorReading(**{**base, **kw})  # type: ignore[arg-type]


def test_the_cursor_panel_shows_both_bars_the_reset_and_the_routing_state() -> None:
    text = tui.cursor_content(reading(), now_s=NOW_S).plain

    assert "cursor" in text and "dev@example.com" in text and "Cursor · team Zvoove" in text
    assert "Included + bonus" in text and "42%" in text and "in 5d" in text
    assert "On-demand" in text and "$12.50 of $250.00" in text
    assert "billed to the company" not in text and "routing:" not in text


def test_the_cursor_panel_for_unknown_logged_out_and_spent() -> None:
    unknown = tui.cursor_content(
        reading(
            usage=None,
            note="usage unknown (HTTP 500)",
            availability=availability(None, margin_cents=1),
        ),
        now_s=NOW_S,
    ).plain
    assert "usage: unknown (usage unknown (HTTP 500)); see cursor.com/dashboard" in unknown
    assert "%" not in unknown and "$" not in unknown  # never a made-up number

    out = tui.cursor_content(reading(logged_in=False, usage=None), now_s=NOW_S).plain
    assert "not logged in — run: cursor-agent login" in out

    spent_usage = CursorUsage(100.0, 24_500, 25_000, CYCLE_END_S)
    spent = tui.cursor_content(
        reading(usage=spent_usage, availability=availability(spent_usage, margin_cents=1000)),
        now_s=NOW_S,
    )
    assert "routing:" not in spent.plain
    styled = " ".join(spent.plain[s.start : s.end] for s in spent.spans if s.style)
    assert "On-demand" in styled  # elevated: the spent line stands out


def test_the_panel_scrubs_what_the_note_might_carry() -> None:
    from vibemaxxing.redact import Secret

    Secret("CURSORTOKEN0000000000")  # registers it, as a real read would
    text = tui.cursor_content(
        reading(
            usage=None,
            note="boom CURSORTOKEN0000000000",
            availability=availability(None, margin_cents=1),
        ),
        now_s=NOW_S,
    ).plain

    assert "CURSORTOKEN0000000000" not in text


async def test_the_dashboard_paints_the_seat_next_to_the_claude_accounts(tmp_home: Path) -> None:
    rig = Rig(tmp_home)
    seed_account(rig.root, "main", credential=make_credential(), active=True)
    rig.client.queue_json(200, fixture_usage())
    rig.queue(percent=42.4, on_demand_used=1250)
    app = tui.Dashboard(rig.ctx())

    async with app.run_test() as pilot:
        await asyncio.wait_for(app.first_paint.wait(), timeout=10)
        await pilot.pause()
        assert set(app.panels) == {"main", "cursor"}
        text = app.panels["cursor"].visual.plain  # type: ignore[union-attr]
        assert "Included + bonus" in text and "$12.50 of $250.00" in text
        assert "pool" in str(app.query_one("#footer", Static).visual.plain)  # type: ignore[union-attr]

    # the same cycle left the numbers where the status line reads them
    assert statusline.render(rig.status(), now_s=NOW_S).endswith("cursor: 42% $12.5/$250")


async def test_a_seat_alone_is_not_the_empty_screen(tmp_home: Path) -> None:
    rig = Rig(tmp_home)
    rig.queue()
    app = tui.Dashboard(rig.ctx())

    async with app.run_test() as pilot:
        await asyncio.wait_for(app.first_paint.wait(), timeout=10)
        await pilot.pause()
        assert set(app.panels) == {"cursor"}
        assert app.query_one("#empty", Static).display is False


# --- the web dashboard --------------------------------------------------------------------------


def test_the_snapshot_carries_the_seat_and_the_claude_only_one_does_not(
    rig: Rig, tmp_home: Path
) -> None:
    seed_account(rig.root, "main", credential=make_credential(), active=True)
    rig.client.queue_json(200, fixture_usage())
    rig.queue(percent=10.0, on_demand_used=300)

    envelope = web.snapshot(rig.ctx())

    (entry,) = envelope["cursor"]  # type: ignore[misc]
    assert entry["alias"] == "cursor" and entry["included_percent"] == 10.0
    assert [a["alias"] for a in envelope["accounts"]] == ["main"]  # type: ignore[attr-defined]
    assert statusline.render(rig.status(), now_s=NOW_S).endswith("cursor: 10% $3/$250")

    cursor.delete_marker(rig.root, "cursor")
    rig.client.queue_json(200, fixture_usage())
    assert "cursor" not in web.snapshot(rig.ctx())  # a Claude-only setup emits what it always did


DOM_SHIM = """
class El {
  constructor() { this.children = []; this.dataset = {}; this.style = {}; this.className = '';
    this.hidden = false; this._text = ''; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute() {} addEventListener() {} remove() {}
  querySelectorAll() { return []; }
}
const byId = {};
global.document = {
  getElementById: (id) => (byId[id] = byId[id] || new El()),
  createElement: () => new El(),
  createElementNS: () => new El(),
  createTextNode: (t) => { const e = new El(); e._text = t; return e; },
  querySelectorAll: () => [],
};
global.setInterval = () => 0;
global.fetch = () => Promise.resolve(
  { ok: true, status: 200, json: () => Promise.resolve(ENVELOPE) });
"""

REPORT = """
setTimeout(() => {
  const items = byId['accounts'].children;
  const widths = (n) => (n.style.width ? [n.style.width] : []).concat(...n.children.map(widths));
  console.log(JSON.stringify(items.map(i => i.textContent)));
  console.log(JSON.stringify(items.map(widths)));
  console.log(byId['empty'].hidden, byId['status'].textContent);
}, 50);
"""


def run_page(tmp_path: Path, envelope: dict[str, object]) -> list[str]:
    """Run the page's own script against a minimal DOM; the lines it printed."""
    page = web.page()
    script = page[page.index("<script>") + len("<script>") : page.rindex("</script>")]
    harness = tmp_path / "page.js"
    harness.write_text(f"const ENVELOPE = {json.dumps(envelope)};{DOM_SHIM}{script}{REPORT}")
    done = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip().splitlines()


def web_envelope() -> dict[str, object]:
    return {
        "schema": 1,
        "generated_at": "2026-11-03T12:00:00+00:00",
        "accounts": [
            {
                "alias": "main", "active": True, "state": "ok", "message": None,
                "email": "me@example.com", "display_name": None, "organization": None,
                "plan": "max", "updated_at": None,
                "rows": [{"kind": "session", "label": "Session", "percent": 3,
                          "severity": "normal", "resets_at": "2026-11-03T15:00:00+00:00"}],
                "breakdown": [],
            }
        ],
        "pool": {"accounts": 1, "remaining_account_weeks": 0.9, "dry_in_seconds": None},
        "resets": [],
        "auto_switch": {"enabled": False, "threshold": 99, "last_switch": None},
        "cursor": [
            {
                "alias": "cursor", "kind": "cursor", "state": "ok", "message": None,
                "email": "dev@example.com", "team": "Zvoove", "plan": None,
                "included_percent": 42.4, "on_demand_used_cents": 1250,
                "on_demand_limit_cents": 25000, "resets_at": "2026-11-08T22:45:00+00:00",
                "available": True,
                "available_why": "on-demand $237.50 of $250.00 left",
            }
        ],
    }  # fmt: skip


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_renders_the_seat_as_a_ledger_entry(tmp_path: Path) -> None:
    texts, widths, rest = run_page(tmp_path, web_envelope())

    shown = json.loads(texts)
    assert len(shown) == 2  # main, then the seat
    seat_text = shown[1]
    assert "cursor" in seat_text and "dev@example.com" in seat_text
    assert "Cursor \u00b7 Zvoove" in seat_text
    assert "Included + bonus" in seat_text and "42%" in seat_text
    assert "$12.50 of $250.00 on-demand, billed to the company" in seat_text
    assert "routing: available" in seat_text
    assert json.loads(widths)[1] == ["42%", "5%"]  # both bars painted
    assert rest.startswith("true updated")  # the empty message is hidden, the page says "updated"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("state", ["unknown", "needs_login"])
def test_the_page_says_unknown_or_logged_out_without_inventing_numbers(
    tmp_path: Path, state: str
) -> None:
    envelope = web_envelope()
    seats = envelope["cursor"]
    assert isinstance(seats, list)
    seats[0].update(
        state=state, included_percent=None, on_demand_used_cents=None,
        on_demand_limit_cents=None, resets_at=None, available=None,
        message="usage unknown (HTTP 500)",
    )  # fmt: skip

    out = json.loads(run_page(tmp_path, envelope)[0])[1]

    if state == "unknown":
        assert "usage: unknown (usage unknown (HTTP 500)); see cursor.com/dashboard" in out
    else:
        assert "not logged in" in out and "cursor-agent login" in out
    assert "%" not in out and "$" not in out  # no number is invented


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_claude_only_envelope_renders_as_before(tmp_path: Path) -> None:
    envelope = web_envelope()
    del envelope["cursor"]

    texts, _, rest = run_page(tmp_path, envelope)

    assert len(json.loads(texts)) == 1 and rest.startswith("true updated")
