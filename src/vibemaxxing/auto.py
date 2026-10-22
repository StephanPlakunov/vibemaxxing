"""Automatic account switching: the settings, one tick, undo, and the launchd job.

Sits above envelope, which it asks for the accounts' numbers, and below cli. The
decision itself is `pool.decide`, pure; everything here is the I/O around it.

A tick is the unit a scheduler calls. It never writes the Keychain except through
`store.switch`, which holds the refresh claims and refuses a login vibe does not
know or an account that is dead.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import plistlib
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from vibemaxxing import envelope, oauth, pool, statusline, store, usage
from vibemaxxing.envelope import AccountView
from vibemaxxing.errors import NeedsLoginError, NetworkError, NotFoundError, UsageError, VibeError
from vibemaxxing.fsutil import private_dir, write_private
from vibemaxxing.httpclient import HttpClient
from vibemaxxing.keychain import KeychainPort
from vibemaxxing.pool import (
    DEFAULT_COOLDOWN_S,
    DEFAULT_THRESHOLD,
    EARLIEST_RESET,
    MAX_THRESHOLD,
    MIN_THRESHOLD,
    LastSwitch,
    Policy,
    Reading,
    Settings,
    Step,
)

SCHEMA_VERSION: Final = 1
LABEL: Final = "de.intra-ai.vibemaxxing-auto"
TICK_INTERVAL_S: Final = 60
# The live account is polled at the 60 s floor, not the 3 minute cadence, once a trigger
# row is this full: finding out late that it is spent costs the work in flight.
FAST_POLL_PERCENT: Final = 90
# The same distance below each limit of an ordered policy: 76% for a step until 85%.
FAST_POLL_MARGIN: Final = DEFAULT_THRESHOLD - FAST_POLL_PERCENT
ORDERED: Final = "ordered"
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


def _time(value: object) -> float | None:
    """A timestamp every surface can format; None for one that no clock could have written."""
    seconds = _number(value)
    if seconds is None:
        return None
    try:
        datetime.fromtimestamp(seconds, UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return seconds


def _parse_policy(raw: object) -> Policy | None:
    """The `policy` member: absent is the default rule, and None means it is wrong anywhere."""
    if raw is None:
        return Policy()
    if not isinstance(raw, dict) or raw.get("mode") != ORDERED:
        return None
    steps, models = raw.get("steps"), raw.get("block_models", [])
    if raw.get("then", EARLIEST_RESET) != EARLIEST_RESET:
        return None
    if not isinstance(steps, list) or not steps:
        return None
    if not isinstance(models, list) or not all(isinstance(m, str) and m.strip() for m in models):
        return None
    parsed: list[Step] = []
    for step in steps:
        if not isinstance(step, dict):
            return None
        account, until = step.get("account"), _int(step.get("until"))
        if not isinstance(account, str) or not store.valid_alias(account):
            return None
        if until is None or not 1 <= until <= MAX_THRESHOLD:
            return None
        parsed.append(Step(account, until))
    return Policy(tuple(parsed), tuple(str(model).strip() for model in models))


def policy_document(policy: Policy) -> dict[str, object] | None:
    """The policy as settings.json spells it; None for the default rule, which has no member."""
    if not policy.steps:
        return None
    document: dict[str, object] = {
        "mode": ORDERED,
        "steps": [{"account": step.account, "until": step.until} for step in policy.steps],
        "then": EARLIEST_RESET,
    }
    if policy.block_models:
        document["block_models"] = list(policy.block_models)
    return document


def _parse_settings(root: Path) -> Settings | None:
    """The settings, or None when the file is missing or wrong anywhere."""
    try:
        parsed: object = json.loads(settings_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict) or parsed.get("schema") != SCHEMA_VERSION:
        return None
    enabled = parsed.get("auto_switch", False)
    threshold = _int(parsed.get("threshold", DEFAULT_THRESHOLD))
    cooldown = _int(parsed.get("cooldown_s", DEFAULT_COOLDOWN_S))
    excluded = parsed.get("excluded", [])
    policy = _parse_policy(parsed.get("policy"))
    if (
        not isinstance(enabled, bool)
        or threshold is None
        or not MIN_THRESHOLD <= threshold <= MAX_THRESHOLD
        or cooldown is None
        or cooldown < 0
        or not isinstance(excluded, list)
        or not all(isinstance(alias, str) and store.valid_alias(alias) for alias in excluded)
        or policy is None
    ):
        return None
    return Settings(
        enabled, threshold, cooldown, tuple(dict.fromkeys(str(a) for a in excluded)), policy
    )


def read_settings(root: Path) -> Settings:
    """The settings file, or the defaults for a file that is missing or wrong anywhere.

    All of it, not the valid half: a hand-edited file that is wrong in one place must
    leave auto-switch off rather than switch on a guess about what was meant. A bad
    file is never an error, so `vibe list` keeps working. This is the one place the
    file is validated.
    """
    settings = _parse_settings(root)
    return settings if settings is not None else Settings()


def settings_for_edit(root: Path) -> Settings:
    """The settings a command is about to change and write back.

    Reading a bad file as the defaults is right for `vibe list`; writing the defaults
    back over it is not, since that silently drops what someone wrote by hand. A file
    that exists and cannot be read is refused instead.
    """
    settings = _parse_settings(root)
    if settings is not None:
        return settings
    path = settings_path(root)
    if path.exists():
        raise UsageError(
            f"{path} is not valid settings (a wrong or missing member), and saving a change "
            "would overwrite it with the defaults. Fix it, or delete it to start over",
            f"rm {path}",
        )
    return Settings()


def write_settings(root: Path, settings: Settings) -> None:
    private_dir(root)
    document = {
        "schema": SCHEMA_VERSION,
        "auto_switch": settings.auto_switch,
        "threshold": settings.threshold,
        "cooldown_s": settings.cooldown_s,
        "excluded": list(settings.excluded),
    }
    policy = policy_document(settings.policy)
    if policy is not None:
        document["policy"] = policy
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
    # Both times are checked against what a clock can write: `vibe list`, the TUI and
    # the web page all format `at`, and one absurd value would stop every one of them.
    at = _time(parsed.get("at"))
    if not (isinstance(origin, str) and isinstance(target, str) and isinstance(reason, str)):
        return None
    if at is None:
        return None
    return LastSwitch(
        origin, target, at, reason, _time(parsed.get("until")), _int(parsed.get("limit"))
    )


def write_last(root: Path, last: LastSwitch) -> None:
    private_dir(root)
    document = {
        "schema": SCHEMA_VERSION,
        "from": last.from_alias,
        "to": last.to_alias,
        "at": last.at_s,
        "reason": last.reason,
        "until": last.until_s,
        "limit": last.limit,
    }
    write_private(last_path(root), json.dumps(document, indent=2) + "\n")


def rename(root: Path, old: str, new: str) -> None:
    """Follow `vibe alias`: exclusions, policy steps and the last switch name accounts by alias."""
    settings = _parse_settings(root)
    steps = settings.policy.steps if settings is not None else ()
    if settings is not None and (old in settings.excluded or any(s.account == old for s in steps)):
        excluded = tuple(new if alias == old else alias for alias in settings.excluded)
        moved = tuple(Step(new, s.until) if s.account == old else s for s in steps)
        policy = replace(settings.policy, steps=moved)
        write_settings(root, replace(settings, excluded=excluded, policy=policy))
    last = read_last(root)
    if last is not None and old in (last.from_alias, last.to_alias):
        write_last(
            root,
            replace(
                last,
                from_alias=new if last.from_alias == old else last.from_alias,
                to_alias=new if last.to_alias == old else last.to_alias,
            ),
        )


def _readings(views: Sequence[AccountView]) -> list[Reading]:
    return [
        Reading(
            view.alias,
            view.state,
            view.summary.rows if view.summary is not None else None,
            view.updated_at,
            identified=view.identity.account_uuid is not None,
        )
        for view in views
    ]


def _is_hot(root: Path, alias: str, now_s: float, settings: Settings) -> bool:
    """Whether the cached answer for ``alias`` is close enough to a limit to poll every minute.

    The default rule: a trigger row at FAST_POLL_PERCENT. An ordered policy: a row that
    counts within FAST_POLL_MARGIN below one of the account's own step limits, and not
    over it -- past 85% the next limit is 99%, and polling a spent account fast buys nothing.
    """
    cached = store.read_usage_cache(root, alias)
    if cached is None or cached.payload is None:
        return False
    rows = usage.summarize(cached.payload).rows
    policy = settings.policy
    if not policy.steps:
        return bool(pool.blocking(rows, FAST_POLL_PERCENT, now_s))
    models = policy.block_models
    return any(
        pool.blocking(rows, step.until - FAST_POLL_MARGIN, now_s, models=models)
        and not pool.blocking(rows, step.until, now_s, models=models)
        for step in policy.steps
        if step.account == alias
    )


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
    same tick, and none is an exit code. Every tick that returns leaves status.json
    behind, so the status line is never older than the scheduler's interval.
    """
    result, live = _tick(root, client=client, port=port, now_s=now_s, notify=notify)
    publish_status(root, live=live, now_s=now_s)
    return result


