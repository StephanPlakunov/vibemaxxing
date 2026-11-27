"""`vibe run <account>`: say exactly what will run on the account the person named.

Everything here builds a plan; cli runs it. A plan is data (an account, a program, notes for
the person), so `vibe run --dry-run` can print it and the tests can compare it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from vibemaxxing import (
    antigravity,
    bridge,
    broker,
    codex,
    codex_usage,
    credentials,
    cursor,
    cursor_translate,
    cursor_usage,
    mcp_oauth,
    route,
    store,
)
from vibemaxxing.cursor import AgentPort
from vibemaxxing.cursor_usage import CursorTokenPort, CursorUsage
from vibemaxxing.errors import NeedsLoginError, NotFoundError, UsageError, VibeError
from vibemaxxing.httpclient import CODEX_HOSTS, HttpClient, UrllibClient
from vibemaxxing.keychain import KeychainPort, config_dir_port

VIBE_LINE: Final = "vibemaxxing: "


@dataclass(frozen=True)
class RunFlags:
    """The vibe-level options of `vibe run`, before the `--`."""

    # A Cursor run: the connectors it needs, refused when the seat lacks one.
    needs: tuple[str, ...] | None = None
    allow_force: bool = False
    allow_fast: bool = False
    allow_no_zdr: bool = False
    trust_workspace: bool = False
    # Do not add vibe-bridge to a Claude run that lacks the connectors the bridge offers.
    no_bridge: bool = False
    # Environment variables to hand to a Cursor child although they look secret.
    pass_env: tuple[str, ...] = ()


@dataclass(frozen=True)
class Services:
    """What planning needs from the outside. All of it injectable; none of it global."""

    root: Path
    client: HttpClient
    port: KeychainPort
    agent: AgentPort
    cursor_client: HttpClient
    cursor_token: CursorTokenPort
    now_s: float
    environ: Mapping[str, str]
    cwd: Path
    home: Path
    # Codex: the client that may reach ChatGPT's usage endpoint only, and the CLI's login file
    # (read only). Defaults so the many places that build Services for Claude and Cursor need not.
    codex_client: HttpClient = field(default_factory=lambda: UrllibClient(CODEX_HOSTS))
    codex_auth: codex.CodexAuthPort = field(default_factory=codex.FileAuth)
    # The Keychain item Claude Code keeps the login of a config dir in. Only ever read.
    port_for: Callable[[Path], KeychainPort] = config_dir_port


@dataclass(frozen=True)
class Plan:
    alias: str
    kind: str
    # The program and arguments to run. For a Claude account the token goes into the child's
    # environment at run time; it is never part of the plan.
    argv: tuple[str, ...]
    notes: tuple[str, ...] = ()
    interactive: bool = False
    # Whether the child needs the Claude account's token in its environment.
    claude_token: bool = False
    # The account's own config dir, when Claude Code keeps its login there: the child runs with
    # CLAUDE_CONFIG_DIR set to it and no token, so Claude Code refreshes the login itself.
    config_dir: str | None = None
    # ``config_dir`` is the account's token broker mirror: vibe writes a working token into it
    # right before the child starts (broker.ensure_for_run), and the child never refreshes it.
    mirror: bool = False


# --- what Claude Code would run ------------------------------------------------------


def _settings_value(path: Path, key: str) -> str | None:
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = parsed.get(key) if isinstance(parsed, dict) else None
    return value if isinstance(value, str) and value else None


def claude_defaults(
    cwd: Path, home: Path, environ: Mapping[str, str]
) -> tuple[str | None, str | None]:
    """The model and effort Claude Code would use with no flag, in its own order.

    The environment first (ANTHROPIC_MODEL, CLAUDE_CODE_EFFORT_LEVEL), then the settings files,
    the most local first: the project's local settings, the project's, the user's.
    """
    config_dir = Path(environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    files = (
        cwd / ".claude" / "settings.local.json",
        cwd / ".claude" / "settings.json",
        config_dir / "settings.json",
    )
    model = environ.get("ANTHROPIC_MODEL") or next(
        (found for found in (_settings_value(path, "model") for path in files) if found), None
    )
    effort = environ.get("CLAUDE_CODE_EFFORT_LEVEL") or next(
        (found for found in (_settings_value(path, "effortLevel") for path in files) if found),
        None,
    )
    return model, effort


# --- readings ------------------------------------------------------------------------


@dataclass(frozen=True)
class CursorReading:
    alias: str
    logged_in: bool
    email: str | None
    team: str | None
    usage: CursorUsage | None
    # Why there is no usage, or "from HH:MM" for a cached answer; "" for a fresh one.
    note: str
    availability: cursor_usage.Availability


def read_cursor(
    svc: Services,
    alias: str,
    *,
    fresh: bool = False,
) -> CursorReading:
    """One Cursor account: logged in or not, and its usage.

    A cached answer is used for two minutes, and for 30 seconds when it is close to the cap. An
    older one (up to 15 minutes) is only shown, with its time: it never decides a run.
    """
    status = svc.agent.status()
    if not status.logged_in:
        why = "not logged in"
        return CursorReading(
            alias, False, None, None, None, why, cursor_usage.Availability(None, why)
        )
    added_at = cursor.read_added_at(svc.root, alias)
    cached = store.read_usage_cache(svc.root, alias)
    if cached is not None and cached.added_at != added_at:
        cached = None
    age = None if cached is None or cached.fetched_at is None else svc.now_s - cached.fetched_at
    held = (
        None
        if cached is None or cached.payload is None
        else cursor_usage.usage_from_payload(cached.payload)
    )
    window = cursor_usage.NEAR_CAP_FRESH_S if cursor_usage.near_cap(held) else cursor_usage.FRESH_S
    if not fresh and held is not None and age is not None and 0 <= age < window:
        return _reading(alias, status, held, "")
    usage, why = cursor_usage.fetch(svc.cursor_client, svc.cursor_token)
    if usage is not None:
        store.write_usage_cache(
            svc.root,
            alias,
            store.UsageCache(
                added_at, svc.now_s, cursor_usage.usage_to_payload(usage), None, 0, None
            ),
        )
        return _reading(alias, status, usage, "")
    if (
        held is not None
        and cached is not None
        and cached.fetched_at is not None
        and age is not None
        and 0 <= age < cursor_usage.STALE_S
    ):
        at = datetime.fromtimestamp(cached.fetched_at).strftime("%H:%M")
        return CursorReading(
            alias,
            True,
            status.email,
            status.team,
            held,
            f"{why}; showing {at}",
            # Shown, never routed by: dollars move while an answer ages.
            cursor_usage.Availability(None, f"{why}; the last answer ({at}) is too old to use"),
        )
    return _reading(alias, status, None, why)


def _reading(
    alias: str,
    status: cursor.AgentStatus,
    usage: CursorUsage | None,
    note: str,
) -> CursorReading:
    return CursorReading(
        alias,
        True,
        status.email,
        status.team,
        usage,
        note,
        cursor_usage.availability(usage, reason=note),
    )


def cursor_entry(reading: CursorReading) -> dict[str, object]:
    """The `cursor` member of the envelope for one account; every key present, null if unknown."""
    usage = reading.usage
    state = "needs_login" if not reading.logged_in else "ok" if usage is not None else "unknown"
    cycle = usage.cycle_end_s if usage is not None else None
    return {
        "alias": reading.alias,
        "kind": cursor.KIND_CURSOR,
        "state": state,
        "message": reading.note or None,
        "email": reading.email,
        "team": reading.team,
        "plan": usage.plan if usage is not None else None,
        "included_percent": None
        if usage is None or usage.included_percent is None
        else round(usage.included_percent, 1),
        "on_demand_used_cents": usage.on_demand_used_cents if usage is not None else None,
        "on_demand_limit_cents": usage.on_demand_limit_cents if usage is not None else None,
        "resets_at": None
        if cycle is None
        else datetime.fromtimestamp(cycle, UTC).isoformat(timespec="seconds"),
        "available": reading.availability.ok,
        "available_why": reading.availability.why,
    }


# --- Codex ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodexReading:
    """One Codex account: what the CLI's login says and, when it could be read, the plan's usage."""

    alias: str
    # "ok" (numbers), "unknown" (a login but no numbers), "needs_login" (no usable login),
    # "api_key" (the CLI uses an API key: there is no plan to read).
    state: str
    email: str | None
    plan: str | None
    usage: codex_usage.CodexUsage | None
    # Why there are no (fresh) numbers, or "from HH:MM" for a cached answer; "" for a fresh one.
    note: str
    fetched_at: float | None = None


def _clock(at_s: float) -> str:
    return datetime.fromtimestamp(at_s).strftime("%H:%M")


def read_codex(svc: Services, alias: str, *, fresh: bool = False) -> CodexReading:
    """One Codex account: the CLI's login and its plan usage. Never refreshes, never writes it.

    A cached answer is used for two minutes. When the access token has ended (the CLI renews it
    when it runs; vibe never does, a second refresher would spend the CLI's refresh token) or the
    request fails, an answer up to 15 minutes old is shown with its time, and nothing older.
    """
    marker = codex.read_marker(svc.root, alias)
    login = svc.codex_auth.read(marker.codex_home)
    if login is None:
        return CodexReading(alias, "needs_login", None, None, None, f"run: {codex.LOGIN_CMD}")
    if login.auth_mode == codex.AUTH_MODE_API_KEY or not login.usable:
        why = (
            "signed in with an API key: no plan usage to read"
            if login.auth_mode == codex.AUTH_MODE_API_KEY
            else f"no ChatGPT login in auth.json; run: {codex.LOGIN_CMD}"
        )
        state = "api_key" if login.auth_mode == codex.AUTH_MODE_API_KEY else "needs_login"
        return CodexReading(alias, state, login.email, login.plan, None, why)
    cached = store.read_usage_cache(svc.root, alias)
    if cached is not None and cached.added_at != marker.added_at:
        cached = None
    age = None if cached is None or cached.fetched_at is None else svc.now_s - cached.fetched_at
    held = (
        None
        if cached is None or cached.payload is None
        else codex_usage.usage_from_payload(cached.payload)
    )
    if not fresh and held is not None and age is not None and 0 <= age < codex_usage.FRESH_S:
        return CodexReading(
            alias,
            "ok",
            login.email,
            held.plan or login.plan,
            held,
            "",
            cached.fetched_at if cached else None,
        )
    ended = (
        login.expires_at is not None and login.expires_at <= svc.now_s + codex_usage.EXPIRY_SKEW_S
    )
    if ended:
        at = _clock(login.expires_at or 0)
        why = f"the Codex login's token ended {at}; run `codex` to renew it"
        usage = None
    else:
        usage, why = codex_usage.fetch(svc.codex_client, login)
    if usage is not None:
        store.write_usage_cache(
            svc.root,
            alias,
            store.UsageCache(
                marker.added_at, svc.now_s, codex_usage.usage_to_payload(usage), None, 0, None
            ),
        )
        return CodexReading(
            alias, "ok", login.email, usage.plan or login.plan, usage, "", svc.now_s
        )
    if (
        held is not None
        and cached is not None
        and cached.fetched_at is not None
        and age is not None
        and 0 <= age < codex_usage.STALE_S
    ):
        return CodexReading(
            alias,
            "ok",
            login.email,
            held.plan or login.plan,
            held,
            f"{why}; showing {_clock(cached.fetched_at)}",
            cached.fetched_at,
        )
    return CodexReading(alias, "unknown", login.email, login.plan, None, why)


def _spend_entry(spend: codex_usage.Spend) -> dict[str, object]:
    """A spend limit the plan reports, in the unit it names (never converted)."""
    return {
        "label": codex_usage.spend_title(spend),
        "unit": spend.unit,
        "used": spend.used,
        "limit": spend.limit,
        "percent": round(codex_usage.spend_percent(spend)),
        "resets_at": None
        if spend.resets_at_s is None
        else datetime.fromtimestamp(spend.resets_at_s, UTC).isoformat(timespec="seconds"),
    }


def codex_entry(reading: CodexReading) -> dict[str, object]:
    """The `codex` member of the envelope for one account; every key present, null if unknown."""
    usage = reading.usage
    windows: list[dict[str, object]] = []
    for window in usage.windows if usage is not None else ():
        windows.append(
            {
                "label": window.label,
                "percent": round(window.used_percent),
                "window_minutes": window.window_minutes,
                "resets_at": None
                if window.resets_at_s is None
                else datetime.fromtimestamp(window.resets_at_s, UTC).isoformat(timespec="seconds"),
            }
        )
    return {
        "alias": reading.alias,
        "kind": codex.KIND_CODEX,
        "state": reading.state,
        "message": reading.note or None,
        "email": reading.email,
        "plan": reading.plan,
        "limit_reached": usage.limit_reached if usage is not None else None,
        "credits_balance": usage.credits_balance if usage is not None else None,
        "credits_unlimited": usage.credits_unlimited if usage is not None else None,
        "windows": windows,
        "spend": None if usage is None or usage.spend is None else _spend_entry(usage.spend),
    }


# --- planning ------------------------------------------------------------------------


def _translate(
    svc: Services,
    alias: str,
    claude_args: Sequence[str],
    flags: RunFlags,
    *,
    needs: Sequence[str],
) -> cursor_translate.Translation:
    cursor.require_login(svc.agent)
    model, effort = claude_defaults(svc.cwd, svc.home, svc.environ)
    options = cursor_translate.Options(
        models=svc.agent.models(),
        default_model=model,
        default_effort=effort,
        needs_mcp=bool(needs),
        allow_force=flags.allow_force,
        allow_fast=flags.allow_fast,
        allow_no_zdr=flags.allow_no_zdr,
        trust_workspace=flags.trust_workspace,
        is_cursor_chat=svc.agent.chat_exists,
    )
    return cursor_translate.translate(claude_args, options)


def _cursor_plan(
    svc: Services,
    alias: str,
    command: Sequence[str],
    flags: RunFlags,
    *,
    needs: Sequence[str],
    notes: list[str],
) -> Plan:
    program = svc.agent.binary()
    if not cursor_translate.is_claude_command(command):
        return Plan(alias, cursor.KIND_CURSOR, tuple(command), tuple(notes))
    args = list(command[1:])
    translation = _translate(svc, alias, args, flags, needs=needs)
    if "--force" in translation.args:
        notes.append(f"warning: {translation.access}")  # the one mode worth a line every time
    if not translation.interactive and not flags.trust_workspace:
        notes.append(
            "headless Cursor stops at a workspace it never trusted: trust it once in "
            "`cursor-agent`, or pass --trust-workspace"
        )
    return Plan(
        alias,
        cursor.KIND_CURSOR,
        (program, *translation.args),
        tuple(notes),
        interactive=translation.interactive,
    )


# What a config dir of its own lacks compared with ~/.claude. Names only; a link brings each over.
_SHARED_NAMES: Final = ("CLAUDE.md", "settings.json", "skills", "projects")


def _config_dir_of(svc: Services, alias: str) -> str | None:
    try:
        return store.read_account(svc.root, alias).config_dir
    except VibeError:
        return None  # the broken file is reported by whoever reads it next


def _config_dir_notes(svc: Services, alias: str, directory: str) -> list[str]:
    """Refuse a config dir with no matching login; say what it does not have.

    The login is Claude Code's, so there is no token to hand over: a dir nobody logged in
    to, or one logged in as somebody else, would start a session on the wrong account.
    """
    path = Path(directory)
    login = f"CLAUDE_CONFIG_DIR={path} claude"
    if svc.port_for(path).read() is None:
        raise NeedsLoginError(
            f'Claude Code has no login for {path}, where "{alias}" lives (log in with /login)',
            login,
        )
    stored = store.read_account(svc.root, alias).identity.account_uuid
    there = credentials.read_claude_identity(path / ".claude.json")
    if stored is not None and there.account_uuid is not None and there.account_uuid != stored:
        raise UsageError(
            f'{path} is logged in as {there.email or there.account_uuid}, not as "{alias}"',
            f"vibe add {alias} --from-config-dir {path}",
        )
    notes: list[str] = []
    base = svc.home / ".claude"
    lacking = [
        name
        for name in _SHARED_NAMES
        if os.path.lexists(base / name) and not os.path.lexists(path / name)
    ]
    if lacking:
        notes.append(
            f"{path} has no {', '.join(lacking)} of its own, so what ~/.claude has there "
            f"does not apply: ln -s ~/.claude/{lacking[0]} {path}/"
        )
    return notes


def plan_run(svc: Services, alias: str, command: Sequence[str], flags: RunFlags) -> Plan:
    """The plan for `vibe run <alias> -- <command>`."""
    if alias == "auto":
        # Reserved, so never an account: say what replaced it rather than "no such account".
        raise UsageError(
            "`vibe run auto` was removed: vibe no longer picks the account, you name it",
            "vibe run <account> -- claude   (vibe list shows the accounts)",
        )
    if not command:
        raise UsageError("vibe run needs a command after --", "vibe run work -- claude")
    if codex.is_codex(svc.root, alias):
        raise UsageError(
            f'"{alias}" is a Codex account: vibe shows its usage and does not start Codex or '
            "Claude Code on it",
            "codex",
        )
    if antigravity.has_account(svc.root, alias):
        raise UsageError(f'"{alias}" is an Antigravity account: start it with agy', "agy")
    if cursor.is_cursor(svc.root, alias):
        needs = _check_needs(svc.root, alias, flags)
        notes: list[str] = []
        if cursor_translate.is_claude_command(command):
            notes += _ceiling_notes(svc, alias)
        return _cursor_plan(svc, alias, command, flags, needs=needs, notes=notes)
    if flags.needs is not None:
        raise UsageError("--needs is for Cursor accounts", f"vibe run {alias} -- claude")
    if alias not in store.list_aliases(svc.root):
        raise NotFoundError(f'there is no account named "{alias}"', "vibe list")
    argv, bridge_notes = _with_bridge(svc, alias, command, flags)
    config_dir = _config_dir_of(svc, alias)
    # A named account is always fixed to it, even when it is Claude Code's own login right now.
    mirrored = _mirror_plan(svc, alias, argv, bridge_notes)
    if mirrored is not None:
        return mirrored
    if config_dir is not None:
        notes = _config_dir_notes(svc, alias, config_dir)
        return Plan(alias, cursor.KIND_CLAUDE, argv, (*notes, *bridge_notes), config_dir=config_dir)
    return Plan(alias, cursor.KIND_CLAUDE, argv, tuple(bridge_notes), claude_token=True)


def _mirror_plan(
    svc: Services,
    alias: str,
    argv: Sequence[str],
    notes: Sequence[str],
) -> Plan | None:
    """With the token broker on: a session in the account's own mirror dir, no token override.

    The dir's Keychain item holds the account's current access token and no refresh token; vibe
    renews it there every few hours (broker.py), so the session is fixed to the account and never
    dies from its token. None when the broker is off or the account lives in its own config dir.
    """
    if not broker.is_on(svc.root) or not broker.brokered(svc.root, alias):
        return None
    directory = broker.mirror_dir(broker.read_settings(svc.root), alias, svc.home)
    return Plan(
        alias,
        cursor.KIND_CLAUDE,
        tuple(argv),
        tuple(notes),
        config_dir=str(directory),
        mirror=True,
    )


def _with_bridge(
    svc: Services,
    alias: str,
    command: Sequence[str],
    flags: RunFlags,
) -> tuple[tuple[str, ...], list[str]]:
    """``command``, with vibe-bridge added when this Claude run lacks what the bridge offers.

    A session on an account without Slack or Atlassian cannot use a bridge registered in
    that account's config dir: `vibe run` starts claude with a token override and without
    CLAUDE_CONFIG_DIR, so Claude Code reads the default config. So the bridge is handed to the run
    itself, as a vibe-owned config holding only vibe-bridge (`--mcp-config=PATH`, the one-token form
    so a prompt after it is never read as a second config; not --strict-mcp-config, so the account's
    own servers stay). Only for a Claude session, only for an upstream the bridge is logged in to
    and the account lacks, never for Cursor, and `--no-bridge` turns it off.
    """
    unchanged: tuple[tuple[str, ...], list[str]] = (tuple(command), [])
    if (
        flags.no_bridge
        or not cursor_translate.is_claude_command(command)
        or cursor_translate.is_not_a_session(command[1:])
    ):
        return unchanged
    try:
        routing = route.read_routing(svc.root)
    except UsageError:
        routing = route.Routing()  # a wrong routing must not stop a session from starting
    lacking = {upstream.id for upstream in bridge.UPSTREAMS.values()} - route.connectors_of(
        routing, alias
    )
    missing = [
        upstream.id
        for upstream in bridge.UPSTREAMS.values()
        if upstream.id in lacking and mcp_oauth.has_login(svc.root, upstream)
    ]
    if not missing:
        return unchanged
    path = bridge.write_mcp_config(svc.root)
    note = f"added vibe-bridge ({', '.join(missing)}) to this run: --no-bridge turns it off"
    return (command[0], f"--mcp-config={path}", *command[1:]), [note]


def _ceiling_notes(svc: Services, alias: str) -> list[str]:
    """Refuse a Cursor run at the money limit; warn when the usage cannot be read.

    The reading is fresh (two minutes, thirty seconds close to the limit). A seat whose on-demand
    spend has reached the limit Cursor reports is spent only once its included usage is used up
    too: below 100% included the run goes ahead, and says on-demand is gone. An unknown reading
    does not stop a run the person asked for by name, but it is said.
    """
    reading = read_cursor(svc, alias)
    included = None if reading.usage is None else reading.usage.included_percent
    if reading.logged_in and reading.availability.ok is False and included is not None:
        if included < 100:
            return [
                f"note: Cursor's on-demand is used up ({reading.availability.why}); "
                f"included usage is at {round(included)}%, running on that"
            ]
    if reading.logged_in and reading.availability.ok is False:
        raise UsageError(
            f"Cursor is at its on-demand limit: {reading.availability.why}",
            "vibe list   (the cycle resets on the date it shows)",
        )
    if reading.logged_in and reading.availability.ok is None:
        return [f"warning: Cursor usage unknown ({reading.availability.why}); running as asked"]
    return []


def _check_needs(root: Path, alias: str, flags: RunFlags) -> tuple[str, ...]:
    # A Cursor account needs only what the caller declares: nothing by default.
    needs = route.check_needs(flags.needs or ())
    missing = sorted(set(needs) - route.connectors_of(route.read_routing(root), alias))
    if missing:
        raise UsageError(
            f"{alias} has no {', '.join(missing)} connector (settings.json routing.connectors)",
            f"vibe run {alias} -- claude",
        )
    return needs
