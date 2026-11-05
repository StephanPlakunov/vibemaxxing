"""The token broker: vibe is the one refresher of every account, and Claude Code reads mirrors.

One login per account, and a refresh token has exactly one holder: vibe's store. Every Claude Code
login Claude Code reads is a **mirror**: a Keychain item that vibe keeps filled with that account's
current access token and **no refresh token**, so Claude Code has nothing to refresh with and never
calls the token endpoint for it (evidence: docs/BROKER.md). Mirrors:

- one fixed config dir per account (``~/.claude-<alias>`` unless settings name another), its own
  Claude Code Keychain item: `vibe run <alias>` starts Claude Code there, and that session stays on
  that account for good;
- Claude Code's default item (``~/.claude``): whichever account the switch made live, so `cs`,
  `cw` and a plain `claude` follow the background switch.

The tick (every minute) refreshes an account once less than ``refresh_left`` of its access
token's lifetime is left (half by default, and never later than ``MIN_LEAD_MS`` before its end,
which is well before Claude Code's own five-minute window), then rewrites every mirror of it. A
failed refresh rewrites nothing: each mirror keeps the token it has until that token really ends,
and the failure is recorded in ``broker.json`` for `vibe auto status` and `vibe list`.

A mirror that holds a real refresh token was written by Claude Code itself (a `claude /login` in
that dir, or the default item before the broker took it over). That login is the newest of its
account, so the tick adopts it into the store first (same account only) and then mirrors it, so the
account still has one holder.

Off until `vibe broker on`, which turns the default item into a mirror first. Nothing here prints a
token.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from vibemaxxing import credentials, cursor, keychain, oauth, store
from vibemaxxing.credentials import Credential, Identity
from vibemaxxing.errors import NeedsLoginError, StoreError, UsageError, VibeError
from vibemaxxing.fsutil import private_dir, write_private
from vibemaxxing.httpclient import HttpClient
from vibemaxxing.keychain import KeychainPort
from vibemaxxing.models import AccountState

SETTINGS_KEY: Final = "broker"
# The name of the mirror in Claude Code's default item (~/.claude), the live account's.
DEFAULT_MIRROR: Final = "default"
STATE_FILE: Final = "broker.json"
SCHEMA_VERSION: Final = 1
# The share of an access token's lifetime that may be left before vibe refreshes it.
DEFAULT_REFRESH_LEFT: Final = 0.5
# The lifetime assumed for a token whose issue time vibe did not see (one adopted from Claude
# Code, or stored before the broker): Claude Code's logins last eight hours.
DEFAULT_LIFETIME_MS: Final = 8 * 60 * 60 * 1000
# Never later than this before the end, whatever the share says: Claude Code calls a token
# expired five minutes before its end, and a tick may be a minute late or fail a few times.
MIN_LEAD_MS: Final = 60 * 60 * 1000
# After a transient failure: 1, 2, 4, 8, then every 15 minutes.
RETRY_BASE_S: Final = 60.0
RETRY_MAX_S: Final = 15 * 60.0
# A login Claude Code holds is not taken over while it ends within this long, or ended less than
# ADOPT_IDLE_MS ago: a session may be renewing it (auto.RENEWAL_GUARD_S, the same reasoning).
ADOPT_GUARD_MS: Final = 10 * 60 * 1000
ADOPT_IDLE_MS: Final = 15 * 60 * 1000
# The files of ~/.claude a mirror dir links to, so it reads the same instructions and settings.
SHARED_NAMES: Final = ("CLAUDE.md", "settings.json", "skills", "projects")


# --- settings --------------------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerSettings:
    on: bool = False
    # alias -> the config dir that mirrors it. An alias not named here mirrors into
    # ~/.claude-<alias> (`mirror_dir`).
    mirrors: Mapping[str, str] = field(default_factory=dict)
    refresh_left: float = DEFAULT_REFRESH_LEFT


def settings_path(root: Path) -> Path:
    return root / "settings.json"


def _settings_document(root: Path) -> dict[str, object] | None:
    try:
        parsed: object = json.loads(settings_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    return {str(k): v for k, v in parsed.items()} if isinstance(parsed, dict) else None


def read_settings(root: Path) -> BrokerSettings:
    """The broker's settings; off for a file that is missing or wrong in the broker member.

    Wrong means off: a hand edit that cannot be read must not leave vibe refreshing logins that
    Claude Code still holds.
    """
    document = _settings_document(root)
    raw = document.get(SETTINGS_KEY) if document is not None else None
    if not isinstance(raw, dict):
        return BrokerSettings()
    on = raw.get("on", False)
    mirrors = raw.get("mirrors", {})
    left = raw.get("refresh_left", DEFAULT_REFRESH_LEFT)
    if (
        not isinstance(on, bool)
        or not isinstance(mirrors, dict)
        or not all(
            isinstance(alias, str) and store.valid_alias(alias) and isinstance(path, str) and path
            for alias, path in mirrors.items()
        )
        or isinstance(left, bool)
        or not isinstance(left, (int, float))
        or not 0.05 <= float(left) <= 0.95
    ):
        return BrokerSettings()
    return BrokerSettings(on, {str(a): str(p) for a, p in mirrors.items()}, float(left))


def is_on(root: Path) -> bool:
    return read_settings(root).on


def write_settings(root: Path, settings: BrokerSettings) -> None:
    """Write the broker member and keep every other member of settings.json as it is."""
    document = _settings_document(root)
    if document is None:
        raise UsageError(
            f"{settings_path(root)} is not valid JSON, and writing the broker setting would "
            "overwrite it. Fix it first",
            f"cat {settings_path(root)}",
        )
    document.setdefault("schema", SCHEMA_VERSION)
    document[SETTINGS_KEY] = {
        "on": settings.on,
        "mirrors": dict(settings.mirrors),
        "refresh_left": settings.refresh_left,
    }
    private_dir(root)
    write_private(settings_path(root), json.dumps(document, indent=2) + "\n")


def mirror_dir(settings: BrokerSettings, alias: str, home: Path) -> Path:
    given = settings.mirrors.get(alias)
    path = Path(given).expanduser() if given else home / f".claude-{alias}"
    return Path(os.path.abspath(path))


def mirror_alias(root: Path, directory: str, home: Path) -> str | None:
    """The account a mirror dir belongs to, from the settings alone (the status line's question)."""
    settings = read_settings(root)
    if not settings.on:
        return None
    try:
        wanted = Path(directory).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    for alias in brokered_aliases(root):
        with contextlib.suppress(OSError, RuntimeError):
            if mirror_dir(settings, alias, home).resolve() == wanted:
                return alias
    return None


# --- which accounts ---------------------------------------------------------------------------


def brokered(root: Path, alias: str) -> bool:
    """Whether vibe holds ``alias``'s login: a Claude account not living in its own config dir."""
    if cursor.is_cursor(root, alias):
        return False
    try:
        return store.read_account(root, alias).config_dir is None
    except VibeError:
        return False


def brokered_aliases(root: Path) -> list[str]:
    return [alias for alias in store.list_aliases(root) if brokered(root, alias)]


# --- the mirror blob ------------------------------------------------------------------------


def mirror_credential(credential: Credential) -> Credential:
    """What a mirror holds: the account's credential with no refresh token.

    Everything else as it is, the real expiry included: vibe refreshes hours before it, so Claude
    Code never reaches its own five-minute window, and a token vibe could not renew in time ends
    when it really ends instead of being sent on as if it were good (docs/BROKER.md).
    """
    return replace(
        credential,
        refresh_token=credentials.NO_REFRESH_TOKEN,
        refresh_token_expires_at_ms=None,
    )


def mirror_blob(credential: Credential, base: Mapping[str, object] | None) -> str:
    return credentials.credential_to_blob(credential, base, mirror=True)


holds_refresh_token = credentials.holds_refresh_token


# --- state: what the broker last did, for status ----------------------------------------------


@dataclass(frozen=True)
class Holding:
    issued_at_ms: int | None = None
    last_refresh_s: float | None = None
    error: str | None = None
    failures: int = 0
    retry_at_s: float | None = None
    mirrors: Mapping[str, str] = field(default_factory=dict)


def state_path(root: Path) -> Path:
    return root / STATE_FILE


def read_state(root: Path) -> dict[str, Holding]:
    try:
        parsed: object = json.loads(state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(parsed, dict) or parsed.get("schema") != SCHEMA_VERSION:
        return {}
    accounts = parsed.get("accounts")
    out: dict[str, Holding] = {}
    for alias, raw in accounts.items() if isinstance(accounts, dict) else ():
        if not isinstance(raw, dict) or not store.valid_alias(str(alias)):
            continue
        mirrors = raw.get("mirrors")
        out[str(alias)] = Holding(
            issued_at_ms=_int(raw.get("issued_at_ms")),
            last_refresh_s=_num(raw.get("last_refresh_s")),
            error=raw.get("error") if isinstance(raw.get("error"), str) else None,
            failures=_int(raw.get("failures")) or 0,
            retry_at_s=_num(raw.get("retry_at_s")),
            mirrors={str(k): str(v) for k, v in mirrors.items()}
            if isinstance(mirrors, dict)
            else {},
        )
    return out


def write_state(root: Path, states: Mapping[str, Holding]) -> None:
    document = {
        "schema": SCHEMA_VERSION,
        "written_at": time.time(),
        "accounts": {
            alias: {
                "issued_at_ms": s.issued_at_ms,
                "last_refresh_s": s.last_refresh_s,
                "error": s.error,
                "failures": s.failures,
                "retry_at_s": s.retry_at_s,
                "mirrors": dict(s.mirrors),
            }
            for alias, s in states.items()
        },
    }
    with contextlib.suppress(OSError):
        private_dir(root)
        write_private(state_path(root), json.dumps(document, indent=2) + "\n")


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# --- when to refresh --------------------------------------------------------------------------


def refresh_at_ms(credential: Credential, issued_at_ms: int | None, refresh_left: float) -> int:
    """When vibe refreshes this token: ``refresh_left`` of its lifetime before its end, and never
    later than MIN_LEAD_MS before it."""
    end = credential.expires_at_ms
    lifetime = end - issued_at_ms if issued_at_ms is not None and issued_at_ms < end else None
    if lifetime is None:
        lifetime = DEFAULT_LIFETIME_MS
    lead = max(int(lifetime * refresh_left), MIN_LEAD_MS)
    return end - lead


def due(
    credential: Credential, issued_at_ms: int | None, refresh_left: float, *, now_ms: int
) -> bool:
    return now_ms >= refresh_at_ms(credential, issued_at_ms, refresh_left)


# --- the work ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Mirror:
    """One Keychain item vibe keeps for one account."""

    alias: str
    # "default" for Claude Code's own item, else the config dir.
    name: str
    port: KeychainPort
    # The file whose oauthAccount names the account (a dir's .claude.json); None for the default
    # item, whose ~/.claude.json the switch writes.
    profile: Path | None
    # The config dir whose Claude Code write lock guards the item (keychain.storage_lock).
    lock_dir: Path


@dataclass(frozen=True)
class Report:
    """What one broker pass did, one line per thing worth saying."""

    lines: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


def _identity_ok(profile: Path, identity: Identity) -> bool:
    there = credentials.read_claude_identity(profile).account_uuid
    return there is not None and there == identity.account_uuid


def write_mirror(
    mirror: Mirror,
    credential: Credential,
    identity: Identity,
    *,
    still_wanted: Callable[[], bool] = lambda: True,
) -> str:
    """Make ``mirror`` hold ``credential`` (without its refresh token). One Keychain write, only
    when something changed; the profile only when it names another account or none.

    ``still_wanted`` is asked again under Claude Code's write lock, right before the write: a
    `vibe broker off` or a switch in another process since this pass began makes it False, and
    then nothing is written (`broker off` and `store.switch` write the default item under the
    same lock). Returns "written", "unchanged" or "skipped". Raises VibeError when the item
    cannot be written.
    """
    with keychain.storage_lock(mirror.lock_dir):
        if not still_wanted():
            return "skipped"
        raw = mirror.port.read()
        if credentials.holds_refresh_token(raw) and not _same_lineage(raw, credential):
            # Claude Code holds a login here (a `claude /login` since the last look): never
            # written over. The next pass adopts it if it is this account's.
            raise VibeError(f"{mirror.name} holds a login vibe did not write", "vibe auto status")
        base = credentials.parse_blob_members(raw) if raw is not None else None
        wanted = mirror_credential(credential)
        current: Credential | None = None
        if raw is not None:
            with contextlib.suppress(StoreError):
                current = credentials.parse_blob(raw, mirror_ok=True)
        changed = current != wanted
        if changed:
            mirror.port.write(mirror_blob(credential, base))
    if mirror.profile is not None and not _identity_ok(mirror.profile, identity):
        private_dir(mirror.profile.parent)
        credentials.write_claude_identity(mirror.profile, identity)
    return "written" if changed else "unchanged"


def _same_lineage(raw: str | None, credential: Credential) -> bool:
    """Whether a full login in an item is the very one vibe holds (just adopted, or the default
    item before `vibe broker on` stripped it): then the mirror may replace it."""
    if raw is None:
        return False
    try:
        return credentials.parse_blob(raw).refresh_token == credential.refresh_token
    except StoreError:
        return False


def mirrors_of(
    root: Path,
    alias: str,
    *,
    settings: BrokerSettings,
    default_port: KeychainPort,
    port_for: Callable[[Path], KeychainPort],
    home: Path,
) -> list[Mirror]:
    """Every mirror of ``alias``: its own dir (once that dir exists), and the default item when
    ``alias`` is the account vibe made live."""
    found: list[Mirror] = []
    directory = mirror_dir(settings, alias, home)
    if directory.is_dir() and directory.resolve() not in _owned_dirs(root):
        port = port_for(directory)
        found.append(Mirror(alias, str(directory), port, directory / ".claude.json", directory))
    if store.read_active(root) == alias:
        found.append(Mirror(alias, DEFAULT_MIRROR, default_port, None, home / ".claude"))
    return found


def _adopt(root: Path, alias: str, mirror: Mirror, *, now_ms: int, home: Path) -> str | None:
    """Take a login Claude Code holds in ``mirror`` into the store, under the account's claim.

    Only for the same account (the profile next to it names the stored uuid) and only when it is
    newer than the stored one. Returns a line saying what happened, or None for nothing to adopt.
    """
    raw = mirror.port.read()
    if not holds_refresh_token(raw):
        return None
    assert raw is not None
    found = credentials.parse_blob(raw)
    account = store.read_account(root, alias)
    profile = mirror.profile if mirror.profile is not None else home / ".claude.json"
    there = credentials.read_claude_identity(profile).account_uuid
    if account.identity.account_uuid is None or there != account.identity.account_uuid:
        return None  # not this account's: write_mirror refuses it and the pass reports it
    if found.refresh_token == account.credential.refresh_token:
        return None  # already ours: the mirror is just not stripped yet
    if -ADOPT_IDLE_MS < found.expires_at_ms - now_ms <= ADOPT_GUARD_MS:
        # A session holding this login may be renewing it from memory right now (Claude Code
        # renews in the last five minutes and caches the item for 30 s); taking it now could
        # take a refresh token that is about to be spent. The next pass looks again.
        return None
    with store.hold_refresh_claim(root, alias, "vibe auto tick"):
        account = store.read_account(root, alias)
        if found.expires_at_ms < account.credential.expires_at_ms:
            # Same account, older than vibe's: a second holder of this account all the same.
            # The mirror replaces it, so the account is held in one place again.
            with keychain.storage_lock(mirror.lock_dir):
                current = mirror.port.read()
                if current == raw:
                    base = credentials.parse_blob_members(raw)
                    mirror.port.write(mirror_blob(account.credential, base))
            return (
                f"{mirror.name} held an older login of {alias} that Claude Code made; "
                "replaced by vibe's mirror"
            )
        store.write_account(
            root, replace(account, credential=found, state=AccountState.OK, message=None)
        )
        store.delete_stash(root, alias)
    return f"took over the login Claude Code holds in {mirror.name} for {alias}"


def _refresh(
    root: Path,
    alias: str,
    state: Holding,
    client: HttpClient,
    *,
    now_ms: int,
) -> tuple[Credential | None, Holding, str | None]:
    """Refresh ``alias`` through oauth.refresh (the claim, the stash, the dead-token mark)."""
    account = store.read_account(root, alias)
    outcome = oauth.refresh(root, alias, account.credential, client, now_ms=now_ms)
    now_s = now_ms / 1000
    if outcome.credential is not None and (outcome.error is None or outcome.stashed):
        fresh = replace(
            state,
            issued_at_ms=now_ms,
            last_refresh_s=now_s,
            error=None,
            failures=0,
            retry_at_s=None,
        )
        return outcome.credential, fresh, None
    if outcome.error == "busy":
        return None, state, None  # another vibe is refreshing it this very moment
    failures = state.failures + 1
    dead = outcome.error in ("invalid_grant", "no_refresh_token")
    retry = None if dead else now_s + min(RETRY_BASE_S * 2 ** (failures - 1), RETRY_MAX_S)
    why = (
        f'the refresh token for "{alias}" was rejected - run: vibe add {alias}'
        if dead
        else f"refresh failed ({outcome.error})"
    )
    return None, replace(state, error=why, failures=failures, retry_at_s=retry), why


def run(
    root: Path,
    *,
    client: HttpClient,
    default_port: KeychainPort,
    port_for: Callable[[Path], KeychainPort],
    now_s: float,
    home: Path | None = None,
    only: Iterable[str] | None = None,
) -> Report:
    """One broker pass: adopt, refresh what is due, rewrite every mirror. Safe every minute.

    ``only`` limits it to some accounts (`vibe run <alias>` makes sure of its own before it
    starts). Nothing is refreshed while the broker is off.
    """
    settings = read_settings(root)
    if not settings.on:
        return Report()
    home = Path.home() if home is None else home
    now_ms = int(now_s * 1000)
    states = read_state(root)
    lines: list[str] = []
    problems: list[str] = []
    wanted = set(only) if only is not None else None
    touched: list[str] = []
    for alias in brokered_aliases(root):
        if wanted is not None and alias not in wanted:
            continue
        state = states.get(alias, Holding())
        touched.append(alias)
        own = mirror_dir(settings, alias, home)
        with contextlib.suppress(OSError, RuntimeError):
            if own.is_dir() and own.resolve() in _owned_dirs(root):
                problems.append(
                    f"{alias}: {own} is the config dir an account lives in, so it gets no mirror "
                    f"(vibe broker mirror {alias} <another dir>)"
                )
        try:
            mirrors = mirrors_of(
                root,
                alias,
                settings=settings,
                default_port=default_port,
                port_for=port_for,
                home=home,
            )
            for mirror in mirrors:
                with contextlib.suppress(VibeError):
                    said = _adopt(root, alias, mirror, now_ms=now_ms, home=home)
                    if said is not None:
                        lines.append(said)
                        if said.startswith("took over"):
                            state = replace(state, issued_at_ms=None)
            account = store.read_account(root, alias)
            credential = account.credential
            dead = account.state is AccountState.NEEDS_LOGIN
            waiting = state.retry_at_s is not None and now_s < state.retry_at_s
            if (
                not dead
                and not waiting
                and due(credential, state.issued_at_ms, settings.refresh_left, now_ms=now_ms)
            ):
                fresh, state, why = _refresh(root, alias, state, client, now_ms=now_ms)
                if fresh is not None:
                    credential = fresh
                    lines.append(f"refreshed {alias}")
                elif why is not None:
                    problems.append(f"{alias}: {why}")
            elif dead:
                why = account.message or f'the login for "{alias}" was rejected'
                state = replace(state, error=why)
                problems.append(f"{alias}: {why}")
            elif state.error is not None:
                problems.append(f"{alias}: {state.error}")
            written: dict[str, str] = dict(state.mirrors)
            for mirror in mirrors:
                if dead and credentials.is_expired(credential, now_ms=now_ms, buffer_ms=0):
                    continue  # nothing usable to write; the old mirror ends on its own
                try:
                    if (
                        write_mirror(
                            mirror,
                            credential,
                            account.identity,
                            still_wanted=_still_wanted(root, alias, mirror),
                        )
                        == "written"
                    ):
                        lines.append(f"mirrored {alias} into {mirror.name}")
                    written[mirror.name] = _fingerprint(credential)
                except (VibeError, OSError) as exc:
                    message = exc.message if isinstance(exc, VibeError) else type(exc).__name__
                    problems.append(f"{alias}: could not write {mirror.name}: {message}")
            state = replace(state, mirrors=written)
        except VibeError as exc:
            problems.append(f"{alias}: {exc.message}")
        states[alias] = state
    # Merged, not replaced: a `vibe run` pass (``only``) beside the tick must not roll back what
    # the other wrote about the accounts it did not touch.
    merged = read_state(root)
    merged.update({alias: states[alias] for alias in touched})
    write_state(root, merged)
    return Report(tuple(lines), tuple(problems))


def _still_wanted(root: Path, alias: str, mirror: Mirror) -> Callable[[], bool]:
    def check() -> bool:
        if not is_on(root):
            return False
        return mirror.name != DEFAULT_MIRROR or store.read_active(root) == alias

    return check


def _owned_dirs(root: Path) -> set[Path]:
    """The config dirs accounts live in (`--from-config-dir`): Claude Code's, never a mirror."""
    found: set[Path] = set()
    for alias in store.list_aliases(root):
        with contextlib.suppress(VibeError, OSError, RuntimeError):
            given = store.read_account(root, alias).config_dir
            if given is not None:
                found.add(Path(given).expanduser().resolve())
    return found


def _safe_read(port: KeychainPort) -> str | None:
    try:
        return port.read()
    except VibeError:
        return None


def _fingerprint(credential: Credential) -> str:
    """Which token a mirror got, without the token: its expiry, as text."""
    return str(credential.expires_at_ms)


# --- turning it on and off --------------------------------------------------------------------


def turn_on(
    root: Path,
    *,
    client: HttpClient,
    default_port: KeychainPort,
    port_for: Callable[[Path], KeychainPort],
    now_s: float,
    home: Path | None = None,
) -> Report:
    """Make vibe the one refresher: take the default login over as a mirror, then sync all.

    Under the live account's claim: the login Claude Code holds in the default item is the
    newest of that account, so it goes into the store first, then the item gets the mirror.
    Refused while that login ends within the renewal window: a running session may be about to
    renew it from the copy it holds in memory, and that would spend the token vibe just took.
    """
    home = Path.home() if home is None else home
    from vibemaxxing import auto  # the renewal window lives with the tick

    active = store.read_active(root)
    if active is None:
        raise UsageError("vibe has no active account", "vibe switch <name>")
    live = store.live_alias(root, default_port)
    if live != active:
        raise UsageError(
            f"Claude Code's own login is {live or 'not an account vibe knows'}, vibe's active "
            f"account is {active}; they must agree first",
            f"vibe switch {live or active}",
        )
    if not brokered(root, active):
        raise UsageError(f'"{active}" lives in its own config dir', f"vibe release {active}")
    raw = default_port.read()
    if holds_refresh_token(raw):
        waiting = auto.renewal_wait(default_port, active, now_s)
        if waiting is not None:
            raise UsageError(waiting, "vibe broker on   (again, in a few minutes)")
    # Everything under the active account's claim and Claude Code's write lock, "on" included: a
    # switch (which takes the same claim and lock) lands either before, and is refused below as a
    # disagreement, or after, and then writes a mirror.
    with (
        store.hold_refresh_claim(root, active, "vibe broker on"),
        keychain.storage_lock(home / ".claude"),
    ):
        if store.read_active(root) != active:
            raise UsageError("a switch just ran", "vibe broker on")
        raw = default_port.read()
        if holds_refresh_token(raw):
            assert raw is not None
            account = store.read_account(root, active)
            taken = credentials.parse_blob(raw)
            store.write_account(
                root, replace(account, credential=taken, state=AccountState.OK, message=None)
            )
            store.delete_stash(root, active)
            base = credentials.parse_blob_members(raw)
            default_port.write(mirror_blob(taken, base))
        settings = read_settings(root)
        write_settings(root, replace(settings, on=True))
    return run(
        root,
        client=client,
        default_port=default_port,
        port_for=port_for,
        now_s=now_s,
        home=home,
    )


def turn_off(
    root: Path,
    *,
    default_port: KeychainPort,
    port_for: Callable[[Path], KeychainPort],
    home: Path | None = None,
) -> list[str]:
    """Give the live login back to Claude Code and remove every dir mirror.

    The default item gets the live account's whole login (refresh token included): from here on
    Claude Code renews it, as before the broker. Dir mirrors hold no refresh token and would end
    with their access token, so they are deleted; an account that should live in its dir again
    gets it with `vibe add <alias> --from-config-dir <dir> --adopt-stored-login`.
    """
    home = Path.home() if home is None else home
    settings = read_settings(root)
    if not settings.on:
        return ["the broker is already off"]
    said: list[str] = []
    active = store.read_active(root)
    if active is not None and brokered(root, active):
        with (
            store.hold_refresh_claim(root, active, "vibe broker off"),
            keychain.storage_lock(home / ".claude"),
        ):
            # Off first, under the claim and the write lock: no refresh of this account can start
            # after this line, one under way holds the claim (this waits for it), and a pass that
            # is about to write the default mirror asks again under the lock and stops.
            write_settings(root, replace(settings, on=False))
            account = store.read_account(root, active)
            raw = default_port.read()
            if holds_refresh_token(raw) and not _same_lineage(raw, account.credential):
                # A `claude /login` since: that login is the only copy of its refresh token.
                said.append(
                    "Claude Code's default login holds a login vibe did not write; left alone "
                    f"({active}'s login stays in vibe: vibe switch {active} puts it there)"
                )
            else:
                base = credentials.parse_blob_members(raw) if raw is not None else None
                default_port.write(credentials.credential_to_blob(account.credential, base))
                said.append(f"gave {active}'s login back to Claude Code's default login")
    else:
        write_settings(root, replace(settings, on=False))
    for alias in brokered_aliases(root):
        directory = mirror_dir(settings, alias, home)
        if not directory.is_dir():
            continue
        if directory.resolve() in _owned_dirs(root):
            continue
        port = port_for(directory)
        with contextlib.suppress(VibeError), keychain.storage_lock(directory):
            raw = port.read()
            if holds_refresh_token(raw):
                said.append(f"{directory} holds a login vibe did not write; left alone")
                continue
            if raw is not None:
                port.delete()
                said.append(f"removed the mirror of {alias} in {directory}")
    return said


def prepare_dir(directory: Path, home: Path) -> list[str]:
    """Create a mirror dir (0700) and link what ~/.claude shares; returns what it did."""
    said: list[str] = []
    if not directory.is_dir():
        private_dir(directory)
        said.append(f"created {directory}")
    base = home / ".claude"
    for name in SHARED_NAMES:
        source, target = base / name, directory / name
        if os.path.lexists(source) and not os.path.lexists(target):
            os.symlink(source, target)
            said.append(f"linked {target} -> {source}")
    return said


def ensure_for_run(
    root: Path,
    alias: str,
    *,
    client: HttpClient,
    default_port: KeychainPort,
    port_for: Callable[[Path], KeychainPort],
    now_s: float,
    home: Path | None = None,
) -> Path:
    """The mirror dir `vibe run <alias>` starts Claude Code in, holding a token that works now.

    Creates the dir and its links when missing, refreshes the account when due and writes the
    mirror. Refuses when the mirror cannot hold a working token (a dead login).
    """
    home = Path.home() if home is None else home
    settings = read_settings(root)
    directory = mirror_dir(settings, alias, home)
    prepare_dir(directory, home)
    report = run(
        root,
        client=client,
        default_port=default_port,
        port_for=port_for,
        now_s=now_s,
        home=home,
        only=(alias,),
    )
    raw = port_for(directory).read()
    if holds_refresh_token(raw):
        # A login Claude Code holds there that vibe did not take over (another account's, or one
        # made in the last minutes): a session would run as whoever that is, labelled as alias.
        raise UsageError(
            f"{directory} holds a Claude Code login vibe did not write, so a session there might "
            f"not be {alias}; `vibe broker status` says whose",
            "vibe broker status",
        )
    there = credentials.read_claude_identity(directory / ".claude.json").account_uuid
    stored = store.read_account(root, alias).identity.account_uuid
    if stored is not None and there != stored:
        raise UsageError(
            f"{directory / '.claude.json'} does not name the account stored as {alias!r}",
            "vibe broker status",
        )
    try:
        held = credentials.parse_blob(raw, mirror_ok=True) if raw is not None else None
    except StoreError:
        held = None
    if held is None or credentials.is_expired(held, now_ms=int(now_s * 1000)):
        why = "; ".join(report.problems) or f"no working token for {alias}"
        raise NeedsLoginError(f"cannot start a session on {alias}: {why}", f"vibe add {alias}")
    return directory


# --- what to show ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    alias: str
    expires_at_ms: int
    refresh_at_ms: int
    mirrors: tuple[str, ...]
    problem: str | None
    retry_at_s: float | None


def describe(root: Path, *, home: Path | None = None) -> list[Row]:
    """One row per account vibe holds: when its token ends and is renewed, where it is mirrored,
    and the last problem (a failed refresh, a mirror it could not write). Reads files only."""
    home = Path.home() if home is None else home
    settings = read_settings(root)
    states = read_state(root)
    rows: list[Row] = []
    for alias in brokered_aliases(root):
        try:
            account = store.read_account(root, alias)
        except VibeError:
            continue
        state = states.get(alias, Holding())
        names = []
        directory = mirror_dir(settings, alias, home)
        if directory.is_dir():
            names.append(_shown(directory, home))
        if store.read_active(root) == alias:
            names.append("default")
        problem = state.error
        if account.state is AccountState.NEEDS_LOGIN:
            problem = account.message or f'the login for "{alias}" was rejected'
        rows.append(
            Row(
                alias,
                account.credential.expires_at_ms,
                refresh_at_ms(account.credential, state.issued_at_ms, settings.refresh_left),
                tuple(names),
                problem,
                state.retry_at_s,
            )
        )
    return rows


def problems(root: Path, *, home: Path | None = None) -> list[str]:
    """The broker's open problems, one line each; [] while it is off."""
    if not is_on(root):
        return []
    return [f"{row.alias}: {row.problem}" for row in describe(root, home=home) if row.problem]


def _shown(directory: Path, home: Path) -> str:
    try:
        return f"~/{directory.relative_to(home)}"
    except ValueError:
        return str(directory)