def _tick(
    root: Path,
    *,
    client: HttpClient,
    port: KeychainPort,
    now_s: float,
    notify: Callable[[str], None],
) -> tuple[TickResult, str | None]:
    """The tick, and the account Claude Code runs as after it when that is known."""
    settings = read_settings(root)
    if not settings.auto_switch:
        return _stay(pool.REASON_OFF), None
    live = store.live_alias(root, port)
    if live is None:
        return _stay(pool.REASON_LIVE_UNKNOWN), None
    active = store.read_active(root)
    if active != live:
        # Polling would treat `active` as the account in use and the live one as any other,
        # whose expired token it then refreshes behind Claude Code's back. `vibe switch
        # <live>` repairs the pointer without losing the live login.
        return _stay(
            f"Claude Code runs as {live} but vibe's active account is {active or 'unset'}"
            f" - run: vibe switch {live}"
        ), live

    last = read_last(root)
    hot = _is_hot(root, live, now_s, settings)
    fast: frozenset[str] = frozenset({live}) if hot else frozenset()
    views = envelope.collect(root, client=client, port=port, now_s=now_s, fast=fast)
    decision = pool.decide(_readings(views), settings=settings, live=live, now_s=now_s, last=last)
    if decision.action == "stay" or decision.target is None:
        return _stay(decision.reason), live
    target = decision.target

    try:
        oauth.ensure_usable(root, target, client, now_ms=int(now_s * 1000))
        # collect and the refresh can take seconds, and a person may have run `vibe
        # switch` in that time. The decision was about `live`; if that is no longer the
        # account in use, it is stale and the person's choice stands.
        if store.live_alias(root, port) != live or store.read_active(root) != live:
            stale = f"the live account changed while checking, so {target} was left alone"
            return _stay(stale), None
        store.switch(root, target, port, now_s=now_s)
    except (UsageError, NeedsLoginError, NetworkError, NotFoundError) as exc:
        return _stay(exc.message), live

    rows = next((v.summary.rows for v in views if v.alias == live and v.summary is not None), ())
    limit = decision.limit if decision.limit is not None else settings.threshold
    models = settings.policy.block_models
    line = f"switched {live} -> {target}: {decision.reason}"
    notice = f"Switched to {target}: {decision.reason}"
    try:
        write_last(
            root,
            LastSwitch(
                live,
                target,
                now_s,
                decision.reason,
                pool.limited_until(rows, limit, now_s, models=models),
                limit,
            ),
        )
    except OSError as exc:
        # The switch landed. Raising here would hide that and skip the notification; the
        # cost of the missing record is a cooldown and a memory of why `live` was left.
        unrecorded = f" (not recorded: {exc.__class__.__name__})"
        line, notice = line + unrecorded, notice + unrecorded
    notify(notice)
    return TickResult(line, switched=True), target


