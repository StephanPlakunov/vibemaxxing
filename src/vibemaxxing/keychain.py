"""The credential store Claude Code itself reads: the macOS Keychain, or a file.

``CLAUDE_CONFIG_DIR`` is never read, never set and never honoured. With it set
Claude Code scopes the item to a hashed service name and ``NO_KEYCHAIN=1`` does
not reliably produce a plaintext file — measured. We swap credentials, we do not
isolate config directories, so only the fixed-name item is ever written.

The one exception is a read: `vibe add --from-config-dir <dir>` takes over a login
Claude Code keeps for a config dir the person names, from that dir's own item.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Final, Protocol

from vibemaxxing.errors import VibeError
from vibemaxxing.fsutil import private_dir, write_private

CLAUDE_CODE_KEYCHAIN_SERVICE: Final = "Claude Code-credentials"
SECURITY_BIN: Final = "/usr/bin/security"
SECURITY_TIMEOUT_S: Final = 10.0
SECURITY_ABSENT_CODE: Final = 44

_RECOVERY: Final = "vibe add"


class KeychainPort(Protocol):
    def read(self) -> str | None: ...

    def write(self, blob: str) -> None: ...


class MacKeychain:
    def __init__(self, service: str = CLAUDE_CODE_KEYCHAIN_SERVICE) -> None:
        self.service = service

    def read(self) -> str | None:
        result = self._run(
            [
                SECURITY_BIN,
                "find-generic-password",
                "-a",
                getpass.getuser(),
                "-w",
                "-s",
                self.service,
            ]
        )
        if result.returncode == SECURITY_ABSENT_CODE:
            return None
        if result.returncode != 0:
            raise VibeError(
                f"the macOS Keychain refused to read {self.service!r} "
                f"(security exit {result.returncode})",
                _RECOVERY,
            )
        return result.stdout[:-1] if result.stdout.endswith("\n") else result.stdout

    def write(self, blob: str) -> None:
        # The blob sits in argv, where `ps` exposes it to any local user for the
        # duration of one exec. That is a real cross-uid exposure and it is the
        # least bad option measured: `security -i` silently truncates at ~4005
        # bytes and stores the truncated credential, the promptless `-w` form
        # truncates at 128, and Security.framework cannot touch an item this
        # process did not create without a GUI prompt on every switch. A path
        # that can only corrupt is worse than an exposure that is documented.
        result = self._run(
            [
                SECURITY_BIN,
                "add-generic-password",
                "-U",
                "-a",
                getpass.getuser(),
                "-s",
                self.service,
                "-w",
                blob,
            ]
        )
        if result.returncode != 0:
            raise VibeError(
                f"the macOS Keychain refused to write {self.service!r} "
                f"(security exit {result.returncode})",
                _RECOVERY,
            )

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                # Explicit, not the process locale: write encodes the blob as
                # UTF-8 into argv, so decoding the read with whatever LANG says
                # is an asymmetric round trip that mangles any non-ASCII byte.
                encoding="utf-8",
                timeout=SECURITY_TIMEOUT_S,
                check=False,
            )
        # UnicodeDecodeError: a non-UTF-8 item must read as unreadable, not escape as a
        # traceback from every dashboard poll.
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
            raise VibeError(
                f"could not run {SECURITY_BIN}: {exc.__class__.__name__}",
                _RECOVERY,
            ) from None


class FileKeychain:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def _path(self) -> Path:
        return self.path if self.path is not None else Path.home() / ".claude" / ".credentials.json"

    def read(self) -> str | None:
        try:
            return self._path().read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise VibeError(
                f"could not read {self._path()}: {exc.__class__.__name__}",
                _RECOVERY,
            ) from None

    def write(self, blob: str) -> None:
        path = self._path()
        private_dir(path.parent)
        try:
            write_private(path, blob)
        except OSError as exc:
            raise VibeError(
                f"could not write {path}: {exc.__class__.__name__}",
                _RECOVERY,
            ) from None


def default_port() -> KeychainPort:
    # Bound to a local str on purpose: mypy resolves a bare `sys.platform`
    # comparison against the checking host, which makes the other two branches
    # statically unreachable and unverifiable.
    platform: str = sys.platform
    if platform == "win32":
        raise VibeError(
            "vibemaxxing has no Windows credential store and never writes one",
            "vibe --help",
        )
    if platform == "darwin":
        return MacKeychain()
    return FileKeychain()


def config_dir_service(config_dir: Path) -> str:
    """The Keychain item Claude Code keeps its login in when CLAUDE_CONFIG_DIR is ``config_dir``.

    Claude Code appends "-" and the first 8 hex digits of the sha256 of the dir, NFC
    normalised. Checked against the item Claude Code created on this fork's machine for
    an ASCII path; the NFC step is what Claude Code's source does, not something measured.
    """
    path = unicodedata.normalize("NFC", str(config_dir))
    return f"{CLAUDE_CODE_KEYCHAIN_SERVICE}-{hashlib.sha256(path.encode()).hexdigest()[:8]}"


def resolve_config_dir(config_dir: Path) -> Path:
    """The dir as the hash must see it: absolute, `~` expanded, no trailing slash.

    Symlinks are not resolved; Claude Code hashes the path it was given.
    """
    return Path(os.path.abspath(os.path.expanduser(config_dir)))


def config_dir_location(config_dir: Path) -> str:
    """Where Claude Code keeps the login for ``config_dir``, for a message that names it."""
    platform: str = sys.platform
    if platform == "darwin":
        return f"the Keychain item {config_dir_service(config_dir)!r}"
    return str(config_dir / ".credentials.json")


def config_dir_port(config_dir: Path) -> KeychainPort:
    """The store Claude Code keeps the login for ``config_dir`` in. Only ever read."""
    platform: str = sys.platform
    if platform == "win32":
        return default_port()  # raises: there is no Windows store
    if platform == "darwin":
        return MacKeychain(config_dir_service(config_dir))
    return FileKeychain(config_dir / ".credentials.json")
