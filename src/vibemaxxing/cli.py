"""Argument parsing and the human renderer. Every command funnels its output
through redact.out / redact.err, so no path prints an unscrubbed byte."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from vibemaxxing import (
    __version__,
    adopt,
    antigravity,
    auto,
    bridge,
    broker,
    childenv,
    codex,
    credentials,
    cursor,
    cursor_translate,
    display,
    envelope,
    history,
    keychain,
    launch,
    mcp_oauth,
    oauth,
    release,
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
    UsageError,
    VibeError,
)
from vibemaxxing.httpclient import (
    BRIDGE_HOSTS,
    CODEX_HOSTS,
    CURSOR_HOSTS,
    HttpClient,
    UrllibClient,
)
from vibemaxxing.keychain import KeychainPort, default_port
from vibemaxxing.models import AccountState
from vibemaxxing.pool import dry_in
from vibemaxxing.redact import Secret, err, install_excepthook, out
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
    # The store Claude Code keeps a second config dir's login in, for `vibe add
    # --from-config-dir`. Read only; a field so a test hands it a fake.
    port_for: Callable[[Path], KeychainPort] = keychain.config_dir_port
    # The user's processes with their environment, for `vibe release`: a session running in a
    # config dir holds that dir's login. A field so a test hands it a fake listing.
    processes: release.ProcessPort = field(default_factory=release.PsProcesses)
    argv0: str = field(default="vibe")
    # Cursor: the agent binary, the client that may talk to Cursor's API and nothing else,
    # and where the agent keeps its login token. Fields so a test hands it fakes.
    agent: AgentPort = field(default_factory=CliAgent)
    cursor_client: HttpClient = field(default_factory=lambda: UrllibClient(CURSOR_HOSTS))
    cursor_token: CursorTokenPort = field(default_factory=AgentTokenStore)
    # Codex: the client that may reach ChatGPT's usage endpoint only, and the Codex CLI's login
    # file (read only). Fields so a test hands it fakes.
    codex_client: HttpClient = field(default_factory=lambda: UrllibClient(CODEX_HOSTS))
    codex_auth: codex.CodexAuthPort = field(default_factory=codex.FileAuth)
    antigravity_port: antigravity.UsagePort = field(default_factory=antigravity.CliUsage)
    # The bridge: the client that may reach only the upstreams' hosts, the Keychain for its tokens
    # (created on first use), a hidden prompt, and the one-shot listener for the browser redirect.
    bridge_client: HttpClient = field(default_factory=lambda: UrllibClient(BRIDGE_HOSTS))
    secrets: mcp_oauth.SecretStore = field(default_factory=mcp_oauth.LazySecrets)
    secret_prompt: Callable[[str], str] = getpass.getpass
    receiver: Callable[[int], mcp_oauth.CodeReceiver] = mcp_oauth.LoopbackReceiver

    def services(self) -> launch.Services:
        return launch.Services(
            root=self.root,
            client=self.client,
            port=self.port,
            agent=self.agent,
            cursor_client=self.cursor_client,
            cursor_token=self.cursor_token,
            codex_client=self.codex_client,
            codex_auth=self.codex_auth,
            now_s=self.now_s,
            environ=os.environ,
            cwd=Path.cwd(),
            home=Path.home(),
            port_for=self.port_for,
        )

    def cursor_readings(self, *, now_s: float | None = None) -> list[launch.CursorReading]:
        """Every Cursor account with its usage; [] without one, and no process is started."""
        aliases = cursor.list_cursor_aliases(self.root)
        if not aliases:
            return []
        svc = self.services()
        if now_s is not None:
            svc = replace(svc, now_s=now_s)
        return [launch.read_cursor(svc, alias) for alias in aliases]

    def codex_readings(self, *, now_s: float | None = None) -> list[launch.CodexReading]:
        """Every Codex account with its plan usage; [] without one. Reads files, never a process."""
        aliases = codex.list_codex_aliases(self.root)
        if not aliases:
            return []
        svc = self.services()
        if now_s is not None:
            svc = replace(svc, now_s=now_s)
        readings: list[launch.CodexReading] = []
        for alias in aliases:
            try:
                readings.append(launch.read_codex(svc, alias))
            except VibeError as exc:
                # A damaged marker must not stop `vibe list`; the row says what is wrong.
                readings.append(
                    launch.CodexReading(alias, "unknown", None, None, None, exc.message)
                )
        return readings

    def refresh_cursor_cache(self, *, min_age_s: float = 180.0) -> None:
        """Re-read each Cursor seat's usage into the cache once it is older than ``min_age_s``.

        For the background tick, so `vibe statusline` (which only reads cached numbers) stays within
        a few minutes of the truth without ever making a request itself. Never raises: a failed read
        leaves the old numbers, which the status line marks with a "?" once they are old.
        """
        aliases = cursor.list_cursor_aliases(self.root)
        if not aliases:
            return
        svc = self.services()
        for alias in aliases:
            cached = store.read_usage_cache(self.root, alias)
            if (
                cached is not None
                and cached.fetched_at is not None
                and 0 <= self.now_s - cached.fetched_at < min_age_s
            ):
                continue
            try:
                launch.read_cursor(svc, alias, fresh=True)
            except (VibeError, OSError):
                continue

    def antigravity_entries(self, *, now_s: float | None = None) -> list[antigravity.Entry]:
        return antigravity.entries(
            self.root, now_s=self.now_s if now_s is None else now_s, port=self.antigravity_port
        )


def default_context() -> Context:
    return Context(
        root=store.store_root(),
        client=UrllibClient(),
        port=default_port(),
        now_s=time.time(),
    )


# --- rendering ---------------------------------------------------------------


def _render_accounts(
    views: Sequence[AccountView], *, now_s: float, dry: float | None, colour: bool = False
) -> str:
    if not views:
        return "no accounts yet — run: vibe add\n"
    lines: list[str] = []
    for view in views:
        marker = "*" if view.active else " "
        who = view.identity.email or view.identity.display_name or "—"
        plan = view.plan or "—"
        summary = view.summary
        lines += display.block(
            f"{marker} {view.alias}  {who}  {plan}",
            [view.message] if view.message else [],
            display.claude_limits(view),
            now_s=now_s,
            colour=colour,
            empty="no limits reported" if summary is not None else None,
        )
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
    return {
        *store.list_aliases(root),
        *cursor.list_cursor_aliases(root),
        *codex.list_codex_aliases(root),
        *antigravity.aliases(root),
        *cursor.RESERVED_ALIASES,
    }


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
        # Stored without one, vibe could never tell this account from the live one
        # (CONTRACT section 17).
        raise UsageError(
            f"{directory / '.claude.json'} names no account (oauthAccount.accountUuid)",
            f"CLAUDE_CONFIG_DIR={directory} claude",
        )
    existing = _already_stored(ctx.root, credential)
    if existing is not None:
        stored = store.read_account(ctx.root, existing)
        if stored.config_dir is None and store.read_active(ctx.root) != existing:
            # Imported before accounts knew their config dir: vibe still refreshes this very
            # lineage, and so does Claude Code in that dir. From here only Claude Code does.
            store.delete_stash(ctx.root, existing)
            store.write_account(ctx.root, replace(stored, config_dir=str(directory)))
            human = (
                f'"{existing}" is now the login in {directory}: Claude Code refreshes it there '
                "and vibe never does\n"
            )
        else:
            human = f'that login is already stored as "{existing}"\n'
        _emit(
            _action("add", alias=existing, adopted=True, config_dir=str(directory)),
            as_json=as_json,
            human=human,
        )
        return 0
    base = identity.email or identity.display_name or "account"
    alias = wanted if wanted is not None else _free_alias(ctx.root, _slug(base.split("@", 1)[0]))
    if not store.valid_alias(alias):
        raise UsageError(
            f'"{alias}" is not a usable alias: lower-case letters, digits, dot, dash, underscore',
            f"vibe add work --from-config-dir {directory}",
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
            config_dir=str(directory),
        ),
    )
    _emit(
        _action("add", alias=alias, adopted=True, config_dir=str(directory)),
        as_json=as_json,
        human=(
            f'imported the login in {directory} as "{alias}"\n'
            f"Claude Code keeps refreshing it there and vibe never does: "
            f"`vibe run {alias} -- claude` starts it with CLAUDE_CONFIG_DIR={directory}\n"
        ),
    )
    return 0


def _require_unused(root: Path, alias: str) -> None:
    """A Claude login may not take a Cursor account's alias, nor a reserved word."""
    if (
        alias in cursor.RESERVED_ALIASES
        or cursor.is_cursor(root, alias)
        or codex.is_codex(root, alias)
        or antigravity.has_account(root, alias)
    ):
        raise UsageError(f'"{alias}" is already used (or reserved)', "vibe list")