def undo(root: Path, *, client: HttpClient, port: KeychainPort, now_s: float) -> LastSwitch:
    """Switch back to the account the last switch left, through the same guarded path."""
    last = read_last(root)
    if last is None:
        raise UsageError("there is no automatic switch to undo", "vibe auto status")
    oauth.ensure_usable(root, last.from_alias, client, now_ms=int(now_s * 1000))
    store.switch(root, last.from_alias, port, now_s=now_s)
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
    # An ordered policy only: the step it is on now and the one a switch would go to,
    # as 0-based indexes into settings.policy.steps.
    step: int | None = None
    next_step: int | None = None


def status(root: Path, *, client: HttpClient, port: KeychainPort, now_s: float) -> Status:
    settings = read_settings(root)
    live = store.live_alias(root, port)
    active = store.read_active(root)
    last = read_last(root)
    # Not when the pointer disagrees: collecting would poll the live account as if it
    # were any other, which the tick refuses to do for the same reason.
    if live is None or live != active:
        return Status(settings, live, active, last, None)
    views = envelope.collect(root, client=client, port=port, now_s=now_s)
    publish_status(root, live=live, now_s=now_s)
    readings = _readings(views)
    next_alias = pool.next_candidate(readings, settings=settings, live=live, now_s=now_s, last=last)
    step, next_step = pool.ordered_steps(
        readings, settings=settings, live=live, now_s=now_s, last=last
    )
    return Status(settings, live, active, last, next_alias, step, next_step)


