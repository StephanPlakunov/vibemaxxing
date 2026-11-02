"""Argument parsing and the human renderer. Every command funnels its output
through redact.out / redact.err, so no path prints an unscrubbed byte."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from vibemaxxing import (
    __version__,
    auto,
    credentials,
    cursor,
    envelope,
    history,
    keychain,
    launch,
    oauth,
    route,
    statusline,
    store,
    tui,
    web,
)
from vibemaxxing.credentials import Credential
from vibemaxxing.cursor import AgentPort, CliAgent
from vibemaxxing.cursor_usage import AgentTokenStore, CursorTokenPort
from vibemaxxing.envelope import AccountView
from vibemaxxing.errors import (
    NeedsLoginError,
    NetworkError,
    NotFoundError,
    UsageError,
    VibeError,
)
from vibemaxxing.httpclient import CURSOR_HOSTS, HttpClient, UrllibClient
from vibemaxxing.keychain import KeychainPort, default_port
from vibemaxxing.models import AccountState
from vibemaxxing.pool import Policy, dry_in
from vibemaxxing.redact import err, install_excepthook, out
from vibemaxxing.web import LOOPBACK

DEFAULT_PORT: Final = 8787


@dataclass
class Context:
    root: Path
    client: HttpClient
    port: KeychainPort
    now_s: float
    # A long-lived surface needs a clock, not a snapshot: the dashboard reads this
    # every cycle. A one-shot command reads now_s and never calls it.
    clock: Callable[[], float] = time.time
    prompt: Callable[[str], str] = input
    browser: Callable[[str], bool] = webbrowser.open
    # What `vibe auto tick` tells the person after it switched. A field, not a call to
    # auto.notify_macos, so a test that drives a switch never raises a real notification.
    notify: Callable[[str], None] = auto.notify_macos
    # The store Claude Code keeps a second config dir's login in, for `vibe add
    # --from-config-dir`. Read only; a field so a test hands it a fake.
    port_for: Callable[[Path], KeychainPort] = keychain.config_dir_port
    argv0: str = field(default="vibe")
    # Cursor: the agent binary, the client that may talk to Cursor's API and nothing else,
    # and where the agent keeps its login token. Fields so a test hands it fakes.
    agent: AgentPort = field(default_factory=CliAgent)
    cursor_client: HttpClient = field(default_factory=lambda: UrllibClient(CURSOR_HOSTS))
    cursor_token: CursorTokenPort = field(default_factory=AgentTokenStore)

    def services(self) -> launch.Services:
        return launch.Services(
            root=self.root,
            client=self.client,
            port=self.port,
            agent=self.agent,
            cursor_client=self.cursor_client,
            cursor_token=self.cursor_token,
            now_s=self.now_s,
            environ=os.environ,
            cwd=Path.cwd(),
            home=Path.home(),
        )

    def cursor_readings(self, *, now_s: float | None = None) -> list[launch.CursorReading]:
        """Every Cursor account with its usage; [] without one, and no process is started."""
        aliases = cursor.list_cursor_aliases(self.root)
        if not aliases:
            return []
        try:
            margin = route.read_routing(self.root).cursor_margin_cents
        except UsageError:
            # A wrong routing stops `vibe run auto`; it must not stop `vibe list`.
            margin = route.Routing().cursor_margin_cents
        svc = self.services()
        if now_s is not None:
            svc = replace(svc, now_s=now_s)
        return [launch.read_cursor(svc, alias, margin_cents=margin) for alias in aliases]


def default_context() -> Context:
    return Context(
        root=store.store_root(),
        client=UrllibClient(),
        port=default_port(),
        now_s=time.time(),
    )


# --- rendering ---------------------------------------------------------------


def _render_accounts(views: Sequence[AccountView], *, now_s: float, dry: float | None) -> str:
    if not views:
        return "no accounts yet — run: vibe add\n"
    lines: list[str] = []
    for view in views:
        marker = "*" if view.active else " "
        who = view.identity.email or view.identity.display_name or "—"
        plan = view.plan or "—"
        lines.append(f"{marker} {view.alias}  {who}  {plan}")
        if view.message:
            lines.append(f"    {view.message}")
        summary = view.summary
        if summary is not None and not summary.rows:
            lines.append("    no limits reported")
        for row in summary.rows if summary else ():
            percent = "—" if row.percent is None else f"{row.percent}%"
            flag = "" if row.severity in (None, "normal") else f"  ({row.severity})"
            lines.append(f"    {row.label:<24}{percent:>5}{flag}")
        lines.append("")
    plural = "" if len(views) == 1 else "s"
    tail = f"pool  {envelope.pool_weeks(views)} account-weeks across {len(views)} account{plural}"
    if dry is not None:
        tail += f"  ·  dry in {dry / 3600:.1f}h"
    lines.append(tail)
    return "\n".join(lines) + "\n"


def _emit(payload: dict[str, object], *, as_json: bool, human: str) -> None:
    out(envelope.dumps(payload) + "\n" if as_json else human)


def _action(action: str, **fields: object) -> dict[str, object]:
    return {"schema": envelope.ENVELOPE_SCHEMA, "ok": True, "action": action, **fields}


# --- commands ----------------------------------------------------------------


def _slug(text: str) -> str:
    # Lower case and strip the leading punctuation store.valid_alias rejects, so
    # an email local part like "_Jan.S" cannot make a bare `vibe add` abort.
    kept = [c if (c.isalnum() or c in "._-") else "-" for c in text.lower()]
    slug = "".join(kept).strip("-._")
    return slug[:64] or "account"


def _taken(root: Path) -> set[str]:
    """Every alias in use: Claude accounts and Cursor ones share one namespace."""
    return {*store.list_aliases(root), *cursor.list_cursor_aliases(root), *cursor.RESERVED_ALIASES}


def _free_alias(root: Path, wanted: str) -> str:
    taken = _taken(root)
    if wanted not in taken:
        return wanted
    for n in range(2, 100):
        candidate = f"{wanted}-{n}"
        if candidate not in taken:
            return candidate
    raise UsageError(f'too many accounts named like "{wanted}"', "vibe list")


def _already_stored(root: Path, credential: Credential) -> str | None:
    wanted = credential.refresh_token
    for alias in store.list_aliases(root):
        try:
            if store.read_account(root, alias).credential.refresh_token == wanted:
                return alias
        except VibeError:
            continue  # a broken account file must not block an adopt
    return None


def _adopt(ctx: Context, as_json: bool) -> int:
    blob = ctx.port.read()
    if blob is None:
        raise NeedsLoginError(
            "Claude Code has no login on this machine to adopt",
            "claude /login",
        )
    credential = credentials.parse_blob(blob)
    existing = _already_stored(ctx.root, credential)
    if existing is not None:
        # Adopting twice would put one refresh token in two account files, and
        # the first rotation under either alias silently kills the other.
        _emit(
            _action("add", alias=existing, adopted=True),
            as_json=as_json,
            human=f'that login is already stored as "{existing}"\n',
        )
        return 0
    identity = credentials.read_claude_identity(Path.home() / ".claude.json")
    base = identity.email or identity.display_name or "account"
    alias = _free_alias(ctx.root, _slug(base.split("@", 1)[0]))
    store.write_account(
        ctx.root,
        store.Account(
            alias=alias,
            credential=credential,
            identity=identity,
            state=AccountState.OK,
            message=None,
            added_at=ctx.now_s,
        ),
    )
    store.write_active(ctx.root, alias)
    _emit(
        _action("add", alias=alias, adopted=True),
        as_json=as_json,
        human=f'adopted the login already in Claude Code as "{alias}"\n',
    )
    return 0


def _login(ctx: Context, alias: str, as_json: bool) -> int:
    if not store.valid_alias(alias):
        raise UsageError(
            f'"{alias}" is not a usable alias: letters, digits, dot, dash, underscore',
            "vibe add work",
        )
    _require_unused(ctx.root, alias)
    verifier, state = oauth.new_verifier(), oauth.new_state()
    url = oauth.build_authorize_url(verifier, state)
    # URL first, then the browser. On Linux, CPython registers text-mode console
    # browsers whenever TERM is set and GenericBrowser.open waits for the child,
    # so a headless login hands the terminal to lynx and blocks there -- with the
    # URL never printed, because printing came after the call.
    err(f"open this to log in:\n\n{url}\n\n")
    if sys.platform == "darwin" or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        ctx.browser(url)
    # The prompt goes to stderr: with --json, stdout must be the envelope alone.
    err("paste the code shown in the browser: ")
    paste = ctx.prompt("").strip()
    code, _ = oauth.parse_pasted_code(paste, state)
    credential, identity = oauth.exchange_code(
        ctx.client, code=code, verifier=verifier, state=state
    )
    # A stash from the previous lineage would be consumed as a successor by the
    # next refresh, destroying the credential this login just issued.
    store.delete_stash(ctx.root, alias)
    store.write_account(
        ctx.root,
        store.Account(
            alias=alias,
            credential=credential,
            identity=identity,
            state=AccountState.OK,
            message=None,
            added_at=ctx.now_s,
        ),
    )
    _emit(
        _action("add", alias=alias, adopted=False),
        as_json=as_json,
        human=f'added "{alias}" — run: vibe switch {alias}\n',
    )
    return 0


def _import(ctx: Context, wanted: str | None, given: Path, as_json: bool) -> int:
    """Take over the login Claude Code keeps for another config dir. No browser, no request.

    Claude Code started with CLAUDE_CONFIG_DIR=<dir> keeps its credential in an item of
    its own and its profile in <dir>/.claude.json. Nothing is refreshed and nothing is
    written but the account file: `active` stays, since this login is not the live one.
    """
    directory = keychain.resolve_config_dir(given)
    blob = ctx.port_for(directory).read()
    if blob is None:
        raise NeedsLoginError(
            f"Claude Code has no login for {directory} "
            f"(looked in {keychain.config_dir_location(directory)})",
            f"CLAUDE_CONFIG_DIR={directory} claude /login",
        )
    credential = credentials.parse_blob(blob)
    identity = credentials.read_claude_identity(directory / ".claude.json")
    if identity.account_uuid is None:
        # Stored without one, vibe could never tell this account from the live one, and
        # auto-switch never switches onto it (CONTRACT section 17).
        raise UsageError(
            f"{directory / '.claude.json'} names no account (oauthAccount.accountUuid)",
            f"CLAUDE_CONFIG_DIR={directory} claude",
        )
    existing = _already_stored(ctx.root, credential)
    if existing is not None:
        _emit(
            _action("add", alias=existing, adopted=True, config_dir=str(directory)),
            as_json=as_json,
            human=f'that login is already stored as "{existing}"\n',
        )
        return 0
    base = identity.email or identity.display_name or "account"
    alias = wanted if wanted is not None else _free_alias(ctx.root, _slug(base.split("@", 1)[0]))
    if not store.valid_alias(alias):
        raise UsageError(
            f'"{alias}" is not a usable alias: lower-case letters, digits, dot, dash, underscore',
            f"vibe add extra --from-config-dir {directory}",
        )
    _require_unused(ctx.root, alias)
    if store.read_active(ctx.root) == alias:
        # The port holds the active account's login; its file must keep matching it.
        raise UsageError(
            f'"{alias}" is the active account, and importing another login over it would '
            "leave its file and Claude Code's login disagreeing",
            f"vibe add <another name> --from-config-dir {directory}",
        )
    if alias in store.list_aliases(ctx.root):
        held = store.read_account(ctx.root, alias).identity.account_uuid
        if held != identity.account_uuid:
            # Its refresh token would be gone for good, with only a browser login to undo it.
            raise UsageError(f'"{alias}" already holds another account', f"vibe remove {alias}")
    # A stash from the previous lineage would be consumed as a successor by the next
    # refresh, destroying the credential this import just stored.
    store.delete_stash(ctx.root, alias)
    store.write_account(
        ctx.root,
        store.Account(
            alias=alias,
            credential=credential,
            identity=identity,
            state=AccountState.OK,
            message=None,
            added_at=ctx.now_s,
        ),
    )
    _emit(
        _action("add", alias=alias, adopted=True, config_dir=str(directory)),
        as_json=as_json,
        human=(
            f'imported the login in {directory} as "{alias}"\n'
            f"vibe now refreshes it; stop using that dir, or the two copies log each other out\n"
        ),
    )
    return 0


def _require_unused(root: Path, alias: str) -> None:
    """A Claude login may not take a Cursor account's alias, nor a reserved word."""
    if alias in cursor.RESERVED_ALIASES or cursor.is_cursor(root, alias):
        raise UsageError(f'"{alias}" is already used (or reserved)', "vibe list")