def _add_cursor(ctx: Context, alias: str | None, as_json: bool) -> int:
    """Register a Cursor seat. vibe never sees its credentials: `cursor-agent login` keeps them."""
    wanted = alias if alias is not None else "cursor"
    if not store.valid_alias(wanted):
        raise UsageError(
            f'"{wanted}" is not a usable alias: lower-case letters, digits, dot, dash, underscore',
            "vibe add cursor --kind cursor",
        )
    if wanted in _taken(ctx.root):
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


def _add_codex(ctx: Context, alias: str | None, as_json: bool) -> int:
    """Register a Codex account: the Codex CLI's login, which vibe reads and never writes."""
    wanted = alias if alias is not None else "codex"
    if not store.valid_alias(wanted):
        raise UsageError(
            f'"{wanted}" is not a usable alias: lower-case letters, digits, dot, dash, underscore',
            "vibe add codex --kind codex",
        )
    if wanted in _taken(ctx.root):
        raise UsageError(f'"{wanted}" is already used (or reserved)', "vibe list")
    # $CODEX_HOME at this moment is the home the account is bound to; none means ~/.codex.
    given = os.environ.get("CODEX_HOME")
    home = Path(given).expanduser() if given else None
    login = ctx.codex_auth.read(home)
    codex.write_marker(ctx.root, wanted, added_at=ctx.now_s, codex_home=home)
    if login is None:
        human = (
            f'added Codex account "{wanted}", but there is no Codex login to read: '
            f"run: {codex.LOGIN_CMD} ({codex.STORE_HINT})\n"
        )
    elif login.auth_mode == codex.AUTH_MODE_API_KEY:
        human = (
            f'added Codex account "{wanted}" (an API-key login: it has no plan usage to show; '
            f"run: {codex.LOGIN_CMD} to sign in with ChatGPT)\n"
        )
    else:
        who = f"logged in as {login.email or 'an unknown user'}"
        if login.plan:
            who += f", plan {login.plan}"
        human = f'added Codex account "{wanted}" ({who})\n'
    _emit(
        _action(
            "add",
            alias=wanted,
            kind=codex.KIND_CODEX,
            logged_in=login is not None and login.usable,
        ),
        as_json=as_json,
        human=human,
    )
    return 0


def _adopt_stored(ctx: Context, alias: str | None, given: Path | None, as_json: bool) -> int:
    """Hand the login vibe stored for ``alias`` to Claude Code, in ``given``'s own login."""
    if given is None or alias is None:
        raise UsageError(
            "--adopt-stored-login needs the account and the dir: "
            "vibe add ALIAS --from-config-dir DIR --adopt-stored-login",
            "vibe add --help",
        )
    directory = keychain.resolve_config_dir(given)
    adopt.adopt_stored_login(
        ctx.root,
        alias,
        directory,
        dir_port=ctx.port_for(directory),
        default_port=ctx.port,
        client=ctx.client,
        now_ms=int(ctx.now_s * 1000),
    )
    _emit(
        _action("add", alias=alias, adopted=True, stored_login=True, config_dir=str(directory)),
        as_json=as_json,
        human=(
            f'"{alias}" now lives in {directory}: its login is Claude Code\'s there, and vibe '
            "never refreshes it again\n"
            f"start it with: vibe run {alias} -- claude\n"
        ),
    )
    return 0


