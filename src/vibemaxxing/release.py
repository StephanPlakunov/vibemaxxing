"""`vibe release ALIAS`: move a config dir's login back into vibe's store.

The inverse of `--adopt-stored-login` (adopt.py). An account imported with `--from-config-dir`
records its dir, and Claude Code in that dir is the one refresher of its lineage
(`store.Account.config_dir`). For one shared login instead (auto-switch puts each stored account
into Claude Code's default login in turn), vibe has to hold that lineage itself, and the dir must
stop holding it: a refresh token has one holder, and a second copy that a session in the dir
renews spends the copy vibe keeps, and reusing that can revoke the whole login.

So the move is one way and in this order:

1. Every refusal is decided first, with nothing written: not a config-dir account, the active
   or live account, a Claude Code process running with that dir (it holds and renews this
   login), a process list that cannot be read.
2. Under the account's refresh claim: the dir's credential is read from its own Keychain item,
   the identity in ``DIR/.claude.json`` must be the stored account, and a lapsed or rejected
   login is refused. Then the account file gets the credential with ``config_dir`` cleared.
3. Then the dir's item is deleted and read back as gone. If that fails, the account file is put
   back as it was (``config_dir`` and the previous credential), so the login is never held in two
   places with vibe believing it holds it alone.

``DIR/.claude.json`` is left alone. Nothing here prints or logs a token, and nothing sends a
request: the next poll refreshes the login once its access token has ended, as for any account
vibe owns.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Final, Protocol

from vibemaxxing import credentials, keychain, store
from vibemaxxing.errors import NeedsLoginError, StoreError, UsageError, VibeError
from vibemaxxing.keychain import KeychainPort
from vibemaxxing.models import AccountState

PS_BIN: Final = "/bin/ps"
PS_TIMEOUT_S: Final = 10.0
CONFIG_DIR_VAR: Final = "CLAUDE_CONFIG_DIR"
_LINE: Final = re.compile(r"^\s*(\d+)\s(.*)$")
_SETTING: Final = re.compile(rf"(?:^|\s){CONFIG_DIR_VAR}=(\S*)")


class ProcessPort(Protocol):
    def listing(self) -> str:
        """One line per process of this user: its pid, its command and its environment.

        Raises VibeError when the list cannot be read.
        """
        ...


class PsProcesses:
    """`ps -E -o pid=,command= -U <uid>`: on macOS, ``-E`` appends each process's environment
    (as it was at launch) to its command, for the user's own processes."""

    def listing(self) -> str:
        argv = [PS_BIN, "-E", "-o", "pid=,command=", "-U", str(os.getuid())]
        # ps itself would otherwise inherit this process's CLAUDE_CONFIG_DIR and name itself.
        env = {key: value for key, value in os.environ.items() if key != CONFIG_DIR_VAR}
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PS_TIMEOUT_S,
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VibeError(f"could not run {PS_BIN}: {exc.__class__.__name__}") from None
        if done.returncode != 0 or not done.stdout.strip():
            raise VibeError(f"{PS_BIN} could not list the processes (exit {done.returncode})")
        return done.stdout


def _same_dir(value: str, directory: Path) -> bool:
    if not value:
        return False
    given = Path(value).expanduser()
    if not given.is_absolute():
        # Relative to that process's working directory, which is not known here: it might be
        # this dir, and a guess the wrong way leaves a session holding a login vibe took.
        return True
    return os.path.realpath(given) == os.path.realpath(directory)


def holders(listing: str, directory: Path, *, own_pid: int) -> tuple[int, ...]:
    """The pids in ``listing`` that run with CLAUDE_CONFIG_DIR set to ``directory``.

    Paths are compared resolved (``~``, symlinks, a trailing slash). Not this process. A
    command line that merely mentions the variable counts too: a false alarm costs a rerun.
    """
    found: list[int] = []
    for line in listing.splitlines():
        match = _LINE.match(line)
        if match is None:
            continue
        pid = int(match.group(1))
        if pid == own_pid:
            continue
        if any(_same_dir(value, directory) for value in _SETTING.findall(match.group(2))):
            found.append(pid)
    return tuple(found)


def _shown(directory: Path, home: Path) -> str:
    try:
        return f"~/{directory.relative_to(home)}"
    except ValueError:
        return str(directory)