def _add_cursor(ctx: Context, alias: str | None, as_json: bool) -> int:
    """Register a Cursor seat. vibe never sees its credentials: `cursor-agent login` keeps them."""
    wanted = alias if alias is not None else "cursor"
    if not store.valid_alias(wanted):
        raise UsageError(
            f'"{wanted}" is not a usable alias: lower-case letters, digits, dot, dash, underscore',
            "vibe add cursor --kind cursor",
        )
    if wanted in cursor.RESERVED_ALIASES or wanted in store.list_aliases(ctx.root):
        raise UsageError(f'"{wanted}" is already used (or reserved)', "vibe list")
    ctx.agent.binary()  # not installed: refused before anything is written
    status = ctx.agent.status()
    cursor.write_marker(ctx.root, wanted, added_at=ctx.now_s)
    if status.logged_in:
        who = f"logged in as {status.email or 'an unknown user'}"
        if status.team:
            who += f", team {status.team}"
        human = f'added Cursor account "{wanted}" ({who})\n'
    else:
        human = (
            f'added Cursor account "{wanted}", but cursor-agent is not logged in: '
            f"run: {cursor.AGENT_LOGIN}\n"
        )
    _emit(
        _action("add", alias=wanted, kind=cursor.KIND_CURSOR, logged_in=status.logged_in),
        as_json=as_json,
        human=human,
    )
    return 0


