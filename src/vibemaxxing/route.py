"""The `routing` member of settings.json: the connectors each account has, and vibe-bridge's own.

`vibe run <account>` reads the connectors to add vibe-bridge to a Claude run on an account that
lacks Slack or Atlassian (launch._with_bridge), and to refuse a Cursor run that declares a
connector (--needs) its seat does not have. vibe never picks an account itself: the person names
it. The member keeps its old name so an existing settings.json still reads, and the members of
the removed `vibe run auto` are accepted and ignored.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from vibemaxxing.errors import UsageError

DEFAULT_CONNECTORS: Final[Mapping[str, tuple[str, ...]]] = {
    "main": ("slack", "atlassian", "m365"),
    "cursor": ("slack", "atlassian", "gcal"),
}
CONNECTORS: Final = ("slack", "atlassian", "m365", "gcal")
# Members an older settings.json may still carry (the routing of the removed `vibe run auto`,
# and settings removed before it). Accepted so the file keeps working; nothing reads them.
_IGNORED: Final = frozenset(
    {
        "default_needs",
        "no_degrade",
        "order",
        "roles",
        "spent_percent",
        "shared_login",
        "timezone",
        "weekday_budget",
        "cursor_margin_cents",
        "cursor_cap_cents",
    }
)


@dataclass(frozen=True)
class Routing:
    # account alias -> the connectors a session on it has of its own.
    connectors: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_CONNECTORS)
    )
    # The `bridge` member, validated by vibe-bridge (bridge.py) where it is used.
    bridge: object = None


# --- settings.json `routing` ---------------------------------------------------------


def _fail(path: Path, why: str) -> UsageError:
    return UsageError(f"{path}: routing {why}", f"edit {path} (or delete its routing member)")


def _strings(raw: object) -> tuple[str, ...] | None:
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        return None
    return tuple(str(item) for item in raw)


def parse_routing(raw: object, path: Path) -> Routing:
    """The `routing` member of settings.json; UsageError for one that is wrong anywhere.

    A wrong member is refused, not read as the defaults: a connector map nobody wrote would add
    or leave out vibe-bridge where the owner did not mean it.
    """
    if raw is None:
        return Routing()
    if not isinstance(raw, dict):
        raise _fail(path, "must be an object")
    unknown = sorted(set(raw) - {"connectors", "bridge"} - _IGNORED)
    if unknown:
        raise _fail(path, f"has unknown members: {', '.join(unknown)}")
    connectors: dict[str, tuple[str, ...]] = dict(DEFAULT_CONNECTORS)
    given = raw.get("connectors")
    if given is not None:
        if not isinstance(given, dict):
            raise _fail(path, "connectors must map an account to its connector names")
        for alias, names in given.items():
            parsed = _strings(names)
            if parsed is None or any(name not in CONNECTORS for name in parsed):
                raise _fail(path, f"connectors.{alias} must be a list of {', '.join(CONNECTORS)}")
            connectors[str(alias)] = parsed
    return Routing(connectors=connectors, bridge=raw.get("bridge"))


def read_routing(root: Path) -> Routing:
    """The routing in settings.json; the defaults when there is no file or no routing member."""
    path = root / "settings.json"
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Routing()
    except (OSError, ValueError):
        raise _fail(path, "cannot be read: the file is not valid JSON") from None
    member = parsed.get("routing") if isinstance(parsed, dict) else None
    return parse_routing(member, path)


def connectors_of(routing: Routing, alias: str) -> set[str]:
    """The connectors a session on ``alias`` has of its own; none for an account not listed."""
    return set(routing.connectors.get(alias, ()))


def check_needs(needs: Sequence[str]) -> tuple[str, ...]:
    """``needs`` without repeats; UsageError for a name that is not a connector vibe knows."""
    unknown = [name for name in needs if name not in CONNECTORS]
    if unknown:
        raise UsageError(
            f"unknown connector {', '.join(unknown)}; known: {', '.join(CONNECTORS)}",
            "vibe run cursor --needs slack,atlassian -- claude",
        )
    return tuple(dict.fromkeys(needs))
