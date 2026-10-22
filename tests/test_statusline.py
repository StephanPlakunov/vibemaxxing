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
from tests.test_policy import ON
from vibemaxxing import auto, cli, statusline, store
from vibemaxxing.pool import LastSwitch, Settings

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


def document(*accounts: dict[str, object], live: str | None = "extra") -> dict[str, object]:
    return {
        "schema": 1,
        "written_at": NOON - 60.0,
        "live": live,
        "active": live,
        "accounts": list(accounts),
        "policy": None,
        "last_switch": None,
    }


def test_the_line_has_the_documented_shape() -> None:
    doc = document(
        account(
            "main",
            [("session", 100, _at(22, 19, 30)), ("weekly_all", 93, _at(26, 0, 0))],
        ),
        # extra's week rolled over since it was read: that number says nothing now.
        account("extra", [("session", 28, _at(22, 23, 10)), ("weekly_all", 70, NOON - 1)]),
    )

    assert statusline.render(doc, now_s=NOON) == (
        "▶ extra · main: 5h 100% (resets 19:30) wk 93% (resets Mon 00:00)"
        " · extra: 5h 28% (resets 23:10)"
    )


def test_numbers_older_than_fifteen_minutes_get_a_question_mark() -> None:
    rows = [("session", 28, _at(22, 23, 10))]
    old = document(account("extra", rows, fetched_at=NOON - 16 * 60))
    recent = document(account("extra", rows, fetched_at=NOON - 14 * 60))

    assert statusline.render(old, now_s=NOON) == "▶ extra · extra: 5h 28%? (resets 23:10)"
    assert statusline.render(recent, now_s=NOON) == "▶ extra · extra: 5h 28% (resets 23:10)"


def test_rows_without_fresh_data_are_left_out() -> None:
    doc = document(
        account("main", [("session", None, _at(22, 19, 30)), ("weekly_all", 5, None)]),
        account("extra", [("session", 50, NOON - 60.0)]),
        account("side", [], fetched_at=None),
        account("gone", [("session", 50, _at(22, 19, 30))], state="needs_login"),
        live=None,
    )

    assert statusline.render(doc, now_s=NOON) == (
        "main: wk 5% · extra: - · side: - · gone: needs login"
    )


def test_a_model_row_shows_only_when_the_policy_counts_it() -> None:
    main = account("main", [("session", 10, None), ("weekly_scoped", 40, None)])
    main["rows"][1]["label"] = "Weekly · Fable"
    plain = document(main, live="main")
    counted = dict(plain, policy={"mode": "ordered", "steps": [], "block_models": ["fable"]})

    assert statusline.render(plain, now_s=NOON) == "▶ main · main: 5h 10%"
    assert statusline.render(counted, now_s=NOON) == "▶ main · main: 5h 10% Fable 40%"


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
    w = world(tmp_home, {"main": (40, 85), "extra": (10, 10)}, live="main")
    auto.write_settings(w.root, ON)
    auto.write_last(w.root, LastSwitch("extra", "main", NOW_S - 50.0, "undo", None))

    assert cli.main(["list"], context=w.context()) == 0

    path = statusline.status_path(w.root)
    assert path == w.root / "status.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(w.root.glob("status.json*.tmp"))
    status = _status(w.root)
    assert (status["schema"], status["live"], status["active"]) == (1, "main", "main")
    assert status["written_at"] == NOW_S
    assert status["policy"] == auto.policy_document(ON.policy)
    assert status["last_switch"] == {
        "from": "extra",
        "to": "main",
        "at": NOW_S - 50.0,
        "reason": "undo",
    }
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


def test_every_tick_writes_the_status_even_when_it_stays(tmp_home: Path) -> None:
    w = world(tmp_home, {"main": (40, 40), "extra": (10, 10)}, live="main", on=False)

    w.tick()

    assert _status(w.root)["live"] is None, "an off tick does not look at the Keychain"
    assert _status(w.root)["active"] == "main"
    assert w.port.log == [] and w.client.requests == []

    auto.write_settings(w.root, Settings(auto_switch=True))
    w.tick()
    assert _status(w.root)["live"] == "main"


def test_a_tick_that_switches_writes_the_new_live_account(tmp_home: Path) -> None:
    w = world(tmp_home, {"main": (40, 85), "extra": (10, 10)}, live="main")
    auto.write_settings(w.root, ON)

    assert w.tick().switched

    status = _status(w.root)
    assert (status["live"], status["active"]) == ("extra", "extra")
    last = status["last_switch"]
    assert isinstance(last, dict) and last["to"] == "extra"


def test_status_and_usage_once_write_it_too(tmp_home: Path) -> None:
    w = world(tmp_home, {"main": (40, 40), "extra": (10, 10)}, live="main")

    for argv in (["auto", "status"], ["usage", "--once"], ["--json"]):
        statusline.status_path(w.root).unlink(missing_ok=True)
        assert cli.main(argv, context=w.context()) == 0, argv
        assert _status(w.root)["live"] == "main", argv


def test_a_status_that_cannot_be_written_costs_nothing(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_home, {"main": (40, 40), "extra": (10, 10)}, live="main")

    def full(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(statusline, "write", full)

    assert cli.main(["list"], context=w.context()) == 0
    assert w.tick().line == "stay: main is below 99%"


# --- who reads it -------------------------------------------------------------


def test_statusline_reads_the_file_and_nothing_else(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world(tmp_home, {"main": (40, 85), "extra": (10, 10)}, live="main")
    assert cli.main(["list"], context=w.context()) == 0
    capsys.readouterr()
    client, port = FakeHttpClient(), FakeKeychain()
    ctx = cli.Context(root=w.root, client=client, port=port, now_s=NOW_S + 60.0)

    assert cli.main(["statusline"], context=ctx) == 0

    assert capsys.readouterr().out.startswith("▶ main · extra: 5h 10% (resets ")
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
    assert line.startswith("▶ main · main: 5h 40% (resets ")
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