def cmd_add(args: argparse.Namespace, ctx: Context) -> int:
    if args.kind == cursor.KIND_CURSOR:
        if args.from_config_dir is not None:
            raise UsageError("--from-config-dir is for Claude accounts", "vibe add --help")
        return _add_cursor(ctx, args.alias, args.json)
    if args.from_config_dir is not None:
        return _import(ctx, args.alias, args.from_config_dir, args.json)
    if args.alias is None:
        return _adopt(ctx, args.json)
    return _login(ctx, args.alias, args.json)


def _collect_and_record(ctx: Context) -> tuple[list[AccountView], float | None]:
    views = envelope.collect(ctx.root, client=ctx.client, port=ctx.port, now_s=ctx.now_s)
    auto.publish(ctx.root, port=ctx.port, now_s=ctx.now_s)
    with closing(history.connect(store.history_path(ctx.root))) as conn:
        history.record(conn, at_s=ctx.now_s, pool=envelope.pool_weeks(views))
        window = history.samples(conn, since_s=ctx.now_s - history.RETENTION_DAYS * 86_400.0)
    forecast = dry_in(window)
    return views, None if forecast is None else forecast.total_seconds()


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _render_cursor(reading: launch.CursorReading) -> list[str]:
    """One Cursor seat: who, the two numbers Cursor bills by, and whether routing may use it."""
    who = reading.email or "—"
    lines = [f"  {reading.alias}  {who}  Cursor{f' · team {reading.team}' if reading.team else ''}"]
    if not reading.logged_in:
        lines.append(f"    not logged in — run: {cursor.AGENT_LOGIN}")
        return lines
    usage = reading.usage
    if usage is None:
        lines.append(f"    usage: unknown ({reading.note}); see cursor.com/dashboard")
        return lines
    if reading.note:
        lines.append(f"    {reading.note}")
    if usage.included_percent is not None:
        lines.append(f"    {'Included + bonus':<24}{round(usage.included_percent):>4}%")
    used, cap = usage.on_demand_used_cents, usage.on_demand_limit_cents
    if used is not None:
        of = f" of {_money(cap)}" if cap else " (off)" if cap == 0 else ""
        lines.append(f"    {'On-demand':<24}{_money(used)}{of}  billed to the company")
    if usage.cycle_end_s is not None:
        end = datetime.fromtimestamp(usage.cycle_end_s).strftime("%a %d %b %H:%M")
        lines.append(f"    resets {end}")
    verdict = {True: "available", False: "spent", None: "unknown"}[reading.availability.ok]
    lines.append(f"    routing: {verdict} ({reading.availability.why})")
    return lines


