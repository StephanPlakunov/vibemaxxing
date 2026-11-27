"""`~/.vibemaxxing/status.json`, and the one line `vibe statusline` prints from it.

Claude Code runs a status line command on every redraw, so the line is made from this
one file and nothing else: no Keychain, no network, no refresh, and none of the
package's heavier modules. `auto.publish_status` writes the file after every command
that collects usage and every tick; this module only knows its path and its shape.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from vibemaxxing.fsutil import private_dir, write_private

SCHEMA_VERSION: Final = 1
# A number this old may have moved: it is shown with a "?".
STALE_S: Final = 15 * 60
NO_STATUS: Final = "vibe: no status yet"
# The Claude windows that decide whether work can start: the 5-hour one and the weekly one.
_WINDOW_KINDS: Final = ("session", "weekly_all")
# At or above this share the account's line also says when its limit resets.
NEAR_PERCENT: Final = 90.0
# English, whatever the locale: strftime("%a") follows LC_TIME.
_WEEKDAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# childenv.SESSION_VAR, and what pins an account: not imported, see above.
SESSION_VAR: Final = "VIBE_SESSION"
# The mark sessions on Claude Code's own login carried while vibe could start them that way.
SESSION_LIVE: Final = "live"
_TOKEN_VAR: Final = "CLAUDE_CODE_OAUTH_TOKEN"
_CONFIG_VAR: Final = "CLAUDE_CONFIG_DIR"
UNKNOWN_SESSION: Final = "?"


def default_root() -> Path:
    # store.store_root(), which this module does not import: store pulls in the Keychain
    # and subprocess, and the status line runs on every redraw.
    return Path.home() / ".vibemaxxing"


def status_path(root: Path) -> Path:
    return root / "status.json"


def write(root: Path, document: Mapping[str, object]) -> None:
    private_dir(root)
    write_private(status_path(root), json.dumps(document, indent=2) + "\n")


def read(root: Path) -> dict[str, object] | None:
    """The status, or None for one that is missing, unreadable or of another schema."""
    try:
        parsed: object = json.loads(status_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict) or parsed.get("schema") != SCHEMA_VERSION:
        return None
    return {str(key): value for key, value in parsed.items()}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _when(at_s: float, now_s: float) -> str:
    """Local time: "19:30" today, "Mon 00:00" any other day."""
    try:
        at, now = datetime.fromtimestamp(at_s), datetime.fromtimestamp(now_s)
    except (ValueError, OverflowError, OSError):
        return "?"
    clock = at.strftime("%H:%M")
    return clock if at.date() == now.date() else f"{_WEEKDAYS[at.weekday()]} {clock}"


def _cell(
    alias: str,
    percent: float,
    *,
    resets_s: float | None,
    stale: bool,
    now_s: float,
    notes: Sequence[str] = (),
) -> str:
    """The one shape every account has: ``alias: 97%``, then in brackets what else matters.

    The percent is the share USED of the account's limit that decides whether work can start
    on it. Its reset is added once it is at or above NEAR_PERCENT, and ``?`` follows a number
    older than STALE_S.
    """
    shown = list(notes)
    if percent >= NEAR_PERCENT and resets_s is not None and resets_s > now_s:
        shown.append(f"resets {_when(resets_s, now_s)}")
    text = f"{alias}: {_plain(round(percent))}%{'?' if stale else ''}"
    return f"{text} ({', '.join(shown)})" if shown else text


def _stale(fetched: float | None, now_s: float) -> bool:
    return fetched is None or not 0 <= now_s - fetched <= STALE_S


def _binding(limits: Sequence[tuple[float, float | None]]) -> tuple[float, float | None] | None:
    """Of several limits that all apply, the one most used: it is the one that stops work."""
    return max(limits, key=lambda limit: limit[0]) if limits else None


def _spend(rows: object) -> tuple[float, float | None] | None:
    """An enterprise seat's monthly spend: its only row. The percent the provider reports (the
    one `vibe usage` shows), else used / limit; no reset is reported."""
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") != "monthly_spend":
            continue
        used, limit = _number(row.get("spend_used")), _number(row.get("spend_limit"))
        if used is None or limit is None or limit <= 0:
            return None
        reported = _number(row.get("percent"))
        return (reported if reported is not None else used / limit * 100), None
    return None


def _account(entry: Mapping[str, object], now_s: float) -> str | None:
    """A Claude account: its monthly spend, or the most used of its 5-hour and weekly windows."""
    alias = _text(entry.get("alias"))
    if alias is None:
        return None
    if entry.get("state") == "needs_login":
        return f"{alias}: needs login"
    fetched = _number(entry.get("fetched_at"))
    if fetched is None:
        return f"{alias}: ?"
    rows = entry.get("rows")
    chosen = _spend(rows)
    if chosen is None:
        limits: list[tuple[float, float | None]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("kind") not in _WINDOW_KINDS:
                continue
            percent, resets = _number(row.get("percent")), _number(row.get("resets_at"))
            # A window that rolled over since it was read says nothing about now.
            if percent is not None and (resets is None or resets > now_s):
                limits.append((percent, resets))
        chosen = _binding(limits)
    if chosen is None:
        return f"{alias}: -"
    percent, resets = chosen
    return _cell(alias, percent, resets_s=resets, stale=_stale(fetched, now_s), now_s=now_s)


def _cursor(entry: Mapping[str, object], now_s: float) -> str | None:
    """One Cursor seat: its INCLUDED usage, ``cursor: 33%``; on-demand only as a note once it
    is used up (``cursor: 33% (on-demand spent)``), since included usage stays usable then."""
    alias = _text(entry.get("alias"))
    if alias is None:
        return None
    fetched = _number(entry.get("fetched_at"))
    cycle_end = _number(entry.get("cycle_end_s"))
    percent = _number(entry.get("included_percent"))
    if fetched is None or percent is None or (cycle_end is not None and cycle_end <= now_s):
        return f"{alias}: ?"
    used, cap = (
        _number(entry.get("on_demand_used_cents")),
        _number(entry.get("on_demand_cap_cents")),
    )
    if cap:
        spent = used is not None and used >= cap
    else:
        spent = entry.get("available") is False and percent < NEAR_PERCENT
    return _cell(
        alias,
        percent,
        resets_s=cycle_end,
        stale=_stale(fetched, now_s),
        now_s=now_s,
        notes=("on-demand spent",) if spent else (),
    )


def _plain(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.2f}".rstrip("0").rstrip(".")


def _codex(entry: Mapping[str, object], now_s: float) -> str | None:
    """One Codex account: the most used of its windows and its credit pool."""
    alias = _text(entry.get("alias"))
    if alias is None:
        return None
    fetched = _number(entry.get("fetched_at"))
    if fetched is None:
        return f"{alias}: ?"
    limits: list[tuple[float, float | None]] = []
    spend = entry.get("spend")
    if isinstance(spend, dict):
        used, limit = _number(spend.get("used")), _number(spend.get("limit"))
        reported = _number(spend.get("percent"))
        if reported is None and used is not None and limit is not None and limit > 0:
            reported = used / limit * 100
        if reported is not None:
            limits.append((reported, _number(spend.get("resets_at_s"))))
    windows = entry.get("windows")
    for window in windows if isinstance(windows, list) else []:
        if isinstance(window, dict):
            percent, resets = _number(window.get("percent")), _number(window.get("resets_at_s"))
            if percent is not None and (resets is None or resets > now_s):
                limits.append((percent, resets))
    chosen = _binding(limits)
    if chosen is None:
        return f"{alias}: -{'?' if _stale(fetched, now_s) else ''}"
    percent, resets = chosen
    return _cell(alias, percent, resets_s=resets, stale=_stale(fetched, now_s), now_s=now_s)


def _iso_s(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _antigravity(entry: Mapping[str, object], now_s: float) -> str | None:
    """One Antigravity account. Each model group (Gemini, Claude/GPT) has its own 5-hour and
    weekly windows, and work can start on whichever group has room: the group shown is the
    least used one, at the most used of its windows."""
    alias = _text(entry.get("alias"))
    if alias is None:
        return None
    fetched = _number(entry.get("fetched_at"))
    windows = entry.get("windows")
    groups: dict[str, list[tuple[float, float | None]]] = {}
    for window in windows if isinstance(windows, list) else []:
        if isinstance(window, dict):
            group = _text(window.get("group"))
            left = _number(window.get("remaining_percent"))
            resets = _iso_s(_text(window.get("resets_at")))
            if group is None or left is None or not 0 <= left <= 100:
                continue
            if resets is None or resets > now_s:
                groups.setdefault(group, []).append((100 - left, resets))
    chosen = [_binding(limits) for limits in groups.values()]
    usable = [limit for limit in chosen if limit is not None]
    if usable:
        percent, resets = min(usable, key=lambda limit: limit[0])
        return _cell(alias, percent, resets_s=resets, stale=_stale(fetched, now_s), now_s=now_s)
    message = _text(entry.get("message"))
    return f"{alias}: {message if message and not _stale(fetched, now_s) else '?'}"


def _broker_mirrors(root: Path) -> dict[str, str] | None:
    """The token broker's mirror dirs named in settings.json; None while the broker is off.

    broker.read_settings, not imported (see above); an alias not named mirrors into
    ~/.claude-<alias>.
    """
    try:
        parsed = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = parsed.get("broker") if isinstance(parsed, dict) else None
    if not isinstance(raw, dict) or raw.get("on") is not True:
        return None
    mirrors = raw.get("mirrors")
    if not isinstance(mirrors, dict):
        return {}
    return {str(k): v for k, v in mirrors.items() if isinstance(v, str) and v}


def _config_dir_alias(root: Path, directory: str, home: Path | None = None) -> str | None:
    """The account whose login lives in ``directory`` (its account file's config dir, or its
    token broker mirror); None if none."""
    try:
        wanted = Path(directory).expanduser().resolve()
        files = sorted((root / "accounts").glob("*.json"))
    except (OSError, RuntimeError):
        return None
    mirrors = _broker_mirrors(root)
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            named = raw.get("alias")
            alias = named if isinstance(named, str) else path.stem
            given = raw.get("config_dir")
            if isinstance(given, str) and Path(given).expanduser().resolve() == wanted:
                return alias
            if mirrors is not None and given is None and home is not None:
                mirror = mirrors.get(alias) or str(home / f".claude-{alias}")
                if Path(mirror).expanduser().resolve() == wanted:
                    return alias
        except (OSError, ValueError, RuntimeError):
            continue
    return None


def session_account(
    environ: Mapping[str, str], *, root: Path, home: Path
) -> tuple[str | None, bool]:
    """The account of the session this status line belongs to, and whether it is fixed.

    ``(None, False)`` is a session on Claude Code's own login: it is whatever account that
    login is now (`vibe switch` changes it). Claude Code and Cursor run the status
    line command with their own environment, which `vibe run` marked (``VIBE_SESSION``); a
    session vibe did not start is told apart by the variables that pin an account.
    """
    marked = environ.get(SESSION_VAR)
    if marked:
        return (None, False) if marked == SESSION_LIVE else (marked, True)
    directory = environ.get(_CONFIG_VAR)
    if directory:
        try:
            default = Path(directory).expanduser().resolve() == (home / ".claude").resolve()
        except (OSError, RuntimeError):
            default = False
        if not default:
            return _config_dir_alias(root, directory, home) or UNKNOWN_SESSION, True
    if environ.get(_TOKEN_VAR):
        return UNKNOWN_SESSION, True
    return None, False


def render(
    document: Mapping[str, object] | None,
    *,
    now_s: float,
    session: tuple[str | None, bool] = (None, False),
) -> str:
    """One line: this session's account, then each account's session and weekly rows.

    ``session`` comes from `session_account`: a session on Claude Code's own login shows the
    account that login is now; a fixed one shows its own account and says so.
    """
    if document is None:
        return NO_STATUS
    parts: list[str] = []
    alias, fixed = session
    seats = document.get("cursor")
    if alias is None:
        live = _text(document.get("live")) or _text(document.get("active"))
        if live is not None:
            parts.append(f"▶ {live}")
    else:
        # Every session vibe pins says so, a Cursor seat's too: `vibe run <account>` means fixed.
        parts.append(f"▶ {alias}" + (" (fixed)" if fixed else ""))
    # Order (Stephan, 26 Nov): plan accounts, Antigravity, Cursor, Codex, then spend-only seats.
    accounts = document.get("accounts")
    plans: list[str] = []
    spends: list[str] = []
    for entry in accounts if isinstance(accounts, list) else []:
        if isinstance(entry, dict):
            text = _account(entry, now_s)
            if text is not None:
                is_spend = _spend(entry.get("rows")) is not None
                (spends if is_spend else plans).append(text)
    parts.extend(plans)
    agy = document.get("antigravity")
    for account in agy if isinstance(agy, list) else []:
        if isinstance(account, dict):
            text = _antigravity(account, now_s)
            if text is not None:
                parts.append(text)
    for seat in seats if isinstance(seats, list) else []:
        if isinstance(seat, dict):
            text = _cursor(seat, now_s)
            if text is not None:
                parts.append(text)
    codex = document.get("codex")
    for account in codex if isinstance(codex, list) else []:
        if isinstance(account, dict):
            text = _codex(account, now_s)
            if text is not None:
                parts.append(text)
    parts.extend(spends)
    return " · ".join(parts) or NO_STATUS


def parse_session_flag(args: Sequence[str]) -> str | None:
    """``--session NAME`` (or ``--session=NAME``), the account a caller names for its session."""
    for index, arg in enumerate(args):
        if arg == "--session" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--session="):
            return arg.split("=", 1)[1]
    return None


def main(args: Sequence[str] = ()) -> int:
    """The console script's fast path for `vibe statusline`. Never fails the redraw."""
    root = default_root()
    named = parse_session_flag(args)
    session = (named, True) if named else session_account(os.environ, root=root, home=Path.home())
    sys.stdout.write(render(read(root), now_s=time.time(), session=session) + "\n")
    return 0