def release(
    root: Path,
    alias: str,
    *,
    port_for: Callable[[Path], KeychainPort],
    default_port: KeychainPort,
    processes: ProcessPort,
    now_ms: int,
    own_pid: int | None = None,
) -> tuple[store.Account, Path]:
    """Move ``alias``'s login out of its config dir into the account file; the dir keeps none."""
    account = store.read_account(root, alias)  # an unknown alias is a NotFoundError
    if account.config_dir is None:
        raise UsageError(f'"{alias}" is not a config-dir login: vibe holds it already', "vibe list")
    directory = Path(account.config_dir)
    recovery = f"vibe release {alias}"
    _refuse_live(root, alias, default_port)
    dir_port = port_for(directory)

    with store.hold_refresh_claim(root, alias, recovery):
        before = store.read_account(root, alias)
        if before.config_dir != account.config_dir:
            raise UsageError(f'"{alias}" changed meanwhile', recovery)
        _refuse_live(root, alias, default_port)
        # As late as it can be, right before the item is read: the window in which a session
        # could start in the dir and renew the login is this function, milliseconds.
        _refuse_sessions(directory, processes, own_pid, recovery)
        credential = _dir_login(before, directory, dir_port, now_ms)
        # vibe holds it from here: it may refresh it, and auto-switch may put it into the
        # default login. Under the claim, so no refresh can start before the item is gone.
        released = replace(
            before, credential=credential, config_dir=None, state=AccountState.OK, message=None
        )
        store.write_account(root, released)
        failure: VibeError | None = None
        try:
            dir_port.delete()
        except VibeError as exc:
            failure = exc
        except BaseException:
            # Interrupted mid-delete: whether the item is gone is not known, so the dir keeps
            # the lineage (vibe never refreshes it) and a rerun finishes the job.
            with contextlib.suppress(VibeError, OSError):
                store.write_account(root, replace(before, credential=credential))
            raise
        gone = _gone(dir_port)
        if gone is not True:
            # Still there: nothing changed, the file goes back as it was. Not known: back to
            # the dir as well, with the newest copy of its login in the file, since the item
            # may still be there and a second refresher is the one thing this must not make.
            restored = before if gone is False else replace(before, credential=credential)
            _put_back(root, restored, directory, failure, known=gone is False)
        # A stash belongs to the lineage vibe refreshed before the import; consumed now, it
        # would replace the login just released with a spent one.
        store.delete_stash(root, alias)
    return released, directory


def _gone(dir_port: KeychainPort) -> bool | None:
    """Whether the dir's item is gone after the delete; None when that cannot be told."""
    try:
        return dir_port.read() is None
    except VibeError:
        return None


def _refuse_live(root: Path, alias: str, default_port: KeychainPort) -> None:
    if store.read_active(root) == alias or store.live_alias(root, default_port) == alias:
        raise UsageError(
            f'"{alias}" is the login Claude Code runs as; there is nothing to release',
            "vibe list",
        )


def _refuse_sessions(
    directory: Path, processes: ProcessPort, own_pid: int | None, recovery: str
) -> None:
    try:
        listing = processes.listing()
    except VibeError as exc:
        # Fail closed: a session nobody can see would keep renewing the login vibe takes.
        raise UsageError(
            f"could not tell whether a Claude Code session runs in {directory} ({exc.message}), "
            "so its login was left where it is",
            recovery,
        ) from None
    pids = holders(listing, directory, own_pid=os.getpid() if own_pid is None else own_pid)
    if pids:
        raise UsageError(
            f"{len(pids)} {'process runs' if len(pids) == 1 else 'processes run'} with "
            f"{CONFIG_DIR_VAR}={directory} (pid {', '.join(map(str, pids))}): a Claude Code "
            "session there holds and renews this login. Close those sessions (and unset "
            f"{CONFIG_DIR_VAR} in this shell, if it is set), then run this again",
            recovery,
        )


def _dir_login(
    account: store.Account, directory: Path, dir_port: KeychainPort, now_ms: int
) -> credentials.Credential:
    alias = account.alias
    blob = dir_port.read()
    if blob is None:
        raise NeedsLoginError(
            f"Claude Code has no login for {directory} "
            f"(looked in {keychain.config_dir_location(directory)})",
            f"CLAUDE_CONFIG_DIR={directory} claude /login",
        )
    credential = credentials.parse_blob(blob)
    stored_uuid = account.identity.account_uuid
    there = credentials.read_claude_identity(directory / ".claude.json").account_uuid
    if stored_uuid is None or there != stored_uuid:
        raise UsageError(
            f"{directory / '.claude.json'} does not name the account stored as {alias!r}, so "
            "the login in that dir may be another account's",
            f"CLAUDE_CONFIG_DIR={directory} claude",
        )
    if credentials.login_lapsed(credential, now_ms=now_ms):
        raise NeedsLoginError(
            f"the login in {directory} has lapsed", f"CLAUDE_CONFIG_DIR={directory} claude /login"
        )
    if (
        account.state is AccountState.NEEDS_LOGIN
        and account.credential.refresh_token == credential.refresh_token
    ):
        raise NeedsLoginError(
            f"the server already rejected the login in {directory}",
            f"CLAUDE_CONFIG_DIR={directory} claude /login",
        )
    return credential


def _put_back(
    root: Path, restored: store.Account, directory: Path, cause: VibeError | None, *, known: bool
) -> None:
    """The item was not deleted (or that cannot be told): the lineage goes back to the dir."""
    item = keychain.config_dir_location(directory)
    why = cause.message if cause is not None else "it is still there after the delete"
    recovery = f"vibe release {restored.alias}"
    try:
        store.write_account(root, restored)
    except (VibeError, OSError) as exc:
        raise StoreError(
            f"could not delete {item} ({why}), and putting the account file back failed too "
            f"({exc.__class__.__name__}): the login may now be held in both places. Close every "
            f"session in {directory} and run this again",
            recovery,
        ) from None
    if known:
        raise StoreError(
            f'could not delete {item} ({why}); "{restored.alias}" is back as it was, the login '
            f"in {directory}",
            recovery,
        )
    raise StoreError(
        f'could not tell whether {item} is gone ({why}); "{restored.alias}" is the login in '
        f"{directory} again, with the newest copy of it in the account file",
        recovery,
    )


def released_line(alias: str, directory: Path, home: Path) -> str:
    shown = _shown(directory, home)
    return f"released {alias}: vibe holds its login now; {shown} has no login any more"