def cmd_list(args: argparse.Namespace, ctx: Context) -> int:
    views, dry = _collect_and_record(ctx)
    readings = ctx.cursor_readings()
    human = _render_accounts(views, now_s=ctx.now_s, dry=dry)
    if readings:
        shown = [line for reading in readings for line in (*_render_cursor(reading), "")]
        if views:
            head, _, tail = human.rpartition("\n\npool")
            human = head + "\n\n" + "\n".join(shown) + "\npool" + tail
        else:
            human = "\n".join(shown) + "\n"
    _emit(
        envelope.build(
            views,
            now_s=ctx.now_s,
            dry_in=None if dry is None else timedelta(seconds=dry),
            auto=auto.read_settings(ctx.root),
            last_switch=auto.read_last(ctx.root),
            cursor=[launch.cursor_entry(reading) for reading in readings],
        ),
        as_json=args.json,
        human=human,
    )
    return 0


def check_loopback(host: str) -> None:
    if host != LOOPBACK:
        raise UsageError(
            f"--host {host} is refused: the dashboard binds loopback ({LOOPBACK}) only, "
            "because it has no authentication and no TLS",
            f"vibe usage web --host {LOOPBACK}",
        )


def cmd_dashboard(args: argparse.Namespace, ctx: Context) -> int:
    if not args.json and not sys.stdin.isatty():
        # Textual would take the alt screen and wait forever for input that is
        # never coming: ssh without -t, cron, a pipeline, a CI step.
        raise UsageError(
            "the dashboard needs a terminal, and stdin is not one",
            "vibe list",
        )
    # Bare `vibe` opens the dashboard; `vibe --json` stays machine-readable.
    return cmd_list(args, ctx) if args.json else tui.run(ctx)


def cmd_usage(args: argparse.Namespace, ctx: Context) -> int:
    if args.mode == "web":
        if args.once:
            raise UsageError(
                "vibe usage web serves continuously; --once fetches and exits",
                "vibe usage --once",
            )
        check_loopback(args.host)
        return web.serve(ctx, port=args.port)
    return cmd_list(args, ctx)


def cmd_switch(args: argparse.Namespace, ctx: Context) -> int:
    if cursor.is_cursor(ctx.root, args.alias):
        raise UsageError(
            f'"{args.alias}" is a Cursor account: it has no Claude Code login to switch to',
            f"vibe run {args.alias} -- claude",
        )
    oauth.ensure_usable(ctx.root, args.alias, ctx.client, now_ms=int(ctx.now_s * 1000))
    store.switch(ctx.root, args.alias, ctx.port, now_s=ctx.now_s)
    _emit(
        _action("switch", alias=args.alias),
        as_json=args.json,
        human=f'Claude Code now uses "{args.alias}"\n',
    )
    return 0


def _toggle_auto(args: argparse.Namespace, ctx: Context, *, on: bool) -> int:
    settings = replace(auto.settings_for_edit(ctx.root), auto_switch=on)
    auto.write_settings(ctx.root, settings)
    word = "on" if on else "off"
    _emit(
        _action(f"auto {word}", enabled=on, threshold=settings.threshold),
        as_json=args.json,
        human=f"auto-switch is {word} (threshold {settings.threshold}%)\n",
    )
    return 0


def cmd_auto_on(args: argparse.Namespace, ctx: Context) -> int:
    return _toggle_auto(args, ctx, on=True)


def cmd_auto_off(args: argparse.Namespace, ctx: Context) -> int:
    return _toggle_auto(args, ctx, on=False)


def cmd_auto_exclude(args: argparse.Namespace, ctx: Context) -> int:
    if args.alias not in store.list_aliases(ctx.root):
        raise NotFoundError(f'there is no account named "{args.alias}"', "vibe list")
    settings = auto.settings_for_edit(ctx.root)
    excluded = tuple(dict.fromkeys((*settings.excluded, args.alias)))
    auto.write_settings(ctx.root, replace(settings, excluded=excluded))
    _emit(
        _action("auto exclude", alias=args.alias, excluded=list(excluded)),
        as_json=args.json,
        human=f'auto-switch will not switch to "{args.alias}"\n',
    )
    return 0


def cmd_auto_include(args: argparse.Namespace, ctx: Context) -> int:
    settings = auto.settings_for_edit(ctx.root)
    excluded = tuple(alias for alias in settings.excluded if alias != args.alias)
    auto.write_settings(ctx.root, replace(settings, excluded=excluded))
    _emit(
        _action("auto include", alias=args.alias, excluded=list(excluded)),
        as_json=args.json,
        human=f'auto-switch may switch to "{args.alias}" again\n',
    )
    return 0


