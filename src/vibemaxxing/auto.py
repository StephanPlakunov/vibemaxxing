"""Automatic account switching: the settings, one tick, undo, and the launchd job.

Sits above envelope, which it asks for the accounts' numbers, and below cli. The
decision itself is `pool.decide`, pure; everything here is the I/O around it.

A tick is the unit a scheduler calls. It never writes the Keychain except through
`store.switch`, which holds the refresh claims and refuses a login vibe does not
know or an account that is dead.
"""

from __future__ import annotations

import json
import math
import os
import plistlib
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from vibemaxxing import envelope, oauth, pool, store, usage
from vibemaxxing.envelope import AccountView
from vibemaxxing.errors import NeedsLoginError, NetworkError, UsageError
from vibemaxxing.fsutil import private_dir, write_private
from vibemaxxing.httpclient import HttpClient
from vibemaxxing.keychain import KeychainPort
from vibemaxxing.pool import (
    DEFAULT_COOLDOWN_S,
    DEFAULT_THRESHOLD,
    MAX_THRESHOLD,
    MIN_THRESHOLD,
    LastSwitch,
    Reading,
    Settings,
)

SCHEMA_VERSION: Final = 1
LABEL: Final = "de.intra-ai.vibemaxxing-auto"
TICK_INTERVAL_S: Final = 60
# The live account is polled at the 60 s floor, not the 3 minute cadence, once a trigger
# row is this full: finding out late that it is spent costs the work in flight.
FAST_POLL_PERCENT: Final = 90
OSASCRIPT_BIN: Final = "/usr/bin/osascript"
OSASCRIPT_TIMEOUT_S: Final = 5.0


def settings_path(root: Path) -> Path:
    return root / "settings.json"


def last_path(root: Path) -> Path:
    return root / "auto.json"