def cmd_add(args: argparse.Namespace, ctx: Context) -> int:
    if args.kind != cursor.KIND_CLAUDE and (
        args.from_config_dir is not None or args.adopt_stored_login
    ):
        raise UsageError(
            "--from-config-dir and --adopt-stored-login are for Claude accounts",
            "vibe add --help",
        )
    if args.kind == cursor.KIND_CURSOR:
        return _add_cursor(ctx, args.alias, args.json)
    if args.kind == codex.KIND_CODEX:
        return _add_codex(ctx, args.alias, args.json)
    if args.kind == antigravity.KIND:
        alias = args.alias if args.alias is not None else antigravity.KIND
        if alias in _taken(ctx.root):
            raise UsageError(f'"{alias}" is already used (or reserved)', "vibe list")
        antigravity.write_marker(ctx.root, alias, added_at=ctx.now_s)
        _emit(
            _action("add", alias=alias, kind=antigravity.KIND),
            as_json=args.json,
            human=f'added Antigravity account "{alias}" (uses the agy login)\n',
        )
        return 0
    if args.adopt_stored_login:
        return _adopt_stored(ctx, args.alias, args.from_config_dir, args.json)
    if args.from_config_dir is not None:
        return _import(ctx, args.alias, args.from_config_dir, args.json)
    if args.alias is None:
        return _adopt(ctx, args.json)
    return _login(ctx, args.alias, args.json)


def _collect_and_record(ctx: Context) -> tuple[list[AccountView], float | None]:
    views = envelope.collect(
        ctx.root, client=ctx.client, port=ctx.port, now_s=ctx.now_s, port_for=ctx.port_for
    )
    auto.publish(ctx.root, port=ctx.port, now_s=ctx.now_s)
    with closing(history.connect(store.history_path(ctx.root))) as conn:
        history.record(conn, at_s=ctx.now_s, pool=envelope.pool_weeks(views))
        window = history.samples(conn, since_s=ctx.now_s - history.RETENTION_DAYS * 86_400.0)
    forecast = dry_in(window)
    return views, None if forecast is None else forecast.total_seconds()


def _render_cursor(reading: launch.CursorReading, *, now_s: float, colour: bool) -> list[str]:
    """One Cursor seat: who, then its included usage and on-demand budget as rows."""
    who = reading.email or "—"
    header = f"  {reading.alias}  {who}  Cursor{f' · team {reading.team}' if reading.team else ''}"
    if not reading.logged_in:
        return [header, f"    not logged in — run: {cursor.AGENT_LOGIN}"]
    if reading.usage is None:
        return [header, f"    usage: unknown ({reading.note}); see cursor.com/dashboard"]
    return display.block(
        header,
        [reading.note] if reading.note else [],
        display.cursor_limits(reading),
        now_s=now_s,
        colour=colour,
    )


def _render_codex(reading: launch.CodexReading, *, now_s: float, colour: bool) -> list[str]:
    """One Codex account: who, the plan, then each window and credit pool as rows."""
    who = reading.email or "—"
    plan = f" · {reading.plan}" if reading.plan else ""
    header = f"  {reading.alias}  {who}  Codex{plan}"
    if reading.state in ("needs_login", "api_key"):
        return [header, f"    {reading.note}"]
    usage = reading.usage
    if usage is None:
        return [header, f"    usage: unknown ({reading.note})"]
    lines = display.block(
        header,
        [reading.note] if reading.note else [],
        display.codex_limits(reading),
        now_s=now_s,
        colour=colour,
    )
    if usage.credits_unlimited:
        lines.append("    credits: unlimited")
    elif usage.credits_balance is not None:
        lines.append(f"    credits: {usage.credits_balance}")
    return lines


def _render_antigravity(entry: antigravity.Entry, *, now_s: float, colour: bool) -> list[str]:
    """One Antigravity account: each model group's windows as rows, the share used."""
    limits = display.antigravity_limits(entry)
    return display.block(
        f"  {entry['alias']}  {antigravity.NAME}",
        [entry["message"]] if entry["message"] else [],
        limits,
        now_s=now_s,
        colour=colour,
        empty=None if entry["message"] else "no limits reported",
    )


def cmd_list(args: argparse.Namespace, ctx: Context) -> int:
    # Cursor first: the status file `_collect_and_record` writes should hold these numbers.
    readings = ctx.cursor_readings()
    codex_seen = ctx.codex_readings()
    agy_seen = ctx.antigravity_entries()
    views, dry = _collect_and_record(ctx)
    colour = not args.json and display.use_colour(sys.stdout)
    now_s = ctx.now_s
    human = _render_accounts(views, now_s=now_s, dry=dry, colour=colour)
    for problem in broker.problems(ctx.root):
        human += f"broker problem: {problem}\n"
    if readings or codex_seen or agy_seen:
        shown = [
            line
            for reading in readings
            for line in (*_render_cursor(reading, now_s=now_s, colour=colour), "")
        ]
        shown += [
            line
            for reading in codex_seen
            for line in (*_render_codex(reading, now_s=now_s, colour=colour), "")
        ]
        shown += [
            line
            for entry in agy_seen
            for line in (*_render_antigravity(entry, now_s=now_s, colour=colour), "")
        ]
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
            cursor=[launch.cursor_entry(reading) for reading in readings],
            codex=[launch.codex_entry(reading) for reading in codex_seen],
            antigravity=agy_seen,
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
    if codex.is_codex(ctx.root, args.alias):
        raise UsageError(
            f'"{args.alias}" is a Codex account: it has no Claude Code login to switch to',
            "vibe list",
        )
    if antigravity.has_account(ctx.root, args.alias):
        raise UsageError(
            f'"{args.alias}" is an Antigravity account: it has no Claude Code login to switch to',
            "vibe list",
        )
    oauth.ensure_usable(ctx.root, args.alias, ctx.client, now_ms=int(ctx.now_s * 1000))
    store.switch(ctx.root, args.alias, ctx.port, now_s=ctx.now_s)
    _emit(
        _action("switch", alias=args.alias),
        as_json=args.json,
        human=f'Claude Code now uses "{args.alias}"\n',
    )
    return 0


def _render_status(status: auto.Status) -> str:
    live = status.live or "unknown"
    if status.live is not None and status.active != status.live:
        live += f"  (vibe's active account is {status.active or 'unset'}"
        live += f" - run: vibe switch {status.live})"
    return f"live         {live}\n"