def _render_policy(policy: Policy) -> str:
    steps = " -> ".join(
        f"{step.account} until {step.until}%"
        + (
            f" (weekly cap Mon-Sun {'/'.join(map(str, step.weekly_cap))}%)"
            if step.weekly_cap
            else ""
        )
        for step in policy.steps
    )
    text = f"ordered: {steps}, then earliest reset"
    if policy.block_models:
        text += f"; {', '.join(policy.block_models)} weekly rows count too"
    return text


def _render_step(policy: Policy, index: int | None) -> str:
    if index is None:
        return "none has room - earliest reset"
    step = policy.steps[index]
    return f"{index + 1} of {len(policy.steps)}: {step.account} until {step.until}%"


def _render_status(status: auto.Status) -> str:
    settings = status.settings
    policy = settings.policy
    live = status.live or "unknown"
    if status.live is not None and status.active != status.live:
        live += f"  (vibe's active account is {status.active or 'unset'}"
        live += f" - run: vibe switch {status.live})"
    last = "none"
    if status.last is not None:
        when = datetime.fromtimestamp(status.last.at_s).strftime("%Y-%m-%d %H:%M")
        last = (
            f"{status.last.from_alias} -> {status.last.to_alias} at {when}  ({status.last.reason})"
        )
    if not policy.steps:
        return (
            f"auto-switch  {'on' if settings.auto_switch else 'off'}\n"
            f"threshold    {settings.threshold}%  cooldown {settings.cooldown_s}s\n"
            f"excluded     {', '.join(settings.excluded) or 'none'}\n"
            f"live         {live}\n"
            f"last switch  {last}\n"
            f"next         {status.next_alias or 'none'}\n"
        )
    # Collected only while the pointer agrees with the live account; otherwise unknown.
    known = status.live is not None and status.live == status.active
    return (
        f"auto-switch  {'on' if settings.auto_switch else 'off'}\n"
        f"policy       {_render_policy(policy)}\n"
        f"cooldown     {settings.cooldown_s}s\n"
        f"excluded     {', '.join(settings.excluded) or 'none'}\n"
        f"live         {live}\n"
        f"step         {_render_step(policy, status.step) if known else 'unknown'}\n"
        f"last switch  {last}\n"
        f"next step    {_render_step(policy, status.next_step) if known else 'unknown'}\n"
    )


def cmd_auto_status(args: argparse.Namespace, ctx: Context) -> int:
    status = auto.status(ctx.root, client=ctx.client, port=ctx.port, now_s=ctx.now_s)
    _emit(
        _action(
            "auto status",
            enabled=status.settings.auto_switch,
            threshold=status.settings.threshold,
            cooldown_s=status.settings.cooldown_s,
            excluded=list(status.settings.excluded),
            live=status.live,
            active=status.active,
            last_switch=envelope.last_switch_entry(status.last),
            next=status.next_alias,
            policy=auto.policy_document(status.settings.policy),
            step=None if status.step is None else status.step + 1,
            next_step=None if status.next_step is None else status.next_step + 1,
        ),
        as_json=args.json,
        human=_render_status(status),
    )
    return 0


def cmd_auto_tick(args: argparse.Namespace, ctx: Context) -> int:
    result = auto.tick(
        ctx.root, client=ctx.client, port=ctx.port, now_s=ctx.now_s, notify=ctx.notify
    )
    _emit(
        _action("auto tick", switched=result.switched, line=result.line),
        as_json=args.json,
        human=result.line + "\n",
    )
    return 0


def cmd_auto_undo(args: argparse.Namespace, ctx: Context) -> int:
    record = auto.undo(ctx.root, client=ctx.client, port=ctx.port, now_s=ctx.now_s)
    _emit(
        _action("auto undo", alias=record.to_alias),
        as_json=args.json,
        human=f'Claude Code is back on "{record.to_alias}"\n',
    )
    return 0


def cmd_auto_install(args: argparse.Namespace, ctx: Context) -> int:
    path = auto.install_plist()
    command = auto.bootstrap_command(path)
    _emit(
        _action("auto install", plist=str(path), load=command),
        as_json=args.json,
        human=f"wrote {path}\nit is not loaded yet; to load it, run:\n  {command}\n",
    )
    return 0


def cmd_auto_uninstall(args: argparse.Namespace, ctx: Context) -> int:
    removed = auto.uninstall_plist()
    command = auto.bootout_command()
    _emit(
        _action("auto uninstall", removed=removed, unload=command),
        as_json=args.json,
        human=(
            (f"removed {auto.plist_path()}\n" if removed else "there was no job file to remove\n")
            + f"if the job is loaded, unload it with:\n  {command}\n"
        ),
    )
    return 0


def cmd_statusline(args: argparse.Namespace, ctx: Context) -> int:
    # The console script answers this before it imports cli (entry.py); this is the same
    # line for `python -m vibemaxxing statusline` and the tests.
    out(statusline.render(statusline.read(ctx.root), now_s=ctx.now_s) + "\n")
    return 0


