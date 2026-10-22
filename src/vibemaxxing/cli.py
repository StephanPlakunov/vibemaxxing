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
    envelope,
    history,
    keychain,
    oauth,
    store,
    tui,
    web,
)
from vibemaxxing.credentials import Credential
from vibemaxxing.envelope import AccountView
from vibemaxxing.errors import (
    NeedsLoginError,
    NetworkError,
    NotFoundError,
    UsageError,
    VibeError,
)
from vibemaxxing.httpclient import HttpClient, UrllibClient
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


def _free_alias(root: Path, wanted: str) -> str:
    taken = set(store.list_aliases(root))
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


def cmd_add(args: argparse.Namespace, ctx: Context) -> int:
    if args.from_config_dir is not None:
        return _import(ctx, args.alias, args.from_config_dir, args.json)
    if args.alias is None:
        return _adopt(ctx, args.json)
    return _login(ctx, args.alias, args.json)


def _collect_and_record(ctx: Context) -> tuple[list[AccountView], float | None]:
    views = envelope.collect(ctx.root, client=ctx.client, port=ctx.port, now_s=ctx.now_s)
    with closing(history.connect(store.history_path(ctx.root))) as conn:
        history.record(conn, at_s=ctx.now_s, pool=envelope.pool_weeks(views))
        window = history.samples(conn, since_s=ctx.now_s - history.RETENTION_DAYS * 86_400.0)
    forecast = dry_in(window)
    return views, None if forecast is None else forecast.total_seconds()


def cmd_list(args: argparse.Namespace, ctx: Context) -> int:
    views, dry = _collect_and_record(ctx)
    _emit(
        envelope.build(
            views,
            now_s=ctx.now_s,
            dry_in=None if dry is None else timedelta(seconds=dry),
            auto=auto.read_settings(ctx.root),
            last_switch=auto.read_last(ctx.root),
        ),
        as_json=args.json,
        human=_render_accounts(views, now_s=ctx.now_s, dry=dry),
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
    steps = " -> ".join(f"{step.account} until {step.until}%" for step in policy.steps)
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


def cmd_remove(args: argparse.Namespace, ctx: Context) -> int:
    store.delete_account(ctx.root, args.alias)
    _emit(
        _action("remove", alias=args.alias),
        as_json=args.json,
        human=f'removed "{args.alias}"\n',
    )
    return 0


def cmd_alias(args: argparse.Namespace, ctx: Context) -> int:
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


def cmd_run(args: argparse.Namespace, ctx: Context) -> int:
    # Only the leading separator is ours; a `--` the child itself needs must
    # survive into its argv.
    command = args.command[1:] if args.command[:1] == ["--"] else list(args.command)
    if not command:
        raise UsageError("vibe run needs a command after --", "vibe run work -- claude")
    credential = _fresh_token(ctx, args.alias)
    # The fifth and last permitted reveal() site: the child's environment. The
    # global Keychain credential is untouched, so other shells keep their account.
    child_env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": credential.access_token.reveal()}
    try:
        completed = subprocess.run(command, env=child_env, check=False)
    except KeyboardInterrupt:
        # The child got the same SIGINT and is already shutting down. Report
        # the conventional 130 rather than a traceback from the wrapper.
        return 130
    if args.json:
        out(envelope.dumps(_action("run", alias=args.alias, exit_code=completed.returncode)) + "\n")
    return completed.returncode


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
    _add_json(add)
    add.set_defaults(handler=cmd_add)

    listing = subs.add_parser("list", help="show every account and the pooled headroom")
    _add_json(listing)
    listing.set_defaults(handler=cmd_list)

    switch = subs.add_parser("switch", help="make one account the active Claude Code login")
    switch.add_argument("alias")
    _add_json(switch)
    switch.set_defaults(handler=cmd_switch)

    run = subs.add_parser("run", help="run one command pinned to one account")
    run.add_argument("alias")
    run.add_argument("command", nargs=argparse.REMAINDER)
    _add_json(run)
    run.set_defaults(handler=cmd_run)

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

    # `vibe help` is what people type; without it argparse answers an unhelpful
    # "invalid choice: 'help'" and lists the commands it just refused to explain.
    helping = subs.add_parser("help", help="what each command is for, and when to use it")
    _add_json(helping)
    helping.set_defaults(handler=cmd_help)

    return parser


def main(argv: Sequence[str] | None = None, *, context: Context | None = None) -> int:
    install_excepthook()
    args = build_parser().parse_args(argv)
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