def cmd_auto_status(args: argparse.Namespace, ctx: Context) -> int:
    status = auto.status(ctx.root, port=ctx.port)
    _emit(
        _action(
            "auto status",
            live=status.live,
            active=status.active,
            broker=_broker_entries(ctx),
        ),
        as_json=args.json,
        # Only once it is on: an install that never used the broker reads as it always did.
        human=_render_status(status)
        + "".join(f"{line}\n" for line in _broker_lines(ctx) if status_shows_broker(ctx)),
    )
    return 0


def cmd_auto_tick(args: argparse.Namespace, ctx: Context) -> int:
    ctx.refresh_cursor_cache()  # before the tick, so the status file it writes has fresh numbers
    result = auto.tick(
        ctx.root,
        client=ctx.client,
        port=ctx.port,
        now_s=ctx.now_s,
        port_for=ctx.port_for,
        antigravity_port=ctx.antigravity_port,
    )
    _emit(
        _action("auto tick", line=result.line),
        as_json=args.json,
        human=result.line + "\n",
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
    session = (
        (args.session, True)
        if args.session
        else statusline.session_account(os.environ, root=ctx.root, home=Path.home())
    )
    out(statusline.render(statusline.read(ctx.root), now_s=ctx.now_s, session=session) + "\n")
    return 0


def cmd_remove(args: argparse.Namespace, ctx: Context) -> int:
    if cursor.is_cursor(ctx.root, args.alias):
        cursor.delete_marker(ctx.root, args.alias)  # the Cursor login itself is cursor-agent's
    elif codex.is_codex(ctx.root, args.alias):
        codex.delete_marker(ctx.root, args.alias)  # the Codex login itself is the CLI's
    elif antigravity.has_account(ctx.root, args.alias):
        antigravity.remove(ctx.root, args.alias)
    else:
        if broker.is_on(ctx.root) and store.read_active(ctx.root) == args.alias:
            # With the broker on, vibe's store holds the only refresh token of the live login;
            # Claude Code's default item is a mirror that ends with its access token.
            raise UsageError(
                f'"{args.alias}" is the live login, and with the token broker on vibe holds its '
                "only refresh token: removing it would end every session on it within hours",
                "vibe broker off   (or vibe switch <another> first)",
            )
        store.delete_account(ctx.root, args.alias)
    _emit(
        _action("remove", alias=args.alias),
        as_json=args.json,
        human=f'removed "{args.alias}"\n',
    )
    return 0


def cmd_release(args: argparse.Namespace, ctx: Context) -> int:
    """Move a config dir's login into vibe's store; the dir's own item is deleted."""
    account, directory = release.release(
        ctx.root,
        args.alias,
        port_for=ctx.port_for,
        default_port=ctx.port,
        processes=ctx.processes,
        now_ms=int(ctx.now_s * 1000),
    )
    _emit(
        _action("release", alias=account.alias, config_dir=str(directory)),
        as_json=args.json,
        human=release.released_line(account.alias, directory, Path.home()) + "\n",
    )
    return 0


# --- the token broker ------------------------------------------------------------


def _hhmm(at_ms: int, now_s: float) -> str:
    at = datetime.fromtimestamp(at_ms / 1000)
    return at.strftime("%H:%M" if at.date() == datetime.fromtimestamp(now_s).date() else "%a %H:%M")


def _broker_lines(ctx: Context) -> list[str]:
    settings = broker.read_settings(ctx.root)
    lines = [f"broker       {'on' if settings.on else 'off'}"]
    if not settings.on:
        return lines
    for row in broker.describe(ctx.root):
        where = ", ".join(row.mirrors) or "no mirror yet"
        text = (
            f"  {row.alias:<10} token ends {_hhmm(row.expires_at_ms, ctx.now_s)}, renewed at "
            f"{_hhmm(row.refresh_at_ms, ctx.now_s)}; mirrors: {where}"
        )
        if row.problem:
            text += f"\n  {'':<10} problem: {row.problem}"
            if row.retry_at_s is not None:
                text += f" (next try {_hhmm(int(row.retry_at_s * 1000), ctx.now_s)})"
        lines.append(text)
    return lines


def _broker_entries(ctx: Context) -> dict[str, object]:
    settings = broker.read_settings(ctx.root)
    return {
        "on": settings.on,
        "accounts": [
            {
                "alias": row.alias,
                "expires_at_ms": row.expires_at_ms,
                "refresh_at_ms": row.refresh_at_ms,
                "mirrors": list(row.mirrors),
                "problem": row.problem,
                "retry_at_s": row.retry_at_s,
            }
            for row in (broker.describe(ctx.root) if settings.on else [])
        ],
    }


def status_shows_broker(ctx: Context) -> bool:
    return broker.is_on(ctx.root)


def cmd_broker_status(args: argparse.Namespace, ctx: Context) -> int:
    _emit(
        _action("broker status", **_broker_entries(ctx)),
        as_json=args.json,
        human="\n".join(_broker_lines(ctx)) + "\n",
    )
    return 0


def cmd_broker_on(args: argparse.Namespace, ctx: Context) -> int:
    """Make vibe the one refresher: the default login becomes a mirror, every account's dir too."""
    report = broker.turn_on(
        ctx.root,
        client=ctx.client,
        default_port=ctx.port,
        port_for=ctx.port_for,
        now_s=ctx.now_s,
    )
    said = [*report.lines, *(f"problem: {problem}" for problem in report.problems)]
    _emit(
        _action("broker on", lines=list(report.lines), problems=list(report.problems)),
        as_json=args.json,
        human="broker on: vibe renews every login; Claude Code reads mirrors\n"
        + "".join(f"  {line}\n" for line in said),
    )
    return 0


def cmd_broker_off(args: argparse.Namespace, ctx: Context) -> int:
    said = broker.turn_off(ctx.root, default_port=ctx.port, port_for=ctx.port_for)
    _emit(
        _action("broker off", lines=said),
        as_json=args.json,
        human="broker off\n" + "".join(f"  {line}\n" for line in said),
    )
    return 0


def cmd_broker_mirror(args: argparse.Namespace, ctx: Context) -> int:
    """Name (or create) the config dir that mirrors an account, link what ~/.claude shares."""
    if not broker.brokered(ctx.root, args.alias):
        raise UsageError(
            f'"{args.alias}" is not an account vibe holds (unknown, Cursor, or still living in '
            "its own config dir)",
            f"vibe release {args.alias}",
        )
    settings = broker.read_settings(ctx.root)
    mirrors = dict(settings.mirrors)
    if args.dir is not None:
        mirrors[args.alias] = str(keychain.resolve_config_dir(Path(args.dir)))
    settings = replace(settings, mirrors=mirrors)
    directory = broker.mirror_dir(settings, args.alias, Path.home())
    for other in broker.brokered_aliases(ctx.root):
        if other != args.alias and broker.mirror_dir(settings, other, Path.home()) == directory:
            raise UsageError(f"{directory} already mirrors {other}", "vibe broker status")
    for other in store.list_aliases(ctx.root):
        given = store.read_account(ctx.root, other).config_dir
        if given is not None and Path(given).resolve() == directory.resolve():
            raise UsageError(
                f"{directory} is the config dir {other} lives in: Claude Code's login, "
                "not a mirror",
                f"vibe release {other}",
            )
    if directory == Path.home() / ".claude":
        raise UsageError(
            "~/.claude is the default login, which mirrors the live account", "vibe broker status"
        )
    said = broker.prepare_dir(directory, Path.home())
    broker.write_settings(ctx.root, settings)
    if settings.on:
        report = broker.run(
            ctx.root,
            client=ctx.client,
            default_port=ctx.port,
            port_for=ctx.port_for,
            now_s=ctx.now_s,
            only=(args.alias,),
        )
        said += [*report.lines, *(f"problem: {problem}" for problem in report.problems)]
    _emit(
        _action("broker mirror", alias=args.alias, dir=str(directory), lines=said),
        as_json=args.json,
        human=f"{args.alias} mirrors into {directory}\n" + "".join(f"  {s}\n" for s in said),
    )
    return 0


def cmd_alias(args: argparse.Namespace, ctx: Context) -> int:
    if broker.is_on(ctx.root) and broker.brokered(ctx.root, args.old):
        # The mirror dir, the broker's state and running fixed sessions are keyed by the name.
        raise UsageError(
            f'"{args.old}" has token broker mirrors under its name; renaming it would leave '
            "them unrenewed",
            "vibe broker off   (rename, then vibe broker on)",
        )
    if cursor.is_cursor(ctx.root, args.old):
        _require_unused(ctx.root, args.new)
        if args.new in store.list_aliases(ctx.root):
            raise UsageError(f'there is already an account named "{args.new}"', "vibe list")
        cursor.rename_marker(ctx.root, args.old, args.new)
    elif codex.is_codex(ctx.root, args.old):
        _require_unused(ctx.root, args.new)
        if args.new in store.list_aliases(ctx.root):
            raise UsageError(f'there is already an account named "{args.new}"', "vibe list")
        codex.rename_marker(ctx.root, args.old, args.new)
    elif antigravity.has_account(ctx.root, args.old):
        if args.new in _taken(ctx.root):
            raise UsageError(f'"{args.new}" is already used (or reserved)', "vibe list")
        antigravity.rename(ctx.root, args.old, args.new)
    else:
        _require_unused(ctx.root, args.new)
        store.rename_account(ctx.root, args.old, args.new)
    _emit(
        _action("alias", old=args.old, new=args.new),
        as_json=args.json,
        human=f'"{args.old}" is now "{args.new}"\n',
    )
    return 0


def _fresh_token(ctx: Context, alias: str) -> Credential:
    account = store.read_account(ctx.root, alias)
    now_ms = int(ctx.now_s * 1000)
    if account.config_dir is not None:
        # Planning sends such an account to its config dir; this is the net under that.
        raise UsageError(
            f'"{alias}" is the Claude Code login in {account.config_dir}, which only Claude Code '
            "refreshes",
            f"vibe run {alias} -- claude",
        )
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
        allow_force=args.allow_force,
        allow_fast=args.allow_fast,
        allow_no_zdr=args.allow_no_zdr,
        trust_workspace=args.trust_workspace,
        no_bridge=args.no_bridge,
        pass_env=tuple(name.strip() for name in (args.pass_env or "").split(",") if name.strip()),
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


DEFAULT_COMMAND: Final = "claude"


def cmd_run(args: argparse.Namespace, ctx: Context) -> int:
    # Only the leading separator is ours; a `--` the child itself needs must
    # survive into its argv.
    command = args.command[1:] if args.command[:1] == ["--"] else list(args.command)
    if not command:
        # `vibe run cursor` means a session: Claude Code, or Cursor's agent for a Cursor seat.
        command = [DEFAULT_COMMAND]
    flags = _run_flags(args)
    plan = launch.plan_run(ctx.services(), args.alias, command, flags)
    # The child's environment is built on purpose, never inherited whole (childenv.py).
    # What was dropped is in the --json and --dry-run output (names only), not on every start.
    child = childenv.build(
        os.environ, strict=plan.kind == cursor.KIND_CURSOR, pass_env=flags.pass_env
    )
    # Last, so the line about a move to Cursor's own UI is the final thing before it starts.
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
                env_dropped=list(child.dropped),
            ),
            as_json=args.json,
            human=f"would run on {plan.alias} ({plan.kind}): {' '.join(plan.argv)}\n",
        )
        return 0
    # Which account this session is, for its status line: fixed for the life of the session.
    marked = {**child.env, childenv.SESSION_VAR: plan.alias}
    if plan.kind == cursor.KIND_CURSOR:
        # No credential of ours goes anywhere: cursor-agent reads its own login.
        code = _spawn(plan.argv, marked)
    elif plan.mirror and plan.config_dir is not None:
        # The token broker's mirror of this account: vibe puts a working access token (and no
        # refresh token) into the dir's Keychain item now and renews it there from the tick, so
        # the session is fixed to this account and never dies from its token. No token override.
        directory = broker.ensure_for_run(
            ctx.root,
            plan.alias,
            client=ctx.client,
            default_port=ctx.port,
            port_for=ctx.port_for,
            now_s=ctx.now_s,
        )
        code = _spawn(plan.argv, {**marked, "CLAUDE_CONFIG_DIR": str(directory)})
    elif plan.config_dir is not None:
        # The account's login is Claude Code's, in that dir: it reads and refreshes it there. Not a
        # token of ours, so a session that outlives the access token keeps its login, and vibe
        # starting another run can never take it away.
        code = _spawn(plan.argv, {**marked, "CLAUDE_CONFIG_DIR": plan.config_dir})
    else:
        credential = _fresh_token(ctx, plan.alias)
        if cursor_translate.is_claude_command(plan.argv) and not {"-p", "--print"} & set(plan.argv):
            ends = datetime.fromtimestamp(credential.expires_at_ms / 1000).strftime("%H:%M")
            _say(f"fixed to {plan.alias} until {ends}: its login cannot renew in this session")
        # The fifth permitted reveal() site: the child's environment. The
        # global Keychain credential is untouched, so other shells keep their account.
        explicit = {childenv.TOKEN_VAR: credential.access_token.reveal()}
        code = _spawn(plan.argv, {**marked, **explicit})
    if args.json and code != 130:
        out(envelope.dumps(_action("run", alias=plan.alias, kind=plan.kind, exit_code=code)) + "\n")
    return code