def cmd_remove(args: argparse.Namespace, ctx: Context) -> int:
    if cursor.is_cursor(ctx.root, args.alias):
        cursor.delete_marker(ctx.root, args.alias)  # the Cursor login itself is cursor-agent's
    else:
        store.delete_account(ctx.root, args.alias)
    _emit(
        _action("remove", alias=args.alias),
        as_json=args.json,
        human=f'removed "{args.alias}"\n',
    )
    return 0


def cmd_alias(args: argparse.Namespace, ctx: Context) -> int:
    if cursor.is_cursor(ctx.root, args.old):
        _require_unused(ctx.root, args.new)
        if args.new in store.list_aliases(ctx.root):
            raise UsageError(f'there is already an account named "{args.new}"', "vibe list")
        cursor.rename_marker(ctx.root, args.old, args.new)
    else:
        _require_unused(ctx.root, args.new)
        store.rename_account(ctx.root, args.old, args.new)
        auto.rename(ctx.root, args.old, args.new)
    _emit(
        _action("alias", old=args.old, new=args.new),
        as_json=args.json,
        human=f'"{args.old}" is now "{args.new}"\n',
    )
    return 0


def _fresh_token(ctx: Context, alias: str) -> Credential:
    account = store.read_account(ctx.root, alias)
    now_ms = int(ctx.now_s * 1000)
    if store.read_active(ctx.root) == alias:
        # Claude Code refreshes this lineage; refreshing it here too logs it out.
        # With the refresh buffer: the child cannot refresh, so a token with
        # seconds left would fail inside it.
        token, problem = envelope.active_token(
            ctx.root, account, ctx.port, now_ms=now_ms, buffer_ms=credentials.EXPIRY_BUFFER_MS
        )
        if token is None:
            raise VibeError(problem, "claude")
        return token
    if credentials.login_lapsed(account.credential, now_ms=now_ms):
        raise NeedsLoginError(f'the login for "{alias}" has lapsed', f"vibe add {alias}")
    if not credentials.is_expired(account.credential, now_ms=now_ms):
        return account.credential
    outcome = oauth.refresh(ctx.root, alias, account.credential, ctx.client, now_ms=now_ms)
    if outcome.credential is not None:
        return outcome.credential
    if outcome.error == "active":
        raise UsageError(f'"{alias}" became the active account', f"vibe run {alias} -- ...")
    if outcome.error in ("invalid_grant", "no_refresh_token"):
        raise NeedsLoginError(f'the refresh token for "{alias}" is dead', f"vibe add {alias}")
    # transient, busy, invalid_client: the login is fine, the attempt was not, so
    # sending the user to a browser login here would be wrong advice.
    raise NetworkError(
        f'could not refresh "{alias}" right now ({outcome.error})',
        f"vibe run {alias} -- ...",
    )


def _run_flags(args: argparse.Namespace) -> launch.RunFlags:
    needs: tuple[str, ...] | None = None
    if args.needs is not None:
        needs = tuple(name.strip() for name in args.needs.split(",") if name.strip())
        if not needs:
            raise UsageError(
                "--needs wants connector names, e.g. slack,atlassian", "vibe run --help"
            )
    return launch.RunFlags(
        needs=needs,
        no_needs=args.no_needs,
        claude_only=args.claude_only,
        allow_force=args.allow_force,
        allow_fast=args.allow_fast,
        allow_no_zdr=args.allow_no_zdr,
        brief=args.brief,
        handover=args.handover,
    )


def _say(text: str) -> None:
    err(f"{launch.VIBE_LINE}{text}\n")


def _spawn(argv: Sequence[str], env: dict[str, str] | None) -> int:
    try:
        return subprocess.run(list(argv), env=env, check=False).returncode
    except KeyboardInterrupt:
        # The child got the same SIGINT and is already shutting down. Report
        # the conventional 130 rather than a traceback from the wrapper.
        return 130
    except OSError as exc:
        raise UsageError(
            f"could not start {argv[0]}: {exc.__class__.__name__}", "vibe list"
        ) from None


def cmd_run(args: argparse.Namespace, ctx: Context) -> int:
    # Only the leading separator is ours; a `--` the child itself needs must
    # survive into its argv.
    command = args.command[1:] if args.command[:1] == ["--"] else list(args.command)
    if not command:
        raise UsageError("vibe run needs a command after --", "vibe run work -- claude")
    plan = launch.plan_run(ctx.services(), args.alias, command, _run_flags(args))
    for note in plan.notes:
        _say(note)
    if args.dry_run:
        _emit(
            _action(
                "run",
                alias=plan.alias,
                kind=plan.kind,
                argv=list(plan.argv),
                dry_run=True,
                notes=list(plan.notes),
            ),
            as_json=args.json,
            human=f"would run on {plan.alias} ({plan.kind}): {' '.join(plan.argv)}\n",
        )
        return 0
    if plan.kind == cursor.KIND_CURSOR:
        # No credential of ours goes anywhere: cursor-agent reads its own login.
        code = _spawn(plan.argv, None)
    elif not plan.claude_token:
        code = _spawn(plan.argv, None)  # `claude --version` and friends: no account involved
    else:
        credential = _fresh_token(ctx, plan.alias)
        # The fifth permitted reveal() site: the child's environment. The
        # global Keychain credential is untouched, so other shells keep their account.
        child_env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": credential.access_token.reveal()}
        code = _spawn(plan.argv, child_env)
    if args.json and code != 130:
        out(envelope.dumps(_action("run", alias=plan.alias, kind=plan.kind, exit_code=code)) + "\n")
    return code


