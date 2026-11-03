"""`vibe run` for Cursor and for `auto`: pick the account, then say exactly what will run.

Everything here builds a plan; cli runs it. A plan is data (an account, a program, notes for
the person), so `vibe run --dry-run` can print it and the tests can compare it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from vibemaxxing import (
    childenv,
    cursor,
    cursor_translate,
    cursor_usage,
    envelope,
    route,
    store,
)
from vibemaxxing.cursor import AgentPort
from vibemaxxing.cursor_usage import CursorTokenPort, CursorUsage
from vibemaxxing.envelope import AccountView
from vibemaxxing.errors import NotFoundError, UsageError
from vibemaxxing.httpclient import HttpClient
from vibemaxxing.keychain import KeychainPort
from vibemaxxing.models import AccountState
from vibemaxxing.poll import USAGE_FRESH_S
from vibemaxxing.pool import WEEKLY_ALL_KIND, blocking

AUTO: Final = "auto"
VIBE_LINE: Final = "vibe: "


@dataclass(frozen=True)
class RunFlags:
    """The vibe-level options of `vibe run`, before the `--`."""

    needs: tuple[str, ...] | None = None
    no_needs: bool = False
    claude_only: bool = False
    allow_force: bool = False
    allow_fast: bool = False
    allow_no_zdr: bool = False
    trust_workspace: bool = False
    # Environment variables to hand to a Cursor child although they look secret.
    pass_env: tuple[str, ...] = ()
    brief: str | None = None
    handover: Path | None = None

    @property
    def routing_flags(self) -> bool:
        return bool(
            self.needs is not None
            or self.no_needs
            or self.claude_only
            or self.brief
            or self.handover
        )


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


@dataclass(frozen=True)
class Plan:
    alias: str
    kind: str
    # The program and arguments to run. For a Claude account the token goes into the child's
    # environment at run time; it is never part of the plan.
    argv: tuple[str, ...]
    notes: tuple[str, ...] = ()
    route: route.Route | None = None
    interactive: bool = False
    # Whether the child needs the Claude account's token in its environment.
    claude_token: bool = False
    # `claude --version` and friends: no account, no environment of ours.
    passthrough: bool = False


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
    margin_cents: int,
    ceiling_cents: int = cursor_usage.HARD_CAP_CENTS,
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
    window = (
        cursor_usage.NEAR_CAP_FRESH_S
        if cursor_usage.near_cap(held, margin_cents=margin_cents, ceiling_cents=ceiling_cents)
        else cursor_usage.FRESH_S
    )
    if not fresh and held is not None and age is not None and 0 <= age < window:
        return _reading(alias, status, held, "", margin_cents, ceiling_cents)
    usage, why = cursor_usage.fetch(svc.cursor_client, svc.cursor_token)
    if usage is not None:
        store.write_usage_cache(
            svc.root,
            alias,
            store.UsageCache(
                added_at, svc.now_s, cursor_usage.usage_to_payload(usage), None, 0, None
            ),
        )
        return _reading(alias, status, usage, "", margin_cents, ceiling_cents)
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
    return _reading(alias, status, None, why, margin_cents, ceiling_cents)


def _reading(
    alias: str,
    status: cursor.AgentStatus,
    usage: CursorUsage | None,
    note: str,
    margin_cents: int,
    ceiling_cents: int,
) -> CursorReading:
    return CursorReading(
        alias,
        True,
        status.email,
        status.team,
        usage,
        note,
        cursor_usage.availability(
            usage, margin_cents=margin_cents, reason=note, ceiling_cents=ceiling_cents
        ),
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


def _claude_candidate(view: AccountView, routing: route.Routing, now_s: float) -> route.Candidate:
    alias = view.alias
    if view.state is AccountState.NEEDS_LOGIN:
        return route.Candidate(
            alias, cursor.KIND_CLAUDE, route.Health.DEAD, view.message or "login lapsed"
        )
    rows = view.summary.rows if view.summary is not None else ()
    if not rows or view.updated_at is None:
        return route.Candidate(
            alias, cursor.KIND_CLAUDE, route.Health.UNKNOWN, view.message or "no numbers"
        )
    fresh = 0 <= now_s - view.updated_at < USAGE_FRESH_S
    weekly = next((row.percent for row in rows if row.kind == WEEKLY_ALL_KIND), None)
    spent = blocking(rows, routing.spent_percent, now_s)
    if spent:
        worst = max(spent, key=lambda row: row.percent or 0)
        return route.Candidate(
            alias,
            cursor.KIND_CLAUDE,
            route.Health.OUT,
            f"{worst.label} at {worst.percent}%",
            weekly,
            fresh,
        )
    if not fresh:
        age = int((now_s - view.updated_at) // 60)
        return route.Candidate(
            alias,
            cursor.KIND_CLAUDE,
            route.Health.UNKNOWN,
            f"numbers are {age} min old",
            weekly,
            False,
        )
    return route.Candidate(alias, cursor.KIND_CLAUDE, route.Health.OK, "has room", weekly, True)


def _cursor_candidate(reading: CursorReading) -> route.Candidate:
    if not reading.logged_in:
        return route.Candidate(
            reading.alias,
            cursor.KIND_CURSOR,
            route.Health.DEAD,
            "not logged in: cursor-agent login",
        )
    ok = reading.availability.ok
    health = route.Health.UNKNOWN if ok is None else route.Health.OK if ok else route.Health.OUT
    return route.Candidate(
        reading.alias, cursor.KIND_CURSOR, health, reading.availability.why or reading.note
    )


def candidates(svc: Services, routing: route.Routing) -> Callable[[str], route.Candidate | None]:
    """A lookup of one account by alias, read only when the order first asks for it.

    The order stops at the first account that qualifies, so Cursor is not asked for its usage
    (a process and two requests) when main already has room.
    """
    found: dict[str, route.Candidate | None] = {}
    claude: dict[str, route.Candidate] = {}
    seats = set(cursor.list_cursor_aliases(svc.root))

    def lookup(alias: str) -> route.Candidate | None:
        if alias in found:
            return found[alias]
        if alias in seats:
            reading = read_cursor(
                svc,
                alias,
                margin_cents=routing.cursor_margin_cents,
                ceiling_cents=routing.cursor_cap_cents,
            )
            found[alias] = _cursor_candidate(reading)
        else:
            if not claude:
                views = envelope.collect(
                    svc.root, client=svc.client, port=svc.port, now_s=svc.now_s
                )
                claude.update({v.alias: _claude_candidate(v, routing, svc.now_s) for v in views})
            found[alias] = claude.get(alias)
        return found[alias]

    return lookup


# --- planning ------------------------------------------------------------------------


def _with_prompt(args: Sequence[str], text: str) -> list[str]:
    return [*args, "--", text] if text.startswith("-") else [*args, text]


def _fresh_run_prompt(flags: RunFlags) -> str:
    parts: list[str] = []
    if flags.brief:
        parts.append(flags.brief)
    if flags.handover is not None:
        parts.append(
            f"Continue earlier work. Read the handover notes in {flags.handover} first, "
            "then carry on from there."
        )
    return "\n\n".join(parts)


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
    moved: bool,
    chosen: route.Route | None,
    why_moved: str | None = None,
    main_alias: str = "main",
) -> Plan:
    program = svc.agent.binary()
    if not cursor_translate.is_claude_command(command):
        return Plan(alias, cursor.KIND_CURSOR, tuple(command), tuple(notes), chosen)
    args = list(command[1:])
    if moved:
        args, removed = cursor_translate.strip_resume(args)
        if removed:
            text = _fresh_run_prompt(flags)
            notes.append(
                "a Claude session cannot be resumed on Cursor: starting a fresh run"
                + (" with the original brief" if flags.brief else "")
                + (" and the handover file" if flags.handover else "")
                + ("" if text else " (no --brief or --handover was given, so it starts empty)")
            )
            if text:
                args = _with_prompt(args, text)
    translation = _translate(svc, alias, args, flags, needs=needs)
    if not translation.interactive and not flags.trust_workspace:
        notes.append(
            "headless cursor-agent stops at a workspace it was never told to trust (exit 1, no "
            "output); trust this one once in `cursor-agent`, or pass --trust-workspace"
        )
    notes.append(f"model {translation.model} (Cursor's name for the Claude model)")
    notes.append(f"what it may do: {translation.access}")
    if translation.interactive:
        # The last thing printed before cursor-agent's own UI takes the terminal.
        lead = f"{why_moved} \u2192 s" if why_moved else "S"
        notes.append(
            f"{lead}tarting Cursor's agent with {translation.model}; no Claude hooks, skills or "
            f"Remote Control here"
            + (f"; `vibe run {main_alias} -- claude` to override" if why_moved else "")
        )
    return Plan(
        alias,
        cursor.KIND_CURSOR,
        (program, *translation.args),
        tuple(notes),
        chosen,
        interactive=translation.interactive,
    )


def plan_run(svc: Services, alias: str, command: Sequence[str], flags: RunFlags) -> Plan:
    """The plan for `vibe run <alias> -- <command>`; ``alias`` may be ``auto``."""
    if not command:
        raise UsageError("vibe run needs a command after --", "vibe run work -- claude")
    if alias == AUTO:
        return _plan_auto(svc, command, flags)
    if cursor.is_cursor(svc.root, alias):
        if flags.claude_only:
            raise UsageError(
                "--claude-only and a Cursor account contradict each other", "vibe run --help"
            )
        if flags.brief or flags.handover:
            raise UsageError(
                "--brief and --handover are for `vibe run auto`", "vibe run auto --help"
            )
        needs = _check_needs(svc.root, alias, flags)
        notes: list[str] = []
        if cursor_translate.is_claude_command(command):
            notes += _ceiling_notes(svc, alias)
        return _cursor_plan(
            svc, alias, command, flags, needs=needs, notes=notes, moved=False, chosen=None
        )
    if flags.routing_flags:
        raise UsageError(
            "--needs, --no-needs, --claude-only, --brief and --handover are for "
            "`vibe run auto` and Cursor accounts",
            "vibe run auto -- claude",
        )
    if alias not in store.list_aliases(svc.root):
        raise NotFoundError(f'there is no account named "{alias}"', "vibe list")
    return Plan(alias, cursor.KIND_CLAUDE, tuple(command), claude_token=True)


def _ceiling_notes(svc: Services, alias: str) -> list[str]:
    """Refuse a Cursor run at the money limit; warn when the usage cannot be read.

    The reading is fresh (two minutes, thirty seconds close to the cap). A seat whose on-demand
    spend is within the margin of the cap, or of vibe's own ceiling, is spent. An unknown reading
    does not stop a run the person asked for by name, but it is said.
    """
    routing = route.read_routing(svc.root)
    reading = read_cursor(
        svc,
        alias,
        margin_cents=routing.cursor_margin_cents,
        ceiling_cents=routing.cursor_cap_cents,
    )
    if reading.logged_in and reading.availability.ok is False:
        raise UsageError(
            f"Cursor is at its on-demand limit: {reading.availability.why}",
            "vibe list   (the cycle resets on the date it shows)",
        )
    if reading.logged_in and reading.availability.ok is None:
        return [f"warning: Cursor usage unknown ({reading.availability.why}); running as asked"]
    return []


def _check_needs(root: Path, alias: str, flags: RunFlags) -> tuple[str, ...]:
    routing = route.read_routing(root)
    # An explicit Cursor account needs only what the caller declares: nothing by default.
    needs = route.needs_for(routing, needs=flags.needs or (), no_needs=False)
    role = next((name for name, held in routing.roles.items() if held == alias), alias)
    have = set(routing.connectors.get(role, routing.connectors.get(alias, ())))
    missing = sorted(set(needs) - have)
    if missing:
        raise UsageError(
            f"{alias} has no {', '.join(missing)} connector (settings.json routing.connectors)",
            "vibe run auto --needs ... -- claude",
        )
    return needs


def _plan_auto(svc: Services, command: Sequence[str], flags: RunFlags) -> Plan:
    if not cursor_translate.is_claude_command(command):
        raise UsageError(
            "vibe run auto routes `claude` only; use `vibe run <account> -- <command>` for "
            "anything else",
            "vibe run auto -- claude",
        )
    if cursor_translate.is_not_a_session(command[1:]):
        # `claude --version`, `claude mcp list`: nothing to route, and nothing to spend.
        return Plan(
            "-", cursor.KIND_CLAUDE, tuple(command), ("not a session: ran as is",), passthrough=True
        )
    routing = route.read_routing(svc.root)
    needs = route.needs_for(routing, needs=flags.needs, no_needs=flags.no_needs)
    if flags.needs is not None and flags.no_needs:
        raise UsageError("--needs and --no-needs contradict each other", "vibe run --help")
    chosen = route.decide(
        candidates(svc, routing),
        routing=routing,
        needs=needs,
        claude_only=flags.claude_only,
        now_s=svc.now_s,
    )
    notes = [f"auto picked {chosen.alias} ({chosen.kind}): {chosen.reason}"]
    notes += [f"skipped {line}" for line in chosen.skipped if "Claude-only" not in line]
    notes += [f"warning: {warning}" for warning in chosen.warnings]
    if chosen.kind == cursor.KIND_CURSOR:
        passed_over = next((line for line in chosen.skipped if "Claude-only" not in line), None)
        return _cursor_plan(
            svc,
            chosen.alias,
            command,
            flags,
            needs=needs,
            notes=notes,
            moved=True,
            chosen=chosen,
            why_moved=passed_over,
            main_alias=routing.roles["main"],
        )
    args = list(command[1:])
    given = cursor_translate.resume_id(args)
    if given is not None and svc.agent.chat_exists(given):
        args, _ = cursor_translate.strip_resume(args)
        notes.append(
            "that is a Cursor chat id, which Claude Code cannot resume: starting a fresh run"
        )
        text = _fresh_run_prompt(flags)
        if text:
            args = _with_prompt(args, text)
    # The account Claude Code is logged in as right now keeps its own login: no token override,
    # so it refreshes itself and keeps the scopes (and connectors) of a normal login.
    live = store.live_alias(svc.root, svc.port) == chosen.alias
    inherited = childenv.parent_carries_claude_account(svc.environ)
    if live and inherited:
        # This process runs inside a session pinned to an account by a token or a config dir.
        # "The live login" would then depend on what is dropped, so the account is set
        # explicitly instead.
        live = False
        notes.append(
            f"this shell carries a Claude token or config dir: {chosen.alias} runs on its own "
            "token, set explicitly, never on the inherited one"
        )
    elif live:
        notes.append(
            f"{chosen.alias} is Claude Code's own login: it runs as that, no token override "
            "(the inherited Claude variables are dropped)"
        )
    return Plan(
        chosen.alias,
        cursor.KIND_CLAUDE,
        (command[0], *args),
        tuple(notes),
        chosen,
        claude_token=not live,
    )