def _row_entry(row: usage.Row) -> dict[str, object]:
    return {
        "kind": row.kind,
        "label": row.label,
        "percent": row.percent,
        "resets_at": None if row.resets_at is None else pool.parse_stamp(row.resets_at),
    }


def _status_account(root: Path, alias: str) -> dict[str, object]:
    try:
        account = store.read_account(root, alias)
    except VibeError:
        return {"alias": alias, "state": "error", "fetched_at": None, "rows": []}
    cached = store.read_usage_cache(root, alias)
    # Another login under the same alias has another added_at; its numbers are not ours.
    if cached is None or cached.added_at != account.added_at or cached.payload is None:
        return {"alias": alias, "state": str(account.state), "fetched_at": None, "rows": []}
    return {
        "alias": alias,
        "state": str(account.state),
        "fetched_at": cached.fetched_at,
        "rows": [_row_entry(row) for row in usage.summarize(cached.payload).rows],
    }


def publish_status(root: Path, *, live: str | None, now_s: float) -> None:
    """Write status.json for `vibe statusline`: who is live, every account's rows, the policy.

    The rows come from the shared usage caches, not from one command's views: every
    surface leaves its answers there, so the file says the same whichever command wrote
    it last. ``live`` is None when the caller did not look at the Keychain. Best effort:
    a status that cannot be written costs the status line its freshness, not the command.
    """
    settings, last = read_settings(root), read_last(root)
    document: dict[str, object] = {
        "schema": statusline.SCHEMA_VERSION,
        "written_at": now_s,
        "live": live,
        "active": store.read_active(root),
        "accounts": [_status_account(root, alias) for alias in store.list_aliases(root)],
        "policy": policy_document(settings.policy),
        "last_switch": None
        if last is None
        else {"from": last.from_alias, "to": last.to_alias, "at": last.at_s, "reason": last.reason},
    }
    with contextlib.suppress(OSError):
        statusline.write(root, document)


def publish(root: Path, *, port: KeychainPort, now_s: float) -> None:
    """`publish_status` for a surface that has not asked who is live: it asks the port."""
    publish_status(root, live=store.live_alias(root, port), now_s=now_s)


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