SHELL_INIT: Final = """# vibe: route Claude Code sessions to the account with room (add to ~/.zshrc)
claude() { vibe run auto -- claude "$@"; }
# a short name for code-only sessions: no Slack/Atlassian needed, so extra comes first
claude-code() { vibe run auto --no-needs -- claude "$@"; }
"""


def cmd_shell_init(args: argparse.Namespace, ctx: Context) -> int:
    out(SHELL_INIT)
    return 0


# --- parser ------------------------------------------------------------------

HELP: Final = """vibe — several Claude Code accounts, one pooled view of the plan limits.

Getting started
  vibe add                    adopt the login Claude Code already has here (no browser)
  vibe add work               log in to another account; opens a browser, you paste a code
  vibe add work --from-config-dir ~/.claude-work
                              import the login Claude Code keeps for that config dir
  vibe switch work            point Claude Code at that account, then run `claude` normally

Every day
  vibe                        the dashboard, in the terminal
  vibe list                   each account's limits and the pooled headroom
  vibe run work -- claude     one command on one account; the active login is untouched
  vibe usage web              the same dashboard in a browser, http://127.0.0.1:8787

Status line
  vibe statusline             one line: who is live and each account's limits, from the
                              numbers the last command or tick saw (no request); for
                              Claude Code's statusLine setting

Housekeeping
  vibe alias old new          rename an account
  vibe remove work            forget an account (the login itself is not revoked)
  vibe usage --once           fetch the numbers once and print them
  vibe --version              the installed version

Automatic switching (off until you turn it on)
  vibe auto on                leave the live account for the one with the most room once its
                              session or week reaches 99%; `off` stops it
  vibe auto status            the settings, who is live, the last switch, where one would go
  vibe auto tick              check once and switch if it is due; a scheduler runs this
  vibe auto install           macOS: write the launchd job that does that every minute, and
                              print the one command that loads it
  vibe auto exclude work      never switch to that account; `include` allows it again
  vibe auto undo              go back to the account the last automatic switch left

switch or run?
  `switch` is global and lasts: every shell, until you switch again. It swaps the
  credential Claude Code itself reads, so a session started afterwards is on the new
  account -- one already running is not.
  `run` is one command only, on a token handed to that child process alone. Use it to
  borrow headroom from another account without disturbing what you are logged in as.

Good to know
  Limits are per account. `vibe list` shows session, weekly-all-models and weekly-Fable,
  plus how many account-weeks the pool has left and roughly when it runs dry.
  Accounts share one ~/.claude history and one MCP config -- only the credential is
  swapped, exactly as if you had logged out and in by hand.
  `--json` works on every command and emits the envelope documented in docs/CONTRACT.md.
  The web dashboard refetches at most every 3 minutes; `vibe list` always fetches now."""


def _add_json(parser: argparse.ArgumentParser, *, root: bool = False) -> None:
    # Only the root parser carries a default. A subparser default would overwrite
    # `vibe --json list` back to False after the root had already set it, so the
    # caller asked for JSON and silently got human text.
    parser.add_argument(
        "--json",
        action="store_true",
        default=False if root else argparse.SUPPRESS,
        help="emit the machine-readable envelope",
    )


