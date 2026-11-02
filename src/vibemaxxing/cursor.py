"""The Cursor account kind: the stored marker, `cursor-agent` as a port, and the model list.

A Cursor account has no credential vibe handles. `cursor-agent login` keeps it, in the
place `cursor-agent` chose, and vibe only asks `cursor-agent status` whether it is logged
in. What vibe stores is a marker, ``~/.vibemaxxing/cursor/<alias>.json``, so `vibe list`
and `vibe run` know the alias is a Cursor one. Claude accounts keep living in
``accounts/``: every function that walks that directory expects a credential in each file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path
from typing import Final, Protocol

from vibemaxxing import store
from vibemaxxing.errors import NotFoundError, StoreError, UsageError, VibeError
from vibemaxxing.fsutil import private_dir, write_private

KIND_CLAUDE: Final = "claude"
KIND_CURSOR: Final = "cursor"
KINDS: Final = (KIND_CLAUDE, KIND_CURSOR)
AGENT_BIN: Final = "cursor-agent"
AGENT_TIMEOUT_S: Final = 30.0
# Words with a meaning of their own on the command line; never an alias.
RESERVED_ALIASES: Final = frozenset({"auto"})
EFFORTS: Final = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT: Final = "medium"
NO_ZDR: Final = "(NO ZDR)"

_ANSI: Final = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_MODEL_LINE: Final = re.compile(r"^(\S+) - (.*)$")
# claude-<family>-<version>[-thinking]-<effort>[-fast]; the version is "5" or "5-5".
_CURSOR_MODEL: Final = re.compile(
    r"^claude-(?P<family>[a-z]+)-(?P<version>\d+(?:-\d+)?)"
    r"(?P<thinking>-thinking)?-(?P<effort>low|medium|high|xhigh|max)(?P<fast>-fast)?$"
)
# What Claude Code is told with --model: an alias, or a full id with an optional date.
_CLAUDE_MODEL: Final = re.compile(
    r"^claude-(?P<family>[a-z]+)-(?P<version>\d+(?:-\d+)?)(?:-\d{8})?(?:\[[0-9a-z]+\])?$"
)
FAMILIES: Final = ("opus", "sonnet", "haiku", "fable")


# --- the stored marker ---------------------------------------------------------------


def cursor_dir(root: Path) -> Path:
    return root / "cursor"


def cursor_path(root: Path, alias: str) -> Path:
    if not store.valid_alias(alias):
        raise UsageError(f"{alias!r} is not a usable alias", "vibe list")
    return cursor_dir(root) / f"{alias}.json"


def list_cursor_aliases(root: Path) -> list[str]:
    try:
        names = [path.stem for path in cursor_dir(root).glob("*.json")]
    except OSError:
        return []
    return sorted(name for name in names if store.valid_alias(name))


def is_cursor(root: Path, alias: str) -> bool:
    return store.valid_alias(alias) and cursor_path(root, alias).is_file()


def read_added_at(root: Path, alias: str) -> float:
    """When the marker was written; the cache key that ties usage numbers to this account."""
    path = cursor_path(root, alias)
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise NotFoundError(f'there is no account named "{alias}"', "vibe list") from None
    except (OSError, ValueError):
        raise StoreError(f"{path} is unreadable", f"vibe add {alias} --kind cursor") from None
    if not isinstance(parsed, dict) or parsed.get("kind") != KIND_CURSOR:
        raise StoreError(f"{path} is not a Cursor account", f"vibe add {alias} --kind cursor")
    added = parsed.get("added_at")
    return float(added) if isinstance(added, (int, float)) and not isinstance(added, bool) else 0.0


def write_marker(root: Path, alias: str, *, added_at: float) -> None:
    private_dir(root)
    private_dir(cursor_dir(root))
    document = {"schema": store.SCHEMA_VERSION, "alias": alias, "kind": KIND_CURSOR}
    write_private(
        cursor_path(root, alias), json.dumps({**document, "added_at": added_at}, indent=2) + "\n"
    )


def delete_marker(root: Path, alias: str) -> None:
    path = cursor_path(root, alias)
    if not path.exists():
        raise NotFoundError(f'there is no account named "{alias}"', "vibe list")
    path.unlink(missing_ok=True)
    store.usage_path(root, alias).unlink(missing_ok=True)


def rename_marker(root: Path, old: str, new: str) -> None:
    added_at = read_added_at(root, old)
    write_marker(root, new, added_at=added_at)
    delete_marker(root, old)


# --- cursor-agent as a port ----------------------------------------------------------


@dataclass(frozen=True)
class AgentStatus:
    logged_in: bool
    email: str | None
    team: str | None


@dataclass(frozen=True)
class ModelEntry:
    id: str
    label: str

    @property
    def no_zdr(self) -> bool:
        return NO_ZDR in self.label

    @property
    def fast(self) -> bool:
        return self.id.endswith("-fast")


class AgentPort(Protocol):
    def binary(self) -> str:
        """The command to exec, resolved now. Raises VibeError when it is not installed."""
        ...

    def status(self) -> AgentStatus: ...

    def models(self) -> tuple[ModelEntry, ...]: ...

    def chat_exists(self, chat_id: str) -> bool: ...


def _not_installed() -> VibeError:
    return UsageError(
        "cursor-agent is not installed (or not on PATH)",
        "curl https://cursor.com/install -fsS | bash",
    )


def parse_models(text: str) -> tuple[ModelEntry, ...]:
    entries: list[ModelEntry] = []
    for line in _ANSI.sub("", text).splitlines():
        found = _MODEL_LINE.match(line.strip())
        if found is not None:
            # Display names carry zero-width spaces after the word "Fast".
            label = found.group(2).replace("​", "").strip()
            entries.append(ModelEntry(found.group(1), label))
    return tuple(entries)


def parse_status(text: str) -> AgentStatus:
    """`cursor-agent status --format json`; anything that is not that reads as logged out."""
    try:
        parsed: object = json.loads(text)
    except ValueError:
        return AgentStatus(False, None, None)
    if not isinstance(parsed, dict) or parsed.get("isAuthenticated") is not True:
        return AgentStatus(False, None, None)
    info = parsed.get("userInfo")
    email = info.get("email") if isinstance(info, dict) else None
    team = info.get("teamName") if isinstance(info, dict) else None
    return AgentStatus(
        True,
        email if isinstance(email, str) else None,
        team if isinstance(team, str) else None,
    )


class CliAgent:
    """The real `cursor-agent` on PATH. Only ever asked read-only questions here."""

    def binary(self) -> str:
        found = shutil.which(AGENT_BIN)
        if found is None:
            raise _not_installed()
        return found

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.binary(), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=AGENT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
            raise UsageError(
                f"could not run {AGENT_BIN} {' '.join(args)}: {exc.__class__.__name__}",
                f"{AGENT_BIN} status",
            ) from None

    def status(self) -> AgentStatus:
        done = self._run("status", "--format", "json")
        return parse_status(done.stdout) if done.returncode == 0 else AgentStatus(False, None, None)

    def models(self) -> tuple[ModelEntry, ...]:
        done = self._run("models")
        if done.returncode != 0:
            raise UsageError(f"`{AGENT_BIN} models` failed (exit {done.returncode})", AGENT_LOGIN)
        return parse_models(done.stdout)

    def chat_exists(self, chat_id: str) -> bool:
        # Cursor keeps a chat at ~/.cursor/chats/<workspace hash>/<chat id>/.
        if not re.fullmatch(r"[0-9A-Za-z-]{8,64}", chat_id):
            return False
        chats = Path(os.path.expanduser("~")) / ".cursor" / "chats"
        try:
            return any((workspace / chat_id).is_dir() for workspace in chats.iterdir())
        except OSError:
            return False


AGENT_LOGIN: Final = f"{AGENT_BIN} login"


def require_login(agent: AgentPort) -> AgentStatus:
    status = agent.status()
    if not status.logged_in:
        raise UsageError("cursor-agent is not logged in", AGENT_LOGIN)
    return status


# --- Claude Code's model, as a Cursor model -------------------------------------------


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("-"))


def _refusal(wanted: str, models: Sequence[ModelEntry], why: str) -> UsageError:
    ids = [model.id for model in models if model.id.startswith("claude-")]
    close = get_close_matches(wanted, ids, n=5, cutoff=0.6)
    hint = f"; closest in `{AGENT_BIN} models`: {', '.join(close)}" if close else ""
    return UsageError(f"no Cursor model for {wanted}: {why}{hint}", f"{AGENT_BIN} models")


def resolve_model(
    claude_model: str,
    effort: str,
    models: Sequence[ModelEntry],
    *,
    allow_fast: bool = False,
    allow_no_zdr: bool = False,
) -> str:
    """The Cursor model id for what Claude Code would run: ``claude-<family>-<version>-<effort>``.

    ``claude_model`` is an alias (`opus`, `sonnet`, `haiku`, `fable`: the newest version Cursor
    lists) or a full id, with or without a date or a `[1m]` suffix. A "-fast" variant and one
    marked "(NO ZDR)" (no zero data retention) are never picked unless the caller says so.
    Anything else is refused with the nearest names.
    """
    if effort not in EFFORTS:
        raise UsageError(f"effort {effort!r} is not one of {', '.join(EFFORTS)}", "vibe run --help")
    name = claude_model.strip()
    base = re.sub(r"\[[0-9a-z]+\]$", "", name)
    pinned: tuple[int, ...] | None = None
    if base in FAMILIES:
        family = base
    else:
        found = _CLAUDE_MODEL.match(name)
        if found is None:
            raise _refusal(name, models, "that is not a Claude model name Cursor lists")
        family, pinned = found.group("family"), _version(found.group("version"))
    candidates: list[tuple[tuple[int, ...], ModelEntry]] = []
    blocked: list[str] = []
    for entry in models:
        parsed = _CURSOR_MODEL.match(entry.id)
        if parsed is None or parsed.group("thinking") or parsed.group("family") != family:
            continue
        if parsed.group("effort") != effort:
            continue
        version = _version(parsed.group("version"))
        if pinned is not None and version != pinned:
            continue
        if entry.fast and not allow_fast:
            blocked.append(f"{entry.id} is a fast (extra-cost) variant")
            continue
        if entry.no_zdr and not allow_no_zdr:
            blocked.append(f"{entry.id} is marked (NO ZDR): no zero data retention")
            continue
        candidates.append((version, entry))
    if not candidates:
        why = "; ".join(blocked) if blocked else f"no {family} model with effort {effort}"
        if blocked:
            why += " - pass the matching vibe flag to allow it"
        raise _refusal(name, models, why)
    return max(candidates, key=lambda pair: pair[0])[1].id
