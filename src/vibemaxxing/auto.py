"""The tick a scheduler runs every minute, and the launchd job that runs it.

A tick renews the token broker's logins, fetches fresh numbers for every account, and writes
status.json for the status lines. It never switches the login Claude Code uses: vibe changes
accounts only when the person runs `vibe switch`, and starts a session only on the account the
person names (`vibe run <account>`).

Sits above envelope, which it asks for the accounts' numbers, and below cli.
"""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from vibemaxxing import (
    broker,
    codex,
    codex_usage,
    cursor,
    envelope,
    pool,
    statusline,
    store,
    usage,
)
from vibemaxxing.credentials import holds_refresh_token, parse_blob
from vibemaxxing.errors import UsageError, VibeError
from vibemaxxing.fsutil import FILE_MODE, write_private
from vibemaxxing.httpclient import HttpClient
from vibemaxxing.keychain import KeychainPort, config_dir_port

LABEL: Final = "de.intra-ai.vibemaxxing-auto"
TICK_INTERVAL_S: Final = 60
# ticks.jsonl gets a line every this many seconds.
LOG_EVERY_S: Final = 300
# `vibe broker on` does not take the live login over while its access token ends within this
# long. A running Claude Code session renews a token in the minutes before it ends, and a renewal
# in that window would spend the refresh token vibe just took.
RENEWAL_GUARD_S: Final = 10 * 60
# ...unless the token ended longer ago than this: a session using it would have renewed it,
# so none is, and the takeover may go ahead.
RENEWAL_IDLE_S: Final = 15 * 60


@dataclass(frozen=True)
class TickResult:
    line: str


def renewal_wait(port: KeychainPort, live: str, now_s: float) -> str | None:
    """Why taking ``live``'s login over (`vibe broker on`) must wait for its renewal; None if not.

    Read from the port, never from the account file: Claude Code renews the live login in the
    Keychain, and the file only catches up at the next switch. Nothing is refreshed here. A
    port that cannot be read now (it could a moment ago) is a wait: not knowing is no licence.
    """
    try:
        raw = port.read()
        if raw is not None and not holds_refresh_token(raw):
            # A token broker mirror: Claude Code holds no refresh token here and can renew
            # nothing, so there is nothing to race. (Parsed first: a blob with no
            # access token either is no mirror, and stays a wait below.)
            parse_blob(raw, mirror_ok=True)
            return None
        expires_s = parse_blob(raw).expires_at_ms / 1000 if raw is not None else None
    except VibeError:
        expires_s = None
    if expires_s is None:
        return f"waiting: could not read {live}'s login to see whether it is being renewed"
    left_s = expires_s - now_s
    if left_s > RENEWAL_GUARD_S or -left_s > RENEWAL_IDLE_S:
        return None
    if left_s >= 0:
        return f"waiting: {live}'s login renews within {RENEWAL_GUARD_S // 60} min"
    return (
        f"waiting: {live}'s login ended {int(-left_s) // 60} min ago and a session may be "
        "renewing it"
    )


def tick(
    root: Path,
    *,
    client: HttpClient,
    port: KeychainPort,
    now_s: float,
    port_for: Callable[[Path], KeychainPort] = config_dir_port,
    home: Path | None = None,
) -> TickResult:
    """One pass: the token broker, then fresh numbers, then status.json. Safe every minute.

    Nothing that is not a bug is an error: a broker problem is said in the line and recorded in
    broker.json, and a login vibe does not know only means no numbers this time. Every tick that
    returns leaves status.json behind, so the status line is never older than the interval.
    """
    report = broker.run(
        root, client=client, default_port=port, port_for=port_for, now_s=now_s, home=home
    )
    result, live = _numbers(root, client=client, port=port, now_s=now_s)
    said = [*report.lines, *(f"problem: {problem}" for problem in report.problems)]
    if said:
        result = replace(result, line=f"{result.line} | broker: {'; '.join(said)}")
    publish_status(root, live=live, now_s=now_s)
    if int(now_s) // LOG_EVERY_S != int(now_s - TICK_INTERVAL_S) // LOG_EVERY_S:
        log_tick(root, result, live=live, now_s=now_s)
    return result


