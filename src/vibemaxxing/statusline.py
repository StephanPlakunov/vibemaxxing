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
_LABELS: Final = {"session": "5h", "weekly_all": "wk"}
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


def _label(row: Mapping[str, object]) -> str | None:
    kind = row.get("kind")
    return _LABELS[kind] if isinstance(kind, str) and kind in _LABELS else None


def _spend(rows: object, fetched: float | None, now_s: float) -> str | None:
    """An enterprise seat's monthly spend, ``$0.15/$125 (0%)``: its only row, from the payload.

    The share of the limit is in brackets, so it reads like the percent of the other accounts:
    the percent the provider reports (the one `vibe list` shows), else used / limit.
    """
    if not isinstance(rows, list) or fetched is None:
        return None
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") != "monthly_spend":
            continue
        used, limit = _number(row.get("spend_used")), _number(row.get("spend_limit"))
        if used is None or limit is None or limit <= 0:
            return None
        currency = _text(row.get("currency"))
        sign = "$" if currency in (None, "USD") else f"{currency} "
        stale = "" if 0 <= now_s - fetched <= STALE_S else "?"
        reported = _number(row.get("percent"))
        share = f"{round(reported)}%" if reported is not None else _share(used, limit)
        return f"{sign}{used:,.2f}/{sign}{limit:,.0f} ({share}){stale}"
    return None


def _share(used: float, limit: float) -> str:
    """How much of ``limit`` is used, as a whole percent (``95%``); ``limit`` is above 0."""
    return f"{round(used / limit * 100)}%"


def _account(entry: Mapping[str, object], now_s: float) -> str | None:
    alias = _text(entry.get("alias"))
    if alias is None:
        return None
    if entry.get("state") == "needs_login":
        return f"{alias}: needs login"
    fetched = _number(entry.get("fetched_at"))
    rows = entry.get("rows")
    spend = _spend(rows, fetched, now_s)
    if spend is not None:
        return f"{alias}: {spend}"
    cells: list[tuple[str, str, float | None]] = []
    for row in rows if isinstance(rows, list) and fetched is not None else []:
        if not isinstance(row, dict):
            continue
        label, percent = _label(row), _number(row.get("percent"))
        resets = _number(row.get("resets_at"))
        # A row whose window rolled over since it was read says nothing about now.
        if label is None or percent is None or (resets is not None and resets <= now_s):
            continue
        stale = fetched is not None and not 0 <= now_s - fetched <= STALE_S
        number = f"{round(percent)}%{'?' if stale else ''}"
        cells.append((label, number, resets))
    # An account with only its weekly window shows the bare number: nothing to tell it from.
    if len(cells) == 1 and cells[0][0] == _LABELS["weekly_all"]:
        return f"{alias}: {cells[0][1]}"
    shown = [
        f"{label} {number}" + ("" if resets is None else f" (resets {_when(resets, now_s)})")
        for label, number, resets in cells
    ]
    return f"{alias}: {' '.join(shown) or '-'}"


def _dollars(cents: float) -> str:
    return f"${cents / 100:g}"


def _cursor(entry: Mapping[str, object], now_s: float) -> str | None:
    """One Cursor seat: ``cursor: 0.1% $0/$250``, from the cached numbers only.

    ``?`` when there are none, when they are older than STALE_S, or when the cycle they describe
    has ended; ``spent`` (with when it resets) once routing counts the seat as used up.
    """
    alias = _text(entry.get("alias"))
    if alias is None:
        return None
    fetched = _number(entry.get("fetched_at"))
    cycle_end = _number(entry.get("cycle_end_s"))
    percent = _number(entry.get("included_percent"))
    used, cap = (
        _number(entry.get("on_demand_used_cents")),
        _number(entry.get("on_demand_cap_cents")),
    )
    if fetched is None or (cycle_end is not None and cycle_end <= now_s):
        return f"{alias}: ?"
    stale = "?" if not 0 <= now_s - fetched <= STALE_S else ""
    if entry.get("available") is False:
        when = "" if cycle_end is None else f" (resets {_when(cycle_end, now_s)})"
        return f"{alias}: spent{when}{stale}"
    cells: list[str] = []
    if percent is not None:
        cells.append(
            f"{percent:.1f}%".replace(".0%", "%") if percent < 10 else f"{round(percent)}%"
        )
    if used is not None:
        cells.append(_dollars(used) + (f"/{_dollars(cap)}" if cap else ""))
    if not cells or entry.get("available") is None:
        return f"{alias}: {' '.join(cells) or '-'}{stale or '?'}"
    return f"{alias}: {' '.join(cells)}{stale}"


def _plain(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.2f}".rstrip("0").rstrip(".")


def _codex(entry: Mapping[str, object], now_s: float) -> str | None:
    """One Codex account from the cached numbers: ``zvoove-codex: 0/5100 cr (0%)`` (the limit the
    plan reports, in the unit it names, and its share in brackets), or its windows
    (``5h 12% weekly 34%``). ``?`` when old."""
    alias = _text(entry.get("alias"))
    if alias is None:
        return None
    fetched = _number(entry.get("fetched_at"))
    if fetched is None:
        return f"{alias}: ?"
    stale = "" if 0 <= now_s - fetched <= STALE_S else "?"
    cells: list[str] = []
    spend = entry.get("spend")
    if isinstance(spend, dict):
        used, limit, unit = (
            _number(spend.get("used")),
            _number(spend.get("limit")),
            _text(spend.get("unit")),
        )
        if used is not None and limit is not None and unit is not None:
            short = "cr" if unit.casefold() == "credit" else unit
            share = f" ({_share(used, limit)})" if limit > 0 else ""
            cells.append(f"{_plain(used)}/{_plain(limit)} {short}{share}")
    windows = entry.get("windows")
    for window in windows if isinstance(windows, list) else []:
        if isinstance(window, dict):
            label, percent = _text(window.get("label")), _number(window.get("percent"))
            if label is not None and percent is not None:
                cells.append(f"{label} {round(percent)}%")
    if not cells:
        return f"{alias}: -{stale or '?'}"
    return f"{alias}: {' '.join(cells)}{stale}"


def _antigravity(entry: Mapping[str, object], now_s: float) -> str | None:
    alias = _text(entry.get("alias"))
    if alias is None:
        return None
    fetched = _number(entry.get("fetched_at"))
    stale = fetched is None or not 0 <= now_s - fetched <= STALE_S
    windows = entry.get("windows")
    groups: dict[str, list[str]] = {}
    for window in windows if isinstance(windows, list) else []:
        if isinstance(window, dict):
            group = _text(window.get("group"))
            period = _text(window.get("period"))
            percent = _number(window.get("remaining_percent"))
            if (
                group is not None
                and period is not None
                and percent is not None
                and 0 <= percent <= 100
            ):
                groups.setdefault(group, []).append(f"{period} {_plain(percent)}%")
    if groups:
        cells = " | ".join(f"{group} {' '.join(limits)}" for group, limits in groups.items())
        return f"{alias}: {cells} left" + ("?" if stale else "")
    message = _text(entry.get("message"))
    return f"{alias}: {message if message and not stale else '?'}"


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
    accounts = document.get("accounts")
    for entry in accounts if isinstance(accounts, list) else []:
        if isinstance(entry, dict):
            text = _account(entry, now_s)
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
    agy = document.get("antigravity")
    for account in agy if isinstance(agy, list) else []:
        if isinstance(account, dict):
            text = _antigravity(account, now_s)
            if text is not None:
                parts.append(text)
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