def _bridge_upstream(name: str) -> mcp_oauth.Upstream:
    found = bridge.UPSTREAMS.get(name)
    if found is None:
        raise UsageError(
            f"unknown upstream {name!r}; known: {', '.join(bridge.UPSTREAMS)}",
            "vibe bridge status",
        )
    return found


def _bridge_config(ctx: Context) -> bridge.BridgeConfig:
    return bridge.parse_config(route.read_routing(ctx.root).bridge)


def _bridge_upstreams(ctx: Context, config: bridge.BridgeConfig) -> bridge.Upstreams:
    return bridge.Upstreams(
        client=ctx.bridge_client,
        secrets=ctx.secrets,
        root=ctx.root,
        config=config,
        clock=ctx.clock,
    )


def cmd_bridge_serve(args: argparse.Namespace, ctx: Context) -> int:
    config = _bridge_config(ctx)
    upstreams = _bridge_upstreams(ctx, config)
    server = bridge.Bridge(
        config=config,
        forward=upstreams.forward,
        logged_in=upstreams.logged_in,
        log=lambda entry: bridge.append_log(ctx.root, entry),
        can_write=upstreams.can_write,
    )
    # The protocol owns stdout; vibe's own words go to stderr.
    bridge.serve(server, sys.stdin, sys.stdout)
    return 0