def cmd_help(args: argparse.Namespace, ctx: Context) -> int:
    out(HELP + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibe",
        description="Manage several Claude Code accounts and see pooled plan usage.",
        epilog=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=__version__)
    _add_json(parser, root=True)
    parser.set_defaults(handler=cmd_dashboard, alias=None, mode=None)
    subs = parser.add_subparsers(dest="command_name")

    add = subs.add_parser("add", help="adopt the current Claude Code login, or log in fresh")
    add.add_argument("alias", nargs="?", help="omit to adopt the login already in Claude Code")
    add.add_argument(
        "--from-config-dir",
        type=Path,
        metavar="DIR",
        help="import the login Claude Code keeps for CLAUDE_CONFIG_DIR=DIR (no browser)",
    )
    add.add_argument(
        "--kind",
        choices=cursor.KINDS,
        default=cursor.KIND_CLAUDE,
        help="claude (default) or cursor: a Cursor seat, logged in with `cursor-agent login`",
    )
    _add_json(add)
    add.set_defaults(handler=cmd_add)

    listing = subs.add_parser("list", help="show every account and the pooled headroom")
    _add_json(listing)
    listing.set_defaults(handler=cmd_list)

    switch = subs.add_parser("switch", help="make one account the active Claude Code login")
    switch.add_argument("alias")
    _add_json(switch)
    switch.set_defaults(handler=cmd_switch)

    run = subs.add_parser(
        "run", help="run one command pinned to one account (`auto` picks it, `cursor` is Cursor)"
    )
    run.add_argument("alias", help="an account, or `auto` to pick by the routing rules")
    run.add_argument(
        "--needs", metavar="LIST", help="connectors the run needs: slack,atlassian,m365,gcal"
    )
    run.add_argument("--no-needs", action="store_true", help="the run needs no connector")
    run.add_argument(
        "--claude-only", action="store_true", help="never translate this run to Cursor"
    )
    run.add_argument(
        "--allow-force", action="store_true", help="on Cursor: let bypassPermissions mean --force"
    )
    run.add_argument("--allow-fast", action="store_true", help='on Cursor: allow "-fast" models')
    run.add_argument(
        "--allow-no-zdr", action="store_true", help='on Cursor: allow "(NO ZDR)" models'
    )
    run.add_argument(
        "--brief", metavar="TEXT", help="the original task, for a fresh run after a move"
    )
    run.add_argument(
        "--handover", type=Path, metavar="FILE", help="notes the fresh run reads first after a move"
    )
    run.add_argument("--dry-run", action="store_true", help="print what would run; run nothing")
    # The command is cut off before parsing (split_run_command); nothing is left for argparse.
    _add_json(run)
    run.set_defaults(handler=cmd_run, command=[])

    init = subs.add_parser(
        "shell-init", help="print a zsh snippet that routes `claude` through `vibe run auto`"
    )
    init.set_defaults(handler=cmd_shell_init)

    remove = subs.add_parser("remove", help="forget an account")
    remove.add_argument("alias")
    _add_json(remove)
    remove.set_defaults(handler=cmd_remove)

    rename = subs.add_parser("alias", help="rename an account")
    rename.add_argument("old")
    rename.add_argument("new")
    _add_json(rename)
    rename.set_defaults(handler=cmd_alias)

    use = subs.add_parser("usage", help="fetch usage once, or serve the web dashboard")
    use.add_argument(
        "mode", nargs="?", choices=["web"], help="omit to fetch once; 'web' serves the dashboard"
    )
    use.add_argument(
        "--once", action="store_true", help="fetch once and print (the default without 'web')"
    )
    use.add_argument("--host", default=LOOPBACK)
    use.add_argument("--port", type=int, default=DEFAULT_PORT)
    _add_json(use)
    use.set_defaults(handler=cmd_usage)

    automatic = subs.add_parser(
        "auto", help="switch accounts by itself when the live one is nearly spent"
    )
    auto_subs = automatic.add_subparsers(
        dest="auto_command",
        required=True,
        metavar="{on,off,status,exclude,include,undo,tick,install,uninstall}",
    )
    for name, handler, summary in (
        ("on", cmd_auto_on, "turn automatic switching on"),
        ("off", cmd_auto_off, "turn automatic switching off"),
        ("status", cmd_auto_status, "show the settings, the live account and where a switch goes"),
        ("undo", cmd_auto_undo, "go back to the account the last automatic switch left"),
        ("tick", cmd_auto_tick, "check once and switch if it is due (what a scheduler runs)"),
        ("install", cmd_auto_install, "macOS: write the launchd job that runs a tick a minute"),
        ("uninstall", cmd_auto_uninstall, "macOS: remove that job's file"),
    ):
        auto_sub = auto_subs.add_parser(name, help=summary)
        _add_json(auto_sub)
        auto_sub.set_defaults(handler=handler)
    for name, handler, summary in (
        ("exclude", cmd_auto_exclude, "never switch automatically to this account"),
        ("include", cmd_auto_include, "allow switching to this account again"),
    ):
        auto_sub = auto_subs.add_parser(name, help=summary)
        auto_sub.add_argument("alias")
        _add_json(auto_sub)
        auto_sub.set_defaults(handler=handler)

    line = subs.add_parser(
        "statusline", help="one line for Claude Code's status line, from the last numbers"
    )
    line.set_defaults(handler=cmd_statusline)

    # `vibe help` is what people type; without it argparse answers an unhelpful
    # "invalid choice: 'help'" and lists the commands it just refused to explain.
    helping = subs.add_parser("help", help="what each command is for, and when to use it")
    _add_json(helping)
    helping.set_defaults(handler=cmd_help)

    return parser


_RUN_VALUE_FLAGS: Final = ("--needs", "--brief", "--handover")


def split_run_command(argv: Sequence[str]) -> tuple[list[str], list[str] | None]:
    """Cut `vibe run <alias> [vibe options] -- <command>` into vibe's part and the child's.

    argparse's REMAINDER would swallow the vibe options written between the alias and the `--`
    as part of the command, so `run` is cut here instead. Everything after the first `--` is the
    child's, verbatim. Without a `--`, the command starts after the alias (the old form
    `vibe run work claude`). None when this is not a `run`.
    """
    items = list(argv)
    first = next((index for index, token in enumerate(items) if not token.startswith("-")), None)
    if first is None or items[first] != "run":
        return items, None
    if "--" in items[first:]:
        cut = items.index("--", first)
        return items[:cut], items[cut + 1 :]
    index = first + 1
    while index < len(items) and items[index].startswith("-"):
        index += 2 if items[index] in _RUN_VALUE_FLAGS else 1
    end = min(index + 1, len(items))  # the alias, if there is one
    return items[:end], items[end:]


def main(argv: Sequence[str] | None = None, *, context: Context | None = None) -> int:
    install_excepthook()
    head, command = split_run_command(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(head)
    if command is not None:
        args.command = ["--", *command]  # cmd_run strips the one separator that is ours
    try:
        ctx = context if context is not None else default_context()
        handler: Callable[[argparse.Namespace, Context], int] = args.handler
        return handler(args, ctx)
    except VibeError as exc:
        if args.json:
            out(envelope.dumps(envelope.error_payload(exc)) + "\n")
        else:
            err(exc.render() + "\n")
        return exc.exit_code
