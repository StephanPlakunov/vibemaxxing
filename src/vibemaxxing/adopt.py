"""`vibe add ALIAS --from-config-dir DIR --adopt-stored-login`: hand the stored login to a dir.

For an account whose only login is vibe's stored copy (nobody can log in again, or nobody wants
to), this puts that login where Claude Code keeps it for ``CLAUDE_CONFIG_DIR=DIR``: its own
Keychain item and the ``oauthAccount`` of ``DIR/.claude.json``. From then on Claude Code in that
dir refreshes it and vibe never does (``store.Account.config_dir``).

The hand-over is one move. A refresh token has one holder, and the only way to get a second one
is a refresh by either side, so the order is what matters:

1. Every refusal is decided first, with nothing written.
2. A stored access token that has ended (or a stash an interrupted refresh left) is refreshed
   once, before the claim, so a dead lineage is found here, with nothing handed over, and what
   Claude Code gets is the newest one.
3. Under the account's refresh claim: the account is marked as the dir's *first* (from that
   instant `oauth.refresh` refuses it, also for a refresh that read the account earlier: it
   checks again under the same claim); then the profile; then the Keychain item, read back.
4. If the item did not land and is not there, the mark is taken back: nobody holds the lineage,
   so vibe may refresh it again. If it may be there, the mark stays, and running the command
   again finishes the job.

Nothing here prints or logs a token.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
from pathlib import Path

from vibemaxxing import credentials, oauth, store
from vibemaxxing.credentials import credential_to_blob, parse_blob
from vibemaxxing.errors import NeedsLoginError, NetworkError, StoreError, UsageError, VibeError
from vibemaxxing.httpclient import HttpClient
from vibemaxxing.keychain import KeychainPort
from vibemaxxing.models import AccountState


def adopt_stored_login(
    root: Path,
    alias: str,
    directory: Path,
    *,
    dir_port: KeychainPort,
    default_port: KeychainPort,
    client: HttpClient,
    now_ms: int,
) -> store.Account:
    """Move ``alias``'s stored login into ``directory``'s own Claude Code login."""
    account = _preflight(root, alias, directory, dir_port, default_port, now_ms)

    if account.config_dir is None:
        account = _prove_and_renew(root, alias, account, client, now_ms)

    recovery = f"vibe add {alias} --from-config-dir {directory} --adopt-stored-login"
    with store.hold_refresh_claim(root, alias, recovery):
        # Under the claim nothing else refreshes it, and nothing else can have changed it.
        account = store.read_account(root, alias)
        _refuse_if_changed(account, directory, root, alias, recovery)
        if dir_port.read() is not None:
            raise UsageError(f"Claude Code already has a login for {directory}", recovery)

        directory.mkdir(parents=True, exist_ok=True)
        # (a) First: vibe stops refreshing. Everything after it is Claude Code's side.
        store.write_account(root, replace(account, config_dir=str(directory)))
        try:
            _hand_over(account, directory, dir_port)
        except BaseException:
            _take_the_mark_back_if_nothing_landed(root, account, dir_port)
            raise
    return replace(account, config_dir=str(directory))


def _preflight(
    root: Path,
    alias: str,
    directory: Path,
    dir_port: KeychainPort,
    default_port: KeychainPort,
    now_ms: int,
) -> store.Account:
    recovery = f"vibe add {alias} --from-config-dir {directory} --adopt-stored-login"
    account = store.read_account(root, alias)  # an unknown alias is a NotFoundError
    if account.config_dir is not None and account.config_dir != str(directory):
        raise UsageError(
            f'"{alias}" already lives in {account.config_dir}, not in {directory}',
            f"vibe add {alias} --from-config-dir {account.config_dir}",
        )
    if store.read_active(root) == alias or store.live_alias(root, default_port) == alias:
        # Claude Code's default login holds this lineage already: a second holder is the bug.
        raise UsageError(
            f'"{alias}" is the login Claude Code runs as; it keeps that one itself', "vibe list"
        )
    if account.state is AccountState.NEEDS_LOGIN or credentials.login_lapsed(
        account.credential, now_ms=now_ms
    ):
        raise NeedsLoginError(
            f'the stored login for "{alias}" is dead (the server rejected it, or it lapsed)',
            f"vibe add {alias}",
        )
    if not account.credential.refresh_token:
        raise NeedsLoginError(
            f'the stored login for "{alias}" has no refresh token', f"vibe add {alias}"
        )
    uuid = account.identity.account_uuid
    if uuid is None:
        raise UsageError(
            f'"{alias}" has no account id stored, so no profile for Claude Code can be written',
            f"vibe add {alias}",
        )
    if dir_port.read() is not None:
        raise UsageError(
            f"Claude Code already has a login for {directory}; take that one over instead",
            f"vibe add {alias} --from-config-dir {directory}",
        )
    there = credentials.read_claude_identity(directory / ".claude.json").account_uuid
    if there is not None and there != uuid:
        raise UsageError(
            f"{directory / '.claude.json'} names another account than {alias!r}", recovery
        )
    return account


def _prove_and_renew(
    root: Path, alias: str, account: store.Account, client: HttpClient, now_ms: int
) -> store.Account:
    """Refresh once when the access token has ended or a refresh left a successor in the stash.

    The server accepting it is the proof that the lineage is alive, found before anything is
    handed over; and Claude Code starts with a token that has its full life. Not done for a
    token that still works: Claude Code's own first refresh proves that, and every refresh is a
    rotation this command would rather not spend.
    """
    pending = store.read_stash(root, alias) is not None
    if not pending and not credentials.is_expired(account.credential, now_ms=now_ms):
        return account
    outcome = oauth.refresh(root, alias, account.credential, client, now_ms=now_ms)
    if outcome.error in ("invalid_grant", "no_refresh_token"):
        raise NeedsLoginError(
            f'the server rejected the stored login for "{alias}": nothing was handed over',
            f"vibe add {alias}",
        )
    if outcome.credential is None or outcome.error is not None:
        raise NetworkError(
            f'could not renew the stored login for "{alias}" right now ({outcome.error}): '
            "nothing was handed over",
            f"vibe add {alias} --from-config-dir DIR --adopt-stored-login",
        )
    return store.read_account(root, alias)


def _refuse_if_changed(
    account: store.Account, directory: Path, root: Path, alias: str, recovery: str
) -> None:
    if account.config_dir is not None and account.config_dir != str(directory):
        raise UsageError(f'"{alias}" moved to {account.config_dir} meanwhile', recovery)
    if store.read_stash(root, alias) is not None:
        # A refresh left its successor: the account file is behind it. Handing the file's token
        # over would hand over a spent one.
        raise UsageError(f'a refresh of "{alias}" left a successor to settle first', recovery)
    if store.read_active(root) == alias:
        raise UsageError(f'"{alias}" became the active account meanwhile', recovery)


def _hand_over(account: store.Account, directory: Path, dir_port: KeychainPort) -> None:
    credentials.write_claude_identity(directory / ".claude.json", account.identity)
    # write_claude_identity leaves a corrupt file alone and says nothing; here that is a failure,
    # found before a login that Claude Code could not match to its profile is written.
    written = credentials.read_claude_identity(directory / ".claude.json").account_uuid
    if written != account.identity.account_uuid:
        raise StoreError(
            f"could not write the account into {directory / '.claude.json'}",
            "fix or remove that file, then run this command again",
        )
    dir_port.write(credential_to_blob(account.credential))
    back = dir_port.read()
    if back is None or parse_blob(back).refresh_token != account.credential.refresh_token:
        raise StoreError(
            f"the login read back from {directory}'s Keychain item is not the one just written",
            "run this command again",
        )


def _take_the_mark_back_if_nothing_landed(
    root: Path, before: store.Account, dir_port: KeychainPort
) -> None:
    """The item is not there: nobody else holds the lineage, so vibe may refresh it again.

    If it is there, or cannot be told, the mark stays: a second holder is worse than a retry.
    """
    with contextlib.suppress(VibeError, OSError):
        if dir_port.read() is None:
            store.write_account(root, before)