def cmd_bridge_login(args: argparse.Namespace, ctx: Context) -> int:
    """The user's own OAuth login to one upstream. Needs the user in the browser."""
    upstream = _bridge_upstream(args.upstream)
    client_id: str | None = args.client_id
    client_secret: Secret | None = None
    if upstream.registration == "preregistered":
        # Slack has no dynamic client registration: the user's own Slack app (an internal app, with
        # the redirect URL below) supplies a client id and secret. The secret is never an argument.
        if client_id is None:
            err(f"{upstream.name} app client id: ")
            client_id = ctx.prompt("").strip()
        err(f"{upstream.name} app client secret (not shown): ")
        client_secret = Secret(ctx.secret_prompt("").strip())
    receiver = ctx.receiver(args.port)
    try:
        err(f"redirect URL for the login: {receiver.redirect_uri}\n")
        tokens = mcp_oauth.login(
            upstream,
            client=ctx.bridge_client,
            secrets=ctx.secrets,
            root=ctx.root,
            receiver=receiver,
            open_browser=ctx.browser,
            say=lambda text: err(text + "\n"),
            now_s=ctx.clock(),
            client_id=client_id or None,
            client_secret=client_secret,
        )
    finally:
        receiver.close()
    _emit(
        _action("bridge login", upstream=upstream.id, scope=tokens.scope),
        as_json=args.json,
        human=f"logged in to {upstream.name}; the token is in the Keychain\n",
    )
    return 0


def cmd_bridge_logout(args: argparse.Namespace, ctx: Context) -> int:
    names = (
        list(bridge.UPSTREAMS) if args.upstream == "all" else [_bridge_upstream(args.upstream).id]
    )
    gone = [
        name for name in names if mcp_oauth.forget(ctx.root, ctx.secrets, bridge.UPSTREAMS[name])
    ]
    _emit(
        _action("bridge logout", upstreams=gone),
        as_json=args.json,
        human=(
            (
                f"logged out of {', '.join(gone)}: the Keychain items are deleted. To invalidate "
                "the token itself, remove the app from your Slack or Atlassian account.\n"
            )
            if gone
            else "there was no login to remove\n"
        ),
    )
    return 0


def cmd_bridge_status(args: argparse.Namespace, ctx: Context) -> int:
    """Per upstream: logged in or not, when the token expires, what was asked for. No token."""
    lines: list[str] = []
    entries: list[dict[str, object]] = []
    for upstream in bridge.UPSTREAMS.values():
        tokens = mcp_oauth.load(ctx.root, ctx.secrets, upstream)
        allowed = [tool.name for tool in bridge.TOOLS if tool.upstream == upstream.id]
        if tokens is None:
            lines.append(f"{upstream.id:<10}not logged in  (vibe bridge login {upstream.id})")
        else:
            left = (
                None if tokens.expires_at is None else round((tokens.expires_at - ctx.clock()) / 60)
            )
            when = (
                "no expiry given"
                if left is None
                else f"access token {left} min left"
                if left > 0
                else "access token expired (refreshes on use)"
            )
            lines.append(f"{upstream.id:<10}logged in; {when}; scopes: {tokens.scope or 'default'}")
        writers = [
            tool.name for tool in bridge.TOOLS if tool.upstream == upstream.id and tool.writes
        ]
        may_write = bool(writers) and mcp_oauth.can_write(ctx.root, upstream)
        if not writers:
            lines.append(f"{'':<10}{len(allowed)} allowlisted tools, all reads")
        elif may_write:
            lines.append(
                f"{'':<10}{len(allowed)} allowlisted tools: reads, and writes that never delete"
            )
        else:
            lines.append(
                f"{'':<10}{len(allowed) - len(writers)} allowlisted tools, all reads; writes "
                f"need a new login (vibe bridge login {upstream.id})"
            )
        entries.append(
            {
                "upstream": upstream.id,
                "logged_in": tokens is not None,
                "scope": tokens.scope if tokens else None,
                "tools": len(allowed),
                "writes": may_write,
            }
        )
    _emit(
        _action("bridge status", upstreams=entries),
        as_json=args.json,
        human="\n".join(lines) + "\n",
    )
    return 0


