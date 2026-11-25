"""The Codex account kind: a stored marker, and the Codex CLI's own login, read and never written.

A Codex account has no credential vibe keeps. ``codex login`` stores it, by default in
``$CODEX_HOME/auth.json`` (``~/.codex/auth.json``; openai/codex
``codex-rs/login/src/auth/storage.rs``). vibe only reads that file, to say who is logged in
and to ask the usage endpoint what is left. vibe never refreshes it: the Codex CLI renews
its own tokens, and a second refresher would spend the refresh token the CLI holds. What
vibe stores is a marker, ``~/.vibemaxxing/codex/<alias>.json``, so `vibe list` knows the
alias is a Codex one. A Codex account is never a target of `vibe switch`: it has no Claude
Code login.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from vibemaxxing import store
from vibemaxxing.errors import NotFoundError, StoreError, UsageError
from vibemaxxing.fsutil import private_dir, write_private
from vibemaxxing.redact import Secret

KIND_CODEX: Final = "codex"
LOGIN_CMD: Final = "codex login"
# `cli_auth_credentials_store` in the CLI's config.toml: only "file" keeps an auth.json to read.
STORE_HINT: Final = (
    'set cli_auth_credentials_store = "file" in ~/.codex/config.toml, then `codex login`'
)
AUTH_MODE_CHATGPT: Final = "chatgpt"
AUTH_MODE_API_KEY: Final = "apikey"
_MAX_AUTH_BYTES: Final = 1_000_000


# --- the stored marker ---------------------------------------------------------------


def codex_dir(root: Path) -> Path:
    return root / "codex"


def codex_path(root: Path, alias: str) -> Path:
    if not store.valid_alias(alias):
        raise UsageError(f"{alias!r} is not a usable alias", "vibe list")
    return codex_dir(root) / f"{alias}.json"


def list_codex_aliases(root: Path) -> list[str]:
    try:
        names = [path.stem for path in codex_dir(root).glob("*.json")]
    except OSError:
        return []
    return sorted(name for name in names if store.valid_alias(name))


def is_codex(root: Path, alias: str) -> bool:
    return store.valid_alias(alias) and codex_path(root, alias).is_file()


def default_home(environ: dict[str, str] | None = None) -> Path:
    """``$CODEX_HOME``, else ``~/.codex``: where the CLI keeps ``auth.json``."""
    env = os.environ if environ is None else environ
    given = env.get("CODEX_HOME")
    return Path(given).expanduser() if given else Path(os.path.expanduser("~")) / ".codex"


@dataclass(frozen=True)
class Marker:
    added_at: float
    codex_home: Path | None


def read_marker(root: Path, alias: str) -> Marker:
    path = codex_path(root, alias)
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise NotFoundError(f'there is no account named "{alias}"', "vibe list") from None
    except (OSError, ValueError):
        raise StoreError(f"{path} is unreadable", f"vibe add {alias} --kind codex") from None
    if not isinstance(parsed, dict) or parsed.get("kind") != KIND_CODEX:
        raise StoreError(f"{path} is not a Codex account", f"vibe add {alias} --kind codex")
    added = parsed.get("added_at")
    home = parsed.get("codex_home")
    return Marker(
        float(added) if isinstance(added, (int, float)) and not isinstance(added, bool) else 0.0,
        Path(home) if isinstance(home, str) and home else None,
    )


def read_added_at(root: Path, alias: str) -> float:
    """When the marker was written; the cache key that ties usage numbers to this account."""
    return read_marker(root, alias).added_at


def write_marker(root: Path, alias: str, *, added_at: float, codex_home: Path | None) -> None:
    private_dir(root)
    private_dir(codex_dir(root))
    document: dict[str, object] = {
        "schema": store.SCHEMA_VERSION,
        "alias": alias,
        "kind": KIND_CODEX,
        "added_at": added_at,
    }
    if codex_home is not None:
        document["codex_home"] = str(codex_home)
    write_private(codex_path(root, alias), json.dumps(document, indent=2) + "\n")


def delete_marker(root: Path, alias: str) -> None:
    path = codex_path(root, alias)
    if not path.exists():
        raise NotFoundError(f'there is no account named "{alias}"', "vibe list")
    path.unlink(missing_ok=True)
    store.usage_path(root, alias).unlink(missing_ok=True)


def rename_marker(root: Path, old: str, new: str) -> None:
    marker = read_marker(root, old)
    write_marker(root, new, added_at=marker.added_at, codex_home=marker.codex_home)
    delete_marker(root, old)


# --- the CLI's login, read only --------------------------------------------------------


@dataclass(frozen=True)
class CodexLogin:
    """What ``auth.json`` says. ``access_token`` is None for an API-key login or a half file."""

    auth_mode: str | None
    email: str | None
    plan: str | None
    account_id: str | None
    access_token: Secret | None
    # The access token's own ``exp`` claim, epoch seconds; None when it names none.
    expires_at: float | None
    has_api_key: bool = False

    @property
    def usable(self) -> bool:
        return self.access_token is not None


class CodexAuthPort(Protocol):
    def read(self, home: Path | None) -> CodexLogin | None:
        """The CLI's login in ``home`` (default: `default_home`), or None when it has none."""
        ...


def _claims(jwt: object) -> dict[str, object]:
    """A JWT's payload, unverified: used to show who it is and when it ends, nothing else."""
    if not isinstance(jwt, str):
        return {}
    parts = jwt.split(".")
    if len(parts) != 3 or not parts[1]:
        return {}
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        parsed: object = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_auth(document: object) -> CodexLogin | None:
    """``auth.json``: ``{auth_mode, OPENAI_API_KEY, tokens: {id_token, access_token, ...}}``."""
    if not isinstance(document, dict):
        return None
    mode = _text(document.get("auth_mode"))
    api_key = _text(document.get("OPENAI_API_KEY"))
    tokens = document.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    access = _text(tokens.get("access_token"))
    ident = _claims(tokens.get("id_token"))
    auth = ident.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    profile = ident.get("https://api.openai.com/profile")
    profile = profile if isinstance(profile, dict) else {}
    email = _text(ident.get("email")) or _text(profile.get("email"))
    exp = _claims(access).get("exp") if access is not None else None
    expires = float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None
    if mode is None:
        # Older files have no auth_mode: tokens mean ChatGPT, a bare key means an API key.
        mode = AUTH_MODE_CHATGPT if access is not None else AUTH_MODE_API_KEY if api_key else None
    if access is None and api_key is None:
        return None
    return CodexLogin(
        auth_mode=mode,
        email=email,
        plan=_text(auth.get("chatgpt_plan_type")),
        account_id=_text(tokens.get("account_id")) or _text(auth.get("chatgpt_account_id")),
        access_token=None if access is None else Secret(access),
        expires_at=expires,
        has_api_key=api_key is not None,
    )


class FileAuth:
    """`$CODEX_HOME/auth.json`, read only. Nothing here is written, logged or put in a message."""

    def read(self, home: Path | None) -> CodexLogin | None:
        path = (home if home is not None else default_home()) / "auth.json"
        try:
            with path.open("rb") as handle:
                data = handle.read(_MAX_AUTH_BYTES)
            parsed: object = json.loads(data)
        except (OSError, ValueError):
            return None
        return parse_auth(parsed)


def require_login(port: CodexAuthPort, home: Path | None) -> CodexLogin:
    login = port.read(home)
    if login is None:
        raise UsageError(f"no Codex login found ({STORE_HINT})", LOGIN_CMD)
    return login