def _numbers(
    root: Path, *, client: HttpClient, port: KeychainPort, now_s: float
) -> tuple[TickResult, str | None]:
    """Fetch every account's numbers, and say who Claude Code runs as.

    Skipped while vibe's pointer is off the live login: polling would treat the pointer's account
    as the one in use and the live one as any other, whose token it would then refresh behind
    Claude Code's back. `vibe switch` to the live account repairs the pointer.
    """
    live = store.live_alias(root, port)
    if live is None:
        return TickResult("live account unknown: no numbers fetched"), None
    active = store.read_active(root)
    if active != live:
        return TickResult(
            f"Claude Code runs as {live} but vibe's active account is {active or 'unset'}: "
            f"no numbers fetched - run: vibe switch {live}"
        ), live
    envelope.collect(root, client=client, port=port, now_s=now_s)
    return TickResult(f"live {live}"), live


@dataclass(frozen=True)
class Status:
    live: str | None
    active: str | None


def status(root: Path, *, port: KeychainPort) -> Status:
    """Who Claude Code runs as and vibe's pointer. Reads the Keychain; fetches nothing."""
    return Status(store.live_alias(root, port), store.read_active(root))


def _row_entry(row: usage.Row) -> dict[str, object]:
    entry: dict[str, object] = {
        "kind": row.kind,
        "label": row.label,
        "percent": row.percent,
        "resets_at": None if row.resets_at is None else pool.parse_stamp(row.resets_at),
    }
    if row.used is not None and row.limit is not None:
        entry.update(spend_used=row.used, spend_limit=row.limit, currency=row.currency)
    return entry


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


def _status_cursor(root: Path) -> list[dict[str, object]]:
    """Each Cursor seat's last numbers, from the usage cache (never a request)."""
    aliases = cursor.list_cursor_aliases(root)
    if not aliases:
        return []
    return [cursor.status_entry(root, alias) for alias in aliases]


def _status_codex(root: Path) -> list[dict[str, object]]:
    """Each Codex account's last numbers, from the usage cache (never a request)."""
    entries: list[dict[str, object]] = []
    for alias in codex.list_codex_aliases(root):
        entry: dict[str, object] = {
            "alias": alias,
            "kind": codex.KIND_CODEX,
            "fetched_at": None,
            "windows": [],
            "spend": None,
        }
        entries.append(entry)
        try:
            added_at = codex.read_added_at(root, alias)
        except VibeError:
            continue
        cached = store.read_usage_cache(root, alias)
        if cached is None or cached.added_at != added_at or cached.payload is None:
            continue
        held = codex_usage.usage_from_payload(cached.payload)
        entry["fetched_at"] = cached.fetched_at
        entry["windows"] = [
            {"label": w.label, "percent": round(w.used_percent)} for w in held.windows
        ]
        if held.spend is not None:
            entry["spend"] = {
                "unit": held.spend.unit,
                "used": held.spend.used,
                "limit": held.spend.limit,
                "percent": codex_usage.spend_percent(held.spend),
                "resets_at_s": held.spend.resets_at_s,
            }
    return entries


def publish_status(root: Path, *, live: str | None, now_s: float) -> None:
    """Write status.json for `vibe statusline`: who is live and every account's rows.

    The rows come from the shared usage caches, not from one command's views: every
    surface leaves its answers there, so the file says the same whichever command wrote
    it last. ``live`` is None when the caller did not look at the Keychain. Best effort:
    a status that cannot be written costs the status line its freshness, not the command.
    """
    document: dict[str, object] = {
        "schema": statusline.SCHEMA_VERSION,
        "written_at": now_s,
        "live": live,
        "active": store.read_active(root),
        "accounts": [_status_account(root, alias) for alias in store.list_aliases(root)],
        "cursor": _status_cursor(root),
        "codex": _status_codex(root),
    }
    with contextlib.suppress(OSError):
        statusline.write(root, document)


def log_tick(root: Path, result: TickResult, *, live: str | None, now_s: float) -> None:
    """Append one line to ticks.jsonl: who is live, each account's [5h, weekly] %, the outcome.

    The only record of what the ticks did; status.json keeps just the latest. Best effort, like
    publish_status.
    """
    percents: dict[str, list[object]] = {}
    for alias in store.list_aliases(root):
        rows = {row["kind"]: row["percent"] for row in _status_account(root, alias)["rows"]}  # type: ignore[attr-defined]
        percents[alias] = [rows.get("session"), rows.get("weekly_all")]
    entry = {
        "at": int(now_s),
        "live": live,
        "pct": percents,
        "line": result.line,
    }
    with contextlib.suppress(OSError):
        fd = os.open(root / "ticks.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")


def publish(root: Path, *, port: KeychainPort, now_s: float) -> None:
    """`publish_status` for a surface that has not asked who is live: it asks the port."""
    publish_status(root, live=store.live_alias(root, port), now_s=now_s)


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
