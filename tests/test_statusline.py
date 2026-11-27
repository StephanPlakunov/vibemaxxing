"""`~/.vibemaxxing/status.json` and `vibe statusline`.

Everything that collects usage, and every tick, writes the file. The status line reads
that file and nothing else -- no Keychain, no network, no refresh -- because Claude Code
runs it on every redraw.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient, FakeKeychain
from tests.test_auto import NOW_S, world
from vibemaxxing import cli, statusline, store

# Thursday 2026-10-22, noon, local time: the line prints local times.
NOON = datetime(2026, 10, 22, 12, 0).timestamp()


def _at(day: int, hour: int, minute: int) -> float:
    return datetime(2026, 10, day, hour, minute).timestamp()


def account(
    alias: str,
    rows: list[tuple[str, int | None, float | None]],
    *,
    fetched_at: float | None = NOON - 60.0,
    state: str = "ok",
) -> dict[str, object]:
    labels = {"session": "Session", "weekly_all": "Weekly · all models"}
    return {
        "alias": alias,
        "state": state,
        "fetched_at": fetched_at,
        "rows": [
            {"kind": kind, "label": labels.get(kind, kind), "percent": percent, "resets_at": at}
            for kind, percent, at in rows
        ],
    }


def document(*accounts: dict[str, object], live: str | None = "alt") -> dict[str, object]:
    return {
        "schema": 1,
        "written_at": NOON - 60.0,
        "live": live,
        "active": live,
        "accounts": list(accounts),
    }


def test_the_line_has_the_documented_shape() -> None:
    doc = document(
        account(
            "main",
            [("session", 100, _at(22, 19, 30)), ("weekly_all", 93, _at(26, 0, 0))],
        ),
        # alt's week rolled over since it was read: that number says nothing now.
        account("alt", [("session", 28, _at(22, 23, 10)), ("weekly_all", 70, NOON - 1)]),
    )

    # Every account is one cell: the percent used of the limit that stops work first (here
    # main's 5-hour window), with its reset once it is at or near 100%.
    assert statusline.render(doc, now_s=NOON) == "▶ alt · main: 100% (resets 19:30) · alt: 28%"


def test_the_weekly_window_decides_when_it_is_the_more_used_one() -> None:
    doc = document(
        account("main", [("session", 12, _at(22, 19, 30)), ("weekly_all", 97, _at(26, 0, 0))])
    )

    assert statusline.render(doc, now_s=NOON) == "▶ alt · main: 97% (resets Mon 00:00)"


def test_the_reset_shows_from_ninety_percent() -> None:
    def line(percent: int) -> str:
        doc = document(account("main", [("weekly_all", percent, _at(26, 0, 0))]))
        return statusline.render(doc, now_s=NOON)

    assert line(89) == "▶ alt · main: 89%"
    assert line(90) == "▶ alt · main: 90% (resets Mon 00:00)"


def test_an_account_with_only_its_weekly_window_shows_its_number() -> None:
    doc = document(account("main", [("weekly_all", 53, _at(26, 0, 0))]))

    assert statusline.render(doc, now_s=NOON) == "▶ alt · main: 53%"


def test_numbers_older_than_fifteen_minutes_get_a_question_mark() -> None:
    rows = [("session", 28, _at(22, 23, 10))]
    old = document(account("alt", rows, fetched_at=NOON - 16 * 60))
    recent = document(account("alt", rows, fetched_at=NOON - 14 * 60))

    assert statusline.render(old, now_s=NOON) == "▶ alt · alt: 28%?"
    assert statusline.render(recent, now_s=NOON) == "▶ alt · alt: 28%"


def test_rows_without_fresh_data_are_left_out() -> None:
    doc = document(
        account("main", [("session", None, _at(22, 19, 30)), ("weekly_all", 5, None)]),
        account("alt", [("session", 50, NOON - 60.0)]),
        account("side", [], fetched_at=None),
        account("gone", [("session", 50, _at(22, 19, 30))], state="needs_login"),
        live=None,
    )

    assert statusline.render(doc, now_s=NOON) == ("main: 5% · alt: - · side: ? · gone: needs login")


def test_a_models_own_row_is_not_shown() -> None:
    main = account("main", [("session", 10, None), ("weekly_scoped", 40, None)])
    main["rows"][1]["label"] = "Weekly · Fable"
    plain = document(main, live="main")
    # An old status file may still carry a policy that named the model: it changes nothing.
    old = dict(plain, policy={"mode": "ordered", "steps": [], "block_models": ["fable"]})

    assert statusline.render(plain, now_s=NOON) == "▶ main · main: 10%"
    assert statusline.render(old, now_s=NOON) == "▶ main · main: 10%"


def test_no_status_yet_is_a_line_not_an_error(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    assert statusline.read(root) is None
    root.mkdir()
    for raw in ("", "{ nope", "[]", '{"schema": 2}'):
        statusline.status_path(root).write_text(raw)
        assert statusline.read(root) is None, raw
    assert statusline.render(None, now_s=NOON) == "vibe: no status yet"


def test_the_line_has_no_emoji() -> None:
    doc = document(account("main", [("session", 100, _at(22, 19, 30))]), live="main")

    line = statusline.render(doc, now_s=NOON)

    assert all(ord(char) < 0x1F000 and char != "️" for char in line)


def test_it_lives_in_the_store() -> None:
    assert statusline.default_root() == store.store_root()


# --- who writes it ------------------------------------------------------------


def _status(root: Path) -> dict[str, object]:
    found = json.loads(statusline.status_path(root).read_text())
    assert isinstance(found, dict)
    return found


def test_list_writes_the_status_owner_only_and_atomically(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world(tmp_home, {"main": (40, 85), "alt": (10, 10)}, live="main")

    assert cli.main(["list"], context=w.context()) == 0

    path = statusline.status_path(w.root)
    assert path == w.root / "status.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(w.root.glob("status.json*.tmp"))
    status = _status(w.root)
    assert (status["schema"], status["live"], status["active"]) == (1, "main", "main")
    assert status["written_at"] == NOW_S
    assert "policy" not in status and "last_switch" not in status
    accounts = status["accounts"]
    assert isinstance(accounts, list)
    main = next(a for a in accounts if a["alias"] == "main")
    assert main["state"] == "ok" and main["fetched_at"] == NOW_S - 10.0
    assert [(r["kind"], r["percent"]) for r in main["rows"]] == [
        ("session", 40),
        ("weekly_all", 85),
    ]
    assert main["rows"][0]["resets_at"] == NOW_S + 3 * 3600
    assert "main-live" not in path.read_text() and "main-rt" not in path.read_text()


def test_every_tick_writes_the_status(tmp_home: Path) -> None:
    w = world(tmp_home, {"main": (40, 40), "alt": (10, 10)}, live="main")

    w.tick()

    assert _status(w.root)["live"] == "main", "a tick says who is live"
    assert _status(w.root)["active"] == "main"
    assert set(w.port.log) <= {"keychain.read"}


def test_usage_once_writes_it_too(tmp_home: Path) -> None:
    w = world(tmp_home, {"main": (40, 40), "alt": (10, 10)}, live="main")

    for argv in (["usage", "--once"], ["--json"]):
        statusline.status_path(w.root).unlink(missing_ok=True)
        assert cli.main(argv, context=w.context()) == 0, argv
        assert _status(w.root)["live"] == "main", argv


def test_a_status_that_cannot_be_written_costs_nothing(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_home, {"main": (40, 40), "alt": (10, 10)}, live="main")

    def full(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(statusline, "write", full)

    assert cli.main(["list"], context=w.context()) == 0
    assert w.tick().line == "live main"


# --- who reads it -------------------------------------------------------------


def test_statusline_reads_the_file_and_nothing_else(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world(tmp_home, {"main": (40, 85), "alt": (10, 10)}, live="main")
    assert cli.main(["list"], context=w.context()) == 0
    capsys.readouterr()
    client, port = FakeHttpClient(), FakeKeychain()
    ctx = cli.Context(root=w.root, client=client, port=port, now_s=NOW_S + 60.0)

    assert cli.main(["statusline"], context=ctx) == 0

    assert capsys.readouterr().out.startswith("▶ main · alt: 10% · main: 85%")
    assert client.requests == [] and port.log == []


def test_the_console_script_answers_statusline_without_loading_the_rest(tmp_home: Path) -> None:
    # Against the real clock: the child process reads time.time(), not a test's NOW.
    now = time.time()
    statusline.write(
        tmp_home / ".vibemaxxing",
        dict(
            document(account("main", [("session", 40, now + 3600)], fetched_at=now - 60)),
            live="main",
        ),
    )
    probe = (
        "import sys; sys.argv = ['vibe', 'statusline']\n"
        "from vibemaxxing.entry import main\n"
        "code = main()\n"
        "heavy = {'vibemaxxing.cli', 'textual', 'urllib.request', 'importlib.metadata'}\n"
        "print('loaded:', sorted(heavy & set(sys.modules)))\n"
        "sys.exit(code)\n"
    )

    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_home)},
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    line, loaded = proc.stdout.splitlines()
    assert line == "▶ main · main: 40%"
    assert loaded == "loaded: []"


def test_the_console_script_still_runs_every_other_command(tmp_home: Path) -> None:
    probe = (
        "import sys; sys.argv = ['vibe', '--version']\n"
        "from vibemaxxing.entry import main\n"
        "sys.exit(main())\n"
    )

    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_home)},
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == importlib.metadata.version("vibemaxxing")


def test_an_enterprise_seat_shows_its_monthly_spend_like_cursor() -> None:
    from vibemaxxing import statusline

    row = {
        "kind": "monthly_spend",
        "label": "Monthly spend",
        "percent": 0,
        "resets_at": None,
        "spend_used": 0.15,
        "spend_limit": 125.0,
        "currency": "USD",
    }
    doc = {
        "schema": 1,
        "accounts": [{"alias": "zvoove-claude", "state": "ok", "fetched_at": 100.0, "rows": [row]}],
    }
    assert statusline.render(doc, now_s=110.0) == "zvoove-claude: 0%"
    assert statusline.render(doc, now_s=100.0 + 3600).endswith("zvoove-claude: 0%?")


def test_the_spend_rows_carry_their_percent_in_brackets() -> None:
    from vibemaxxing import statusline

    spend = {"kind": "monthly_spend", "spend_used": 119.21, "spend_limit": 125.0}
    claude = {"alias": "zvoove-claude", "state": "ok", "fetched_at": 100.0}
    codex = {
        "alias": "zvoove-codex",
        "fetched_at": 100.0,
        "spend": {"used": 3570.58, "limit": 5100, "unit": "credit"},
    }
    reported = {**claude, "rows": [{**spend, "percent": 95.4}]}
    computed = {**claude, "rows": [spend]}

    line = statusline.render({"schema": 1, "accounts": [reported], "codex": [codex]}, now_s=110.0)
    assert "zvoove-claude: 95%" in line
    assert "zvoove-codex: 70%" in line
    assert statusline.render({"schema": 1, "accounts": [computed]}, now_s=110.0) == (
        "zvoove-claude: 95%"
    )