def _int(value: object) -> int | None:
    # bool is an int; a hand-edited `true` is not a threshold of 1.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def read_settings(root: Path) -> Settings:
    """The settings file, or the defaults for a file that is missing or wrong anywhere.

    All of it, not the valid half: a hand-edited file that is wrong in one place must
    leave auto-switch off rather than switch on a guess about what was meant. A bad
    file is never an error, so `vibe list` keeps working. This is the one place the
    file is validated.
    """
    try:
        parsed: object = json.loads(settings_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(parsed, dict) or parsed.get("schema") != SCHEMA_VERSION:
        return Settings()
    enabled = parsed.get("auto_switch", False)
    threshold = _int(parsed.get("threshold", DEFAULT_THRESHOLD))
    cooldown = _int(parsed.get("cooldown_s", DEFAULT_COOLDOWN_S))
    excluded = parsed.get("excluded", [])
    if (
        not isinstance(enabled, bool)
        or threshold is None
        or not MIN_THRESHOLD <= threshold <= MAX_THRESHOLD
        or cooldown is None
        or cooldown < 0
        or not isinstance(excluded, list)
        or not all(isinstance(alias, str) and store.valid_alias(alias) for alias in excluded)
    ):
        return Settings()
    return Settings(enabled, threshold, cooldown, tuple(dict.fromkeys(str(a) for a in excluded)))


def write_settings(root: Path, settings: Settings) -> None:
    private_dir(root)
    document = {
        "schema": SCHEMA_VERSION,
        "auto_switch": settings.auto_switch,
        "threshold": settings.threshold,
        "cooldown_s": settings.cooldown_s,
        "excluded": list(settings.excluded),
    }
    write_private(settings_path(root), json.dumps(document, indent=2) + "\n")


def read_last(root: Path) -> LastSwitch | None:
    """The last switch, or None for a record that is missing or unreadable."""
    try:
        parsed: object = json.loads(last_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict) or parsed.get("schema") != SCHEMA_VERSION:
        return None
    origin, target, reason = parsed.get("from"), parsed.get("to"), parsed.get("reason")
    at = _number(parsed.get("at"))
    if not (isinstance(origin, str) and isinstance(target, str) and isinstance(reason, str)):
        return None
    if at is None:
        return None
    return LastSwitch(origin, target, at, reason, _number(parsed.get("until")))


def write_last(root: Path, last: LastSwitch) -> None:
    private_dir(root)
    document = {
        "schema": SCHEMA_VERSION,
        "from": last.from_alias,
        "to": last.to_alias,
        "at": last.at_s,
        "reason": last.reason,
        "until": last.until_s,
    }
    write_private(last_path(root), json.dumps(document, indent=2) + "\n")


def _readings(views: Sequence[AccountView]) -> list[Reading]:
    return [
        Reading(
            view.alias,
            view.state,
            view.summary.rows if view.summary is not None else None,
            view.updated_at,
        )
        for view in views
    ]


def _is_hot(root: Path, alias: str, now_s: float) -> bool:
    """Whether the cached answer for ``alias`` already has a trigger row at FAST_POLL_PERCENT."""
    cached = store.read_usage_cache(root, alias)
    if cached is None or cached.payload is None:
        return False
    return bool(pool.blocking(usage.summarize(cached.payload).rows, FAST_POLL_PERCENT, now_s))


@dataclass(frozen=True)
class TickResult:
    line: str
    switched: bool


def _stay(reason: str) -> TickResult:
    return TickResult(f"stay: {reason}", switched=False)


def tick(
    root: Path,
    *,
    client: HttpClient,
    port: KeychainPort,
    now_s: float,
    notify: Callable[[str], None],
) -> TickResult:
    """One evaluation and, if it is due, one switch. Safe to run every minute.

    Everything that is not a bug is a `stay` with its reason: a claim held, a login
    vibe does not know, a target that turned out dead. None of them is retried in the
    same tick, and none is an exit code.
    """
    settings = read_settings(root)
    if not settings.auto_switch:
        return _stay(pool.REASON_OFF)
    live = store.live_alias(root, port)
    if live is None:
        return _stay(pool.REASON_LIVE_UNKNOWN)
    active = store.read_active(root)
    if active != live:
        # Polling would treat `active` as the account in use and the live one as any other,
        # whose expired token it then refreshes behind Claude Code's back. `vibe switch
        # <live>` repairs the pointer without losing the live login.
        return _stay(
            f"Claude Code runs as {live} but vibe's active account is {active or 'unset'}"
            f" - run: vibe switch {live}"
        )

    last = read_last(root)
    fast: frozenset[str] = frozenset({live}) if _is_hot(root, live, now_s) else frozenset()
    views = envelope.collect(root, client=client, port=port, now_s=now_s, fast=fast)
    decision = pool.decide(_readings(views), settings=settings, live=live, now_s=now_s, last=last)
    if decision.action == "stay" or decision.target is None:
        return _stay(decision.reason)
    target = decision.target

    try:
        oauth.ensure_usable(root, target, client, now_ms=int(now_s * 1000))
        store.switch(root, target, port)
    except (UsageError, NeedsLoginError, NetworkError) as exc:
        return _stay(exc.message)

    rows = next((v.summary.rows for v in views if v.alias == live and v.summary is not None), ())
    write_last(
        root,
        LastSwitch(
            live,
            target,
            now_s,
            decision.reason,
            pool.limited_until(rows, settings.threshold, now_s),
        ),
    )
    line = f"switched {live} -> {target}: {decision.reason}"
    notify(line)
    return TickResult(line, switched=True)


def undo(root: Path, *, client: HttpClient, port: KeychainPort, now_s: float) -> LastSwitch:
    """Switch back to the account the last switch left, through the same guarded path."""
    last = read_last(root)
    if last is None:
        raise UsageError("there is no automatic switch to undo", "vibe auto status")
    oauth.ensure_usable(root, last.from_alias, client, now_ms=int(now_s * 1000))
    store.switch(root, last.from_alias, port)
    # Recorded as a switch of its own, so the cooldown starts again and the next tick
    # does not walk straight back out of the account the person just asked for.
    record = LastSwitch(last.to_alias, last.from_alias, now_s, "undo", None)
    write_last(root, record)
    return record


@dataclass(frozen=True)
class Status:
    settings: Settings
    live: str | None
    active: str | None
    last: LastSwitch | None
    next_alias: str | None


def status(root: Path, *, client: HttpClient, port: KeychainPort, now_s: float) -> Status:
    settings = read_settings(root)
    live = store.live_alias(root, port)
    active = store.read_active(root)
    last = read_last(root)
    next_alias = None
    # Not when the pointer disagrees: collecting would poll the live account as if it
    # were any other, which the tick refuses to do for the same reason.
    if live is not None and live == active:
        views = envelope.collect(root, client=client, port=port, now_s=now_s)
        next_alias = pool.next_candidate(
            _readings(views), settings=settings, live=live, now_s=now_s, last=last
        )
    return Status(settings, live, active, last, next_alias)


def notify_macos(message: str) -> None:
    """Tell the person a switch happened. Best effort: a failed one is not a failed switch."""
    platform: str = sys.platform
    if platform != "darwin":
        return
    # The message is an argument of the script, never part of its text: it names accounts
    # and a limit kind the server chose, and none of that belongs inside AppleScript source.
    argv = [OSASCRIPT_BIN]
    for line in (
        "on run argv",
        'display notification (item 1 of argv) with title "vibemaxxing"',
        "end run",
    ):
        argv += ["-e", line]
    argv.append(message)
    try:
        subprocess.run(argv, capture_output=True, timeout=OSASCRIPT_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def bootstrap_command(path: Path) -> str:
    return f"launchctl bootstrap gui/{os.getuid()} {path}"


def bootout_command() -> str:
    return f"launchctl bootout gui/{os.getuid()}/{LABEL}"


def _require_macos(verb: str) -> None:
    platform: str = sys.platform
    if platform != "darwin":
        raise UsageError(
            f"vibe auto {verb} manages a launchd job, which only macOS has",
            "vibe auto tick",
        )


def install_plist() -> Path:
    """Write the launchd job. Loading it is the operator's to do: this never runs launchctl."""
    _require_macos("install")
    path = plist_path()
    job = {
        "Label": LABEL,
        # The interpreter that is running this, by absolute path: launchd's PATH finds no `vibe`.
        "ProgramArguments": [sys.executable, "-m", "vibemaxxing", "auto", "tick"],
        "StartInterval": TICK_INTERVAL_S,
    }
    # Not private_dir: ~/Library/LaunchAgents is the user's, and its mode is not ours to narrow.
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private(path, plistlib.dumps(job).decode("utf-8"))
    return path


def uninstall_plist() -> bool:
    """Remove the launchd job's file. True if there was one."""
    _require_macos("uninstall")
    path = plist_path()
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed
