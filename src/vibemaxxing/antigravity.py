"""Antigravity accounts backed by the Google CLI's read-only `/usage` panel.

The CLI owns the Google login. Its documented quota command is interactive, so
we open it in a private workspace and only submit it after the CLI identifies
it in the command menu. No model prompt, raw terminal output or credentials are
stored by vibe.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import termios
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol, TypedDict

from vibemaxxing import store
from vibemaxxing.errors import UsageError
from vibemaxxing.fsutil import private_dir, write_private

KIND: Final = "antigravity"
NAME: Final = "Antigravity"
FRESH_S: Final = 55.0
_TIMEOUT_S: Final = 12.0
_PANEL_SETTLE_S: Final = 0.25
_MAX_OUTPUT: Final = 128 * 1024
_MESSAGES: Final = {
    "needs_login": "not logged in — run: agy",
    "not_installed": "agy is not installed or on PATH",
    "no_usage": "no usage reported",
    "unknown": "usage: unknown (could not read agy /usage)",
}
_GROUPS: Final = {"GEMINI MODELS": "Gemini", "CLAUDE AND GPT MODELS": "Claude/GPT"}
_PERIODS: Final = {"Weekly Limit Remaining": "wk", "Five Hour Limit Remaining": "5h"}
_ANSI: Final = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
_PROMPT: Final = re.compile(r"(?m)^> *\r?$")
_LOGIN: Final = re.compile(r"(?im)^\s*(?:>\s*)?sign in with google\b")
_COMMAND: Final = re.compile(r"(?m)^\s*> /?usage +View model quota usage\s*$")
_PERCENT: Final = re.compile(r"\[[^\[\]]*\]\s+(\d{1,3}(?:\.\d{1,2})?)%")
_RESET: Final = re.compile(r"Refreshes in ((?:\d{1,3}\s*[dhms]\s*){1,4})")
_SECONDS: Final = {"d": 86400, "h": 3600, "m": 60, "s": 1}


class Window(TypedDict):
    group: str
    period: str
    remaining_percent: float
    resets_at: str | None


class Entry(TypedDict):
    alias: str
    kind: str
    state: str
    message: str | None
    fetched_at: float | None
    windows: tuple[Window, ...]


@dataclass(frozen=True)
class Reading:
    state: str
    windows: tuple[Window, ...] = ()

    @property
    def message(self) -> str | None:
        return _MESSAGES.get(self.state)

    def payload(self) -> dict[str, object]:
        return {"state": self.state, "windows": self.windows}


class UsagePort(Protocol):
    def read(self, workspace: Path, *, now_s: float) -> Reading: ...


def _reset_at(line: str, now_s: float) -> str | None:
    match = _RESET.fullmatch(line)
    if match is None:
        return None
    seconds = sum(
        int(count) * _SECONDS[unit] for count, unit in re.findall(r"(\d+)\s*([dhms])", match[1])
    )
    try:
        return datetime.fromtimestamp(now_s + seconds).astimezone().isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None


def parse_panel(text: str, *, now_s: float) -> Reading:
    """Read the reported percentages and any displayed reset countdown."""
    _, _, panel = text.rpartition("Models & Quota")
    lines = [line.strip() for line in panel.splitlines() if line.strip()]
    group: str | None = None
    windows: dict[tuple[str, str], Window] = {}
    for index, line in enumerate(lines):
        if line.isupper():
            group = _GROUPS.get(line)
        period = _PERIODS.get(line)
        if group is None or period is None or index + 1 >= len(lines):
            continue
        value = _PERCENT.fullmatch(lines[index + 1])
        if value is None or not 0 <= (remaining := float(value[1])) <= 100:
            continue
        reset = _reset_at(lines[index + 2], now_s) if index + 2 < len(lines) else None
        windows[group, period] = Window(
            group=group, period=period, remaining_percent=remaining, resets_at=reset
        )
    return Reading("ok", tuple(windows.values())) if windows else Reading("no_usage")


def _receive(fd: int, deadline: float, ready: Callable[[str], bool], *, settle_s: float = 0) -> str:
    data = bytearray()
    text = ""
    until = deadline
    while (left := until - time.monotonic()) > 0 and len(data) < _MAX_OUTPUT:
        if not select.select([fd], [], [], left)[0]:
            break
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        data.extend(chunk)
        text = _ANSI.sub("", data.decode("utf-8", errors="replace"))
        until = min(deadline, time.monotonic() + settle_s) if ready(text) else deadline
    return text


def _bin() -> str | None:
    if found := shutil.which("agy"):
        return found
    # launchd starts with a minimal PATH. Homebrew's cask keeps its wrapper here.
    for path in (
        "/opt/homebrew/bin/agy",
        "/usr/local/bin/agy",
        str(Path.home() / ".local/bin/agy"),
    ):
        if os.access(path, os.X_OK) and Path(path).is_file():
            return path
    return None


def _query(master: int, *, now_s: float) -> Reading:
    deadline = time.monotonic() + _TIMEOUT_S
    start = _receive(
        master, deadline, lambda text: bool(_PROMPT.search(text) or _LOGIN.search(text))
    )
    if _LOGIN.search(start):
        return Reading("needs_login")
    if not _PROMPT.search(start):
        return Reading("unknown")
    os.write(master, b"/usage")
    menu = _receive(master, deadline, lambda text: bool(_COMMAND.search(text)))
    if not _COMMAND.search(menu):
        return Reading("unknown")
    os.write(master, b"\r")
    # The panel can open with a loading frame before the account's limits arrive.
    panel = _receive(
        master,
        deadline,
        lambda text: "esc Close" in text and bool(parse_panel(text, now_s=now_s).windows),
        settle_s=_PANEL_SETTLE_S,
    )
    if "Models & Quota" not in panel or "esc Close" not in panel:
        return Reading("unknown")
    return parse_panel(panel, now_s=now_s)


class CliUsage:
    def read(self, workspace: Path, *, now_s: float) -> Reading:
        binary = _bin()
        if binary is None:
            return Reading("not_installed")
        try:
            workspace = private_dir(workspace).resolve()
            # The empty workspace is trusted for this invocation only.
            settings = json.dumps({"trustedWorkspaces": [str(workspace)]})
            master, slave = pty.openpty()
            try:
                try:
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 80, 160, 0, 0))
                    child = subprocess.Popen(
                        [binary, "--log-file", os.devnull, "--settings", settings],
                        cwd=workspace,
                        env={
                            **os.environ,
                            "TERM": "xterm-256color",
                            "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
                        },
                        stdin=slave,
                        stdout=slave,
                        stderr=slave,
                        start_new_session=True,
                    )
                finally:
                    os.close(slave)
                try:
                    return _query(master, now_s=now_s)
                finally:
                    child.terminate()
                    try:
                        child.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
            finally:
                os.close(master)
        except OSError:
            return Reading("unknown")


def _path(root: Path, alias: str) -> Path:
    if not store.valid_alias(alias):
        raise UsageError(f"{alias!r} is not a usable alias", "vibe list")
    return root / KIND / f"{alias}.json"


def aliases(root: Path) -> list[str]:
    return sorted(
        path.stem for path in (root / KIND).glob("*.json") if store.valid_alias(path.stem)
    )


def has_account(root: Path, alias: str) -> bool:
    return store.valid_alias(alias) and _path(root, alias).is_file()


def write_marker(root: Path, alias: str, *, added_at: float) -> None:
    path = _path(root, alias)
    private_dir(root)
    private_dir(path.parent)
    marker = {"schema": store.SCHEMA_VERSION, "alias": alias, "kind": KIND, "added_at": added_at}
    write_private(path, json.dumps(marker, indent=2) + "\n")


def remove(root: Path, alias: str) -> None:
    _path(root, alias).unlink(missing_ok=True)
    store.usage_path(root, alias).unlink(missing_ok=True)


def _added_at(root: Path, alias: str) -> float | None:
    try:
        marker = json.loads(_path(root, alias).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict) or marker.get("kind") != KIND:
        return None
    added = marker.get("added_at")
    return float(added) if isinstance(added, (float, int)) and not isinstance(added, bool) else None


def rename(root: Path, old: str, new: str) -> None:
    added = _added_at(root, old)
    if added is None:
        raise UsageError(f'cannot read Antigravity account "{old}"', "vibe list")
    write_marker(root, new, added_at=added)
    remove(root, old)


def _cached(root: Path, alias: str, added_at: float) -> tuple[Reading, float | None]:
    cached = store.read_usage_cache(root, alias)
    if cached is None or cached.added_at != added_at or cached.payload is None:
        return Reading("unknown"), None
    payload = cached.payload
    state = payload.get("state")
    if isinstance(state, str) and state in _MESSAGES:
        return Reading(state), cached.fetched_at
    windows: list[Window] = []
    raw = payload.get("windows")
    for item in raw if isinstance(raw, list) and state == "ok" else []:
        if not isinstance(item, dict):
            continue
        group, period, percent, reset = (
            item.get("group"),
            item.get("period"),
            item.get("remaining_percent"),
            item.get("resets_at"),
        )
        if (
            isinstance(group, str)
            and group in _GROUPS.values()
            and isinstance(period, str)
            and period in _PERIODS.values()
            and isinstance(percent, (int, float))
            and not isinstance(percent, bool)
            and 0 <= percent <= 100
        ):
            windows.append(
                Window(
                    group=group,
                    period=period,
                    remaining_percent=float(percent),
                    resets_at=reset if isinstance(reset, str) else None,
                )
            )
    if windows:
        return Reading("ok", tuple(windows)), cached.fetched_at
    return Reading("unknown"), None


def _read(root: Path, alias: str, *, now_s: float, port: UsagePort | None) -> Entry:
    added_at = _added_at(root, alias)
    reading, fetched_at = (
        (Reading("unknown"), None) if added_at is None else _cached(root, alias, added_at)
    )
    recent = fetched_at is not None and 0 <= now_s - fetched_at < FRESH_S
    if port is not None and added_at is not None and not recent:
        try:
            claimed = store.claim_refresh(root, alias, now_s=now_s)
        except OSError:
            claimed = False
        if claimed:
            try:
                # The last reader could have finished while we were acquiring the claim.
                reading, fetched_at = _cached(root, alias, added_at)
                if fetched_at is None or not 0 <= now_s - fetched_at < FRESH_S:
                    reading = port.read(root / KIND / "workspace", now_s=now_s)
                    fetched_at = now_s
                    if _added_at(root, alias) == added_at:
                        store.write_usage_cache(
                            root,
                            alias,
                            store.UsageCache(
                                added_at, fetched_at, reading.payload(), None, 0, None
                            ),
                        )
            finally:
                store.release_refresh(root, alias)
    return {
        "alias": alias,
        "kind": KIND,
        "message": reading.message,
        "fetched_at": fetched_at,
        "state": reading.state,
        "windows": reading.windows,
    }


def entries(root: Path, *, now_s: float, port: UsagePort | None = None) -> list[Entry]:
    """All registered accounts, from cache only when the caller supplies no port."""
    return [_read(root, alias, now_s=now_s, port=port) for alias in aliases(root)]


def human_lines(entry: Entry) -> list[str]:
    lines = [f"{entry['alias']}  {NAME}"]
    if entry["message"]:
        lines.append(f"  {entry['message']}")
    for window in entry["windows"]:
        reset = ""
        if isinstance(window["resets_at"], str):
            try:
                stamp = datetime.fromisoformat(window["resets_at"]).astimezone()
                reset = f"  resets {stamp:%a %d %b %H:%M}"
            except ValueError:
                pass
        lines.append(
            f"  {window['group']} {window['period']}: {window['remaining_percent']:g}% left{reset}"
        )
    return lines