def _property_names(tool: Mapping[str, object]) -> set[str]:
    """The argument names in a vendor's input schema; only plain identifiers, nothing else."""
    schema = tool.get("inputSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    return {
        name
        for name in (properties if isinstance(properties, dict) else {})
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_]{1,40}", name)
    }


def cmd_bridge_tools(args: argparse.Namespace, ctx: Context) -> int:
    """After a login: which allowlisted tools the live upstream offers, and whether our argument
    names match its own. Names only: nothing the vendor wrote is printed."""
    upstream = _bridge_upstream(args.upstream)
    live = {
        str(tool.get("name")): tool
        for tool in _bridge_upstreams(ctx, _bridge_config(ctx)).upstream_tools(upstream)
    }
    lines: list[str] = []
    for tool in bridge.TOOLS:
        if tool.upstream != upstream.id:
            continue
        found = live.get(tool.upstream_tool)
        if found is None:
            lines.append(f"  {tool.name:<42}NOT offered")
            continue
        ours, theirs = {name for name, _ in tool.args}, _property_names(found)
        if not ours or ours <= theirs:
            mark = "offered, arguments match"
        else:
            mark = (
                f"offered, arguments DIFFER: ours {sorted(ours - theirs)} "
                f"not in its {sorted(theirs)}"
            )
        lines.append(f"  {tool.name:<42}{mark}")
    reachable = {t.upstream_tool for t in bridge.TOOLS if t.upstream == upstream.id}
    unreachable = len(set(live) - reachable)
    lines.append(
        f"{upstream.name} offers {len(live)} tools; {unreachable} "
        f"{'other is' if unreachable == 1 else 'others are'} not reachable through the bridge"
    )
    _emit(
        _action("bridge tools", upstream=upstream.id, offered=len(live)),
        as_json=args.json,
        human="\n".join(lines) + "\n",
    )
    return 0


def cmd_bridge_add(args: argparse.Namespace, ctx: Context) -> int:
    command = bridge.print_add_command(args.config_dir)
    out(
        "run this yourself (vibe does not): it registers vibe-bridge with that config dir only\n"
        f"  {command}\n"
        "note: `vibe run` adds the bridge by itself to a Claude run\n"
        "on an account without Slack or Atlassian (a vibe-owned --mcp-config; `--no-bridge`\n"
        "turns it off), because such a run does not read this config dir. Register it by hand\n"
        "only for a session you start without `vibe run`.\n"
    )
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
  vibe run <alias>            a session on that account (`-- <cmd>` runs something else)
  vibe add cursor --kind cursor
                              a Cursor seat (log in with `cursor-agent login` first)
  vibe add codex --kind codex
                              a Codex account's plan usage (log in with `codex login` first)
  vibe add antigravity --kind antigravity
                              the quota of the Google account signed in to agy
  vibe usage web              the same dashboard in a browser, http://127.0.0.1:8787

Connector bridge (for a session on an account without connectors)
  vibe bridge login atlassian|slack     your own OAuth login, in the browser (run it yourself)
  vibe bridge status|tools|logout       who is logged in; what the upstream offers; forget a login
  vibe bridge add-command               print the `claude mcp add` line (it never runs it)

Status line
  vibe statusline             one line: who is live and each account's limits, from the
                              numbers the last command or tick saw (no request); for
                              Claude Code's statusLine setting

Housekeeping
  vibe alias old new          rename an account
  vibe remove work            forget an account (the login itself is not revoked)
  vibe release work           move a --from-config-dir login into vibe, so the token broker
                              renews it; the dir's own login is deleted
  vibe usage --once           fetch the numbers once and print them
  vibe --version              the installed version

Token broker (off until you turn it on; docs/BROKER.md)
  vibe broker on              vibe renews every login; Claude Code reads mirrors with no
                              refresh token, so `vibe run main` stays on main for good;
                              `off` gives the login back
  vibe broker mirror main     create ~/.claude-main (links CLAUDE.md, settings, skills, projects)
  vibe broker status          when each token ends and is renewed, its mirrors, any problem

