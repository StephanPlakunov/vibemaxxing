"""`~/.vibemaxxing/status.json`, and the one line `vibe statusline` prints from it.

Claude Code runs a status line command on every redraw, so the line is made from this
one file and nothing else: no Keychain, no network, no refresh, and none of the
package's heavier modules. `auto.publish_status` writes the file after every command
that collects usage and every tick; this module only knows its path and its shape.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Final

from vibemaxxing.fsutil import private_dir, write_private

SCHEMA_VERSION: Final = 1
# A number this old may have moved: it is shown with a "?".
STALE_S: Final = 15 * 60
NO_STATUS: Final = "vibe: no status yet"
_LABELS: Final = {"session": "5h", "weekly_all": "wk"}
_SCOPED_KIND: Final = "weekly_scoped"
_SCOPED_PREFIX: Final = "Weekly · "  # usage.SCOPED_LABEL_PREFIX, not imported: see above
# English, whatever the locale: strftime("%a") follows LC_TIME.
_WEEKDAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


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


def _models(document: Mapping[str, object]) -> set[str]:
    policy = document.get("policy")
    models = policy.get("block_models") if isinstance(policy, dict) else None
    if not isinstance(models, list):
        return set()
    return {model.casefold() for model in models if isinstance(model, str)}


def _label(row: Mapping[str, object], models: set[str]) -> str | None:
    kind = row.get("kind")
    if isinstance(kind, str) and kind in _LABELS:
        return _LABELS[kind]
    label = _text(row.get("label"))
    if kind != _SCOPED_KIND or label is None or not label.startswith(_SCOPED_PREFIX):
        return None
    model = label.removeprefix(_SCOPED_PREFIX)
    # A model's own row shows only when the policy counts it, like the rows that move it.
    return model if model.casefold() in models else None


def _account(entry: Mapping[str, object], models: set[str], now_s: float) -> str | None:
    alias = _text(entry.get("alias"))
    if alias is None:
        return None
    if entry.get("state") == "needs_login":
        return f"{alias}: needs login"
    fetched = _number(entry.get("fetched_at"))
    rows = entry.get("rows")
    cells: list[str] = []
    for row in rows if isinstance(rows, list) and fetched is not None else []:
        if not isinstance(row, dict):
            continue
        label, percent = _label(row, models), _number(row.get("percent"))
        resets = _number(row.get("resets_at"))
        # A row whose window rolled over since it was read says nothing about now.
        if label is None or percent is None or (resets is not None and resets <= now_s):
            continue
        stale = fetched is not None and not 0 <= now_s - fetched <= STALE_S
        cell = f"{label} {round(percent)}%{'?' if stale else ''}"
        cells.append(cell if resets is None else f"{cell} (resets {_when(resets, now_s)})")
    return f"{alias}: {' '.join(cells) or '-'}"


def render(document: Mapping[str, object] | None, *, now_s: float) -> str:
    """One line: the live account, then each account's session and weekly rows."""
    if document is None:
        return NO_STATUS
    parts: list[str] = []
    live = _text(document.get("live")) or _text(document.get("active"))
    if live is not None:
        parts.append(f"▶ {live}")
    models = _models(document)
    accounts = document.get("accounts")
    for entry in accounts if isinstance(accounts, list) else []:
        if isinstance(entry, dict):
            text = _account(entry, models, now_s)
            if text is not None:
                parts.append(text)
    return " · ".join(parts) or NO_STATUS


def main() -> int:
    """The console script's fast path for `vibe statusline`. Never fails the redraw."""
    sys.stdout.write(render(read(default_root()), now_s=time.time()) + "\n")
    return 0
