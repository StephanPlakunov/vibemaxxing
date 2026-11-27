"""Antigravity quota updates use a fixture and a private, fake CLI."""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient, FakeKeychain
from tests.test_cursor_display import run_page
from vibemaxxing import antigravity, cli, statusline, store, web

NOW = 1_757_930_000.0


@pytest.fixture
def panel() -> str:
    return (Path(__file__).parent / "fixtures" / "antigravity_usage.txt").read_text(
        encoding="utf-8"
    )


def test_reads_only_reported_percentages_and_reset_times(panel: str) -> None:
    report = antigravity.parse_panel(panel, now_s=NOW)
    assert report.state == "ok"
    assert [(w["group"], w["period"], w["remaining_percent"]) for w in report.windows] == [
        ("Gemini", "wk", 91.25),
        ("Gemini", "5h", 60.5),
        ("Claude/GPT", "wk", 100),
        ("Claude/GPT", "5h", 0),
    ]
    stamp = report.windows[0]["resets_at"]
    assert stamp is not None
    assert datetime.fromisoformat(stamp).timestamp() == NOW + 6 * 86400 + 23 * 3600 + 56 * 60
    assert report.windows[2]["resets_at"] is None
    assert "agy-fixture@example.com" not in repr(report.payload())


def test_no_usage_for_an_empty_panel_or_invalid_percentages(panel: str) -> None:
    assert antigravity.parse_panel("Models & Quota\nNo quota available", now_s=NOW).state == (
        "no_usage"
    )
    for value in ("91.25%", "60.50%", "100.00%", "0.00%"):
        panel = panel.replace(value, "200%")
    assert antigravity.parse_panel(panel, now_s=NOW).windows == ()


def test_pty_waits_for_the_quota_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, panel: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = tmp_path / "fake-agy"
    early, last = panel.rsplit("Five Hour Limit Remaining", 1)
    fake.write_text(
        f"#!{sys.executable}\n"
        "import os, time, tty\n"
        "tty.setraw(0)\n"
        "os.write(1, b'\\x1b[1m>\\x1b[0m\\r\\n')\n"
        "assert os.read(0, 64) == b'/usage'\n"
        "os.write(1, b'> /usage  View model quota usage\\r\\n')\n"
        "assert os.read(0, 64) == b'\\r'\n"
        "os.write(1, b'Models & Quota\\r\\nLoading...\\r\\nesc Close\\r\\n')\n"
        "time.sleep(0.05)\n"
        f"os.write(1, {early.encode()!r})\n"
        "time.sleep(0.05)\n"
        f"os.write(1, {('Five Hour Limit Remaining' + last).encode()!r})\n"
        "time.sleep(30)\n"
    )
    fake.chmod(0o700)
    monkeypatch.setattr(antigravity, "_bin", lambda: str(fake))
    report = antigravity.CliUsage().read(tmp_path / "workspace", now_s=NOW)
    assert report == antigravity.parse_panel(panel, now_s=NOW)
    assert capsys.readouterr() == ("", "")


def test_does_not_submit_unrecognized_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    screens = iter([">\r\n", "/usage  User-provided skill"])
    written: list[bytes] = []
    monkeypatch.setattr(antigravity, "_receive", lambda *_: next(screens))
    monkeypatch.setattr(antigravity.os, "write", lambda _, data: written.append(data))
    assert antigravity._query(0, now_s=NOW).state == "unknown"
    assert written == [b"/usage"]


class StubUsage:
    def __init__(self, report: antigravity.Reading) -> None:
        self.report = report
        self.calls: list[float] = []

    def read(self, workspace: Path, *, now_s: float) -> antigravity.Reading:
        assert workspace.name == "workspace"
        self.calls.append(now_s)
        return self.report


def context(root: Path, stub: StubUsage) -> cli.Context:
    return cli.Context(
        root=root,
        client=FakeHttpClient(),
        port=FakeKeychain(),
        now_s=NOW,
        clock=lambda: NOW,
        antigravity_port=stub,
    )


def test_cli_tick_and_statusline(
    tmp_home: Path, panel: str, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = StubUsage(antigravity.parse_panel(panel, now_s=NOW))
    ctx = context(tmp_home / ".vibemaxxing", stub)

    assert cli.main(["add", "--kind", "antigravity"], context=ctx) == 0
    assert stub.calls == []
    assert cli.main(["list"], context=ctx) == 0
    listed = capsys.readouterr().out
    assert "antigravity  Antigravity" in listed
    # Rows like every other account's: label, bar, the share USED (100 - 91.25), the reset.
    weekly = next(line for line in listed.splitlines() if "Weekly · Gemini" in line)
    assert "█" in weekly and weekly.split("█", 1)[0].strip() == "Weekly · Gemini"
    assert "   9%" in weekly and "left" not in listed
    assert "agy-fixture@example.com" not in listed

    assert cli.main(["usage", "--json"], context=ctx) == 0
    usage = json.loads(capsys.readouterr().out)
    assert usage["antigravity"][0]["windows"][1]["remaining_percent"] == 60.5
    assert usage["pool"]["accounts"] == 0
    assert stub.calls == [NOW]

    later = replace(ctx, now_s=NOW + 60)
    assert cli.main(["auto", "tick"], context=later) == 0
    capsys.readouterr()
    assert cli.main(["statusline"], context=later) == 0
    assert capsys.readouterr().out.strip() == (
        # Gemini (5h 40%, wk 9%) still has room; Claude/GPT's 5-hour window is used up.
        "antigravity: 40%"
    )
    assert stub.calls == [NOW, NOW + 60]

    assert cli.main(["switch", "antigravity"], context=ctx) == 2
    assert cli.main(["run", "antigravity", "--", "claude"], context=ctx) == 2
    assert cli.main(["add", "antigravity", "--kind", "codex"], context=ctx) == 2
    assert cli.main(["alias", "antigravity", "google"], context=ctx) == 0
    assert antigravity.aliases(ctx.root) == ["google"]
    assert not store.usage_path(ctx.root, "antigravity").exists()
    assert cli.main(["remove", "google"], context=ctx) == 0
    assert antigravity.aliases(ctx.root) == []


@pytest.mark.parametrize("state", ["ok", "no_usage"])
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_web_displays_each_quota_or_the_empty_state(
    tmp_home: Path, tmp_path: Path, panel: str, state: str
) -> None:
    report = (
        antigravity.parse_panel(panel, now_s=NOW)
        if state == "ok"
        else (antigravity.Reading("no_usage"))
    )
    ctx = context(tmp_home / ".vibemaxxing", StubUsage(report))
    antigravity.write_marker(ctx.root, "google", added_at=NOW)

    snapshot = web.snapshot(ctx)
    cards, widths, _ = run_page(tmp_path, snapshot)
    assert len(json.loads(cards)) == 1
    assert "google" in cards and "Antigravity" in cards
    if state == "ok":
        assert "Gemini weekly used" in cards and "Claude/GPT 5h used" in cards
        assert json.loads(widths) == [["8.75%", "39.5%", "0%", "100%"]]
    else:
        assert "no usage reported" in cards
        assert "%" not in cards
        assert (
            statusline.render(statusline.read(ctx.root), now_s=NOW) == "google: no usage reported"
        )