Background tick (vibe never switches accounts by itself)
  vibe auto tick              renew the broker's logins and fetch every account's numbers
                              once, for the status lines; a scheduler runs this
  vibe auto install           macOS: write the launchd job that does that every minute, and
                              print the one command that loads it
  vibe auto status            who is live, and the token broker's state

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
        "--adopt-stored-login",
        action="store_true",
        help=(
            "with ALIAS and --from-config-dir DIR: write the login vibe has stored for ALIAS into "
            "DIR's own Claude Code login (no browser); from then on only Claude Code refreshes it"
        ),
    )
    add.add_argument(
        "--kind",
        choices=(*cursor.KINDS, codex.KIND_CODEX, antigravity.KIND),
        default=cursor.KIND_CLAUDE,
        help=(
            "claude (default), cursor (a Cursor seat, logged in with `cursor-agent login`), "
            "codex (the Codex CLI's login, `codex login`) or antigravity (the agy login)"
        ),
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

    run = subs.add_parser("run", help="a session on one account (an alias from `vibe list`)")
    run.add_argument("alias", help="an account alias from `vibe list`")
    # On a Cursor account: the connectors the run needs (refused when the seat lacks one).
    run.add_argument("--needs", metavar="LIST", help=argparse.SUPPRESS)
    run.add_argument(
        "--allow-force", action="store_true", help="on Cursor: let bypassPermissions mean --force"
    )
    run.add_argument("--allow-fast", action="store_true", help='on Cursor: allow "-fast" models')
    run.add_argument(
        "--allow-no-zdr", action="store_true", help='on Cursor: allow "(NO ZDR)" models'
    )
    run.add_argument(
        "--no-bridge",
        action="store_true",
        help="do not add vibe-bridge (Slack/Atlassian reads) to a Claude run that lacks them",
    )
    run.add_argument(
        "--pass-env",
        metavar="NAMES",
        help="on Cursor: environment variables to pass although they look secret (NAME,NAME)",
    )
    run.add_argument(
        "--trust-workspace",
        action="store_true",
        help="on Cursor: pass --trust (headless runs refuse a workspace nobody trusted)",
    )
    run.add_argument("--dry-run", action="store_true", help="print what would run; run nothing")
    # The command is cut off before parsing (split_run_command); nothing is left for argparse.
    _add_json(run)
    run.set_defaults(handler=cmd_run, command=[])

    bridge_cmd = subs.add_parser(
        "bridge",
        help="vibe-bridge: Slack and Atlassian reads through your own login",
    )
    bridge_subs = bridge_cmd.add_subparsers(dest="bridge_command", required=True)
    serve = bridge_subs.add_parser("serve", help="the stdio MCP server (Claude Code starts it)")
    serve.set_defaults(handler=cmd_bridge_serve)
    login = bridge_subs.add_parser(
        "login", help="log in to an upstream in the browser (you run this)"
    )
    login.add_argument("upstream", choices=list(bridge.UPSTREAMS))
    login.add_argument("--client-id", help="the client id of an app of your own (Slack)")
    login.add_argument(
        "--port", type=int, default=mcp_oauth.DEFAULT_PORT, help="127.0.0.1 port for the redirect"
    )
    _add_json(login)
    login.set_defaults(handler=cmd_bridge_login)
    logout = bridge_subs.add_parser("logout", help="delete an upstream's tokens from the Keychain")
    logout.add_argument("upstream", choices=[*bridge.UPSTREAMS, "all"])
    _add_json(logout)
    logout.set_defaults(handler=cmd_bridge_logout)
    status = bridge_subs.add_parser("status", help="who is logged in and what is allowlisted")
    _add_json(status)
    status.set_defaults(handler=cmd_bridge_status)
    tools = bridge_subs.add_parser("tools", help="which allowlisted tools the live upstream offers")
    tools.add_argument("upstream", choices=list(bridge.UPSTREAMS))
    _add_json(tools)
    tools.set_defaults(handler=cmd_bridge_tools)
    add_cmd = bridge_subs.add_parser(
        "add-command", help="print the `claude mcp add` command for a config dir"
    )
    add_cmd.add_argument(
        "--config-dir", required=True, help="the config dir to register it in, e.g. ~/.claude-work"
    )
    add_cmd.set_defaults(handler=cmd_bridge_add)

    remove = subs.add_parser("remove", help="forget an account")
    remove.add_argument("alias")
    _add_json(remove)
    remove.set_defaults(handler=cmd_remove)

    give_back = subs.add_parser(
        "release",
        help="move a config dir's login (--from-config-dir) into vibe; the dir keeps none",
    )
    give_back.add_argument("alias")
    _add_json(give_back)
    give_back.set_defaults(handler=cmd_release)

    brokering = subs.add_parser(
        "broker", help="token broker: vibe renews every login, Claude Code reads mirrors"
    )
    broker_subs = brokering.add_subparsers(
        dest="broker_command", required=True, metavar="{on,off,status,mirror}"
    )
    for name, handler, summary in (
        ("on", cmd_broker_on, "take the default login over as a mirror; renew every login"),
        ("off", cmd_broker_off, "give the live login back to Claude Code; remove dir mirrors"),
        ("status", cmd_broker_status, "when each token ends and is renewed, its mirrors"),
    ):
        broker_sub = broker_subs.add_parser(name, help=summary)
        _add_json(broker_sub)
        broker_sub.set_defaults(handler=handler)
    mirror_cmd = broker_subs.add_parser(
        "mirror", help="the config dir `vibe run ALIAS` uses (default ~/.claude-ALIAS)"
    )
    mirror_cmd.add_argument("alias")
    mirror_cmd.add_argument("dir", nargs="?", help="default: ~/.claude-ALIAS")
    _add_json(mirror_cmd)
    mirror_cmd.set_defaults(handler=cmd_broker_mirror)

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
        "auto", help="the background tick: token renewal and fresh numbers (never a switch)"
    )
    auto_subs = automatic.add_subparsers(
        dest="auto_command",
        required=True,
        metavar="{status,tick,install,uninstall}",
    )
    for name, handler, summary in (
        ("status", cmd_auto_status, "show who is live and the token broker's state"),
        ("tick", cmd_auto_tick, "renew logins and fetch the numbers once (what a scheduler runs)"),
        ("install", cmd_auto_install, "macOS: write the launchd job that runs a tick a minute"),
        ("uninstall", cmd_auto_uninstall, "macOS: remove that job's file"),
    ):
        auto_sub = auto_subs.add_parser(name, help=summary)
        _add_json(auto_sub)
        auto_sub.set_defaults(handler=handler)

    line = subs.add_parser(
        "statusline", help="one line for Claude Code's status line, from the last numbers"
    )
    line.add_argument(
        "--session",
        metavar="ACCOUNT",
        help="the account this session is (Cursor's status line passes `cursor`)",
    )
    line.set_defaults(handler=cmd_statusline)

    # `vibe help` is what people type; without it argparse answers an unhelpful
    # "invalid choice: 'help'" and lists the commands it just refused to explain.
    helping = subs.add_parser("help", help="what each command is for, and when to use it")
    _add_json(helping)
    helping.set_defaults(handler=cmd_help)

    return parser


_RUN_VALUE_FLAGS: Final = ("--needs", "--pass-env")
# vibe's own `run` options, which may also follow the alias when no `--` comes after it
# (`vibe run cursor --dry-run`): a session with no command, not a command named `--dry-run`.
_RUN_FLAGS: Final = frozenset(
    {
        *_RUN_VALUE_FLAGS,
        "--allow-force",
        "--allow-fast",
        "--allow-no-zdr",
        "--no-bridge",
        "--trust-workspace",
        "--dry-run",
        "--json",
        "-h",
        "--help",
    }
)


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
    while end < len(items) and items[end].split("=", 1)[0] in _RUN_FLAGS:
        end += 2 if items[end] in _RUN_VALUE_FLAGS else 1
    end = min(end, len(items))
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
