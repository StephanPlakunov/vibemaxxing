"""The user's own OAuth login to an upstream MCP server, and the token that comes of it.

The bridge (bridge.py) talks straight to a vendor's own remote MCP server as an MCP client, so it
needs that vendor's access token, obtained the way the MCP authorization spec says: protected
resource metadata, authorization server metadata, dynamic client registration when the server
offers it, authorization code with PKCE in the user's browser, token exchange, refresh.

Where a token lives, and where it never does:

* the access token, the refresh token and a client secret are three Keychain items under
  vibe's own service names (``vibemaxxing-bridge-<upstream>-access`` ...). Never a file, a log, an
  error message or a command line: the Keychain tool takes the value on its standard input;
* what is not secret (client id, expiry, scopes, endpoints) is ``~/.vibemaxxing/bridge/<up>.json``;
* in memory a token is a ``Secret``, and is revealed only to build an Authorization header or a
  token-endpoint request body.

Every endpoint an upstream's metadata names is checked against that upstream's own host list before
anything is sent to it, so a poisoned discovery document cannot send a token to another host.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import http.server
import json
import math
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from vibemaxxing import jsonutil, oauth
from vibemaxxing.errors import DeadlineError, NeedsLoginError, NetworkError, UsageError, VibeError
from vibemaxxing.fsutil import private_dir, write_private
from vibemaxxing.httpclient import HttpClient, HTTPError
from vibemaxxing.redact import Secret, scrub

SERVICE_PREFIX: Final = "vibemaxxing-bridge"
KEYCHAIN_ACCOUNT: Final = "vibemaxxing"
SECURITY_BIN: Final = "/usr/bin/security"
SECURITY_TIMEOUT_S: Final = 10.0
SECURITY_ABSENT_CODE: Final = 44
# What a token may be made of to go through the Keychain tool unquoted (JWTs, xoxp-..., opaque ids).
_SAFE_SECRET: Final = re.compile(r"[A-Za-z0-9._~+/=-]{8,3500}")
REFRESH_SKEW_S: Final = 60.0
LOGIN_TIMEOUT_S: Final = 300.0
DEFAULT_PORT: Final = 8765
SCHEMA_VERSION: Final = 1


@dataclass(frozen=True)
class Upstream:
    """One vendor's remote MCP server and how to log in to it. The allowlist is in bridge.py."""

    id: str
    name: str
    mcp_url: str
    # The protected-resource metadata document (RFC 9728); where a server serves it is not uniform.
    prm_url: str
    # Every host this upstream's own endpoints may be on. Nothing else is ever contacted.
    hosts: frozenset[str]
    # "dcr": register a public client on the fly; "preregistered": the user brings a client id
    # (and secret) from an app of their own, because the server has no dynamic registration.
    registration: str
    read_scopes: tuple[str, ...]
    # Whether to send the RFC 8707 `resource` parameter.
    send_resource: bool = True


# --- the Keychain, as a port --------------------------------------------------------------------


class SecretStore(Protocol):
    def read(self, service: str) -> str | None: ...

    def write(self, service: str, value: str) -> None: ...

    def delete(self, service: str) -> None: ...


class Runner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        input: str | None,
        capture_output: bool,
        text: bool,
        encoding: str,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


class MacSecretStore:
    """The macOS Keychain through `/usr/bin/security`, a value never on the command line.

    A write sends ``add-generic-password ... -w <value>`` to `security -i` on its standard input.
    (The Keychain write in keychain.py puts a credential in argv; this one does not.) The value is
    read back and compared, because `security -i` silently truncates a line that is too long.
    """

    def __init__(self, runner: Runner = subprocess.run) -> None:
        self._run = runner

    def _call(
        self, argv: Sequence[str], stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self._run(
                list(argv),
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=SECURITY_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
            raise VibeError(
                f"could not run {SECURITY_BIN}: {exc.__class__.__name__}", "vibe bridge status"
            ) from None

    def read(self, service: str) -> str | None:
        done = self._call(
            [SECURITY_BIN, "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service, "-w"]
        )
        if done.returncode == SECURITY_ABSENT_CODE:
            return None
        if done.returncode != 0:
            raise VibeError(
                f"the macOS Keychain refused to read {service!r} (security exit {done.returncode})",
                "vibe bridge status",
            )
        return done.stdout[:-1] if done.stdout.endswith("\n") else done.stdout

    def write(self, service: str, value: str) -> None:
        if not _SAFE_SECRET.fullmatch(value):
            raise VibeError(
                "a token has characters or a length vibe will not hand to the Keychain tool",
                "vibe bridge login",
            )
        command = f"add-generic-password -U -a {KEYCHAIN_ACCOUNT} -s {service} -w {value}\n"
        done = self._call([SECURITY_BIN, "-i"], stdin=command)
        if done.returncode != 0 or self.read(service) != value:
            # Nothing of the command or the value goes into the message.
            raise VibeError(
                f"the macOS Keychain did not keep {service!r} intact (security exit "
                f"{done.returncode})",
                "vibe bridge login",
            )

    def delete(self, service: str) -> None:
        done = self._call(
            [SECURITY_BIN, "delete-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service]
        )
        if done.returncode not in (0, SECURITY_ABSENT_CODE):
            raise VibeError(
                f"the macOS Keychain refused to delete {service!r} "
                f"(security exit {done.returncode})",
                "vibe bridge status",
            )


def default_secret_store() -> SecretStore:
    platform: str = sys.platform
    if platform != "darwin":
        raise VibeError(
            "the bridge keeps its tokens in the macOS Keychain, which is not here", "vibe"
        )
    return MacSecretStore()


class LazySecrets:
    """The Keychain, created on first use: a platform without it fails only when it is needed."""

    def __init__(self) -> None:
        self._store: SecretStore | None = None

    def _get(self) -> SecretStore:
        if self._store is None:
            self._store = default_secret_store()
        return self._store

    def read(self, service: str) -> str | None:
        return self._get().read(service)

    def write(self, service: str, value: str) -> None:
        self._get().write(service, value)

    def delete(self, service: str) -> None:
        self._get().delete(service)


# --- what is stored -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Tokens:
    access: Secret
    refresh: Secret | None
    expires_at: float | None
    client_id: str
    client_secret: Secret | None
    scope: str
    token_endpoint: str
    resource: str | None


def service(upstream: Upstream, part: str) -> str:
    return f"{SERVICE_PREFIX}-{upstream.id}-{part}"


def meta_path(root: Path, upstream: Upstream) -> Path:
    return root / "bridge" / f"{upstream.id}.json"


def _recovery(upstream: Upstream) -> str:
    return f"vibe bridge login {upstream.id}"


def _finite(value: object) -> float | None:
    """A JSON number as a finite float; None for a bool, a string, NaN, infinity or an overflow."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def has_login(root: Path, upstream: Upstream) -> bool:
    """Whether a login is on record (the description exists). No Keychain read, no request."""
    return meta_path(root, upstream).is_file()


def load(root: Path, secrets: SecretStore, upstream: Upstream) -> Tokens | None:
    """The stored login, or None when there is none or it is incomplete."""
    try:
        parsed: object = jsonutil.loads(meta_path(root, upstream).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict) or parsed.get("schema") != SCHEMA_VERSION:
        return None
    access = secrets.read(service(upstream, "access"))
    client_id, endpoint = parsed.get("client_id"), parsed.get("token_endpoint")
    if not access or not isinstance(client_id, str) or not isinstance(endpoint, str):
        return None
    refresh = secrets.read(service(upstream, "refresh"))
    client_secret = secrets.read(service(upstream, "client-secret"))
    expires, scope, resource = parsed.get("expires_at"), parsed.get("scope"), parsed.get("resource")
    return Tokens(
        access=Secret(access),
        refresh=Secret(refresh) if refresh else None,
        expires_at=_finite(expires),
        client_id=client_id,
        client_secret=Secret(client_secret) if client_secret else None,
        scope=scope if isinstance(scope, str) else "",
        token_endpoint=endpoint,
        resource=resource if isinstance(resource, str) else None,
    )


def save(root: Path, secrets: SecretStore, upstream: Upstream, tokens: Tokens) -> None:
    """Secrets to the Keychain, the refresh token FIRST, the non-secret description last.

    A kill between two writes must never cost a browser login. The server has already rotated the
    refresh token by the time this runs, so the new one goes in before anything else (a stale access
    token beside a fresh refresh token heals itself on the next 401); the access token follows, the
    client secret never changes, and the metadata, which only describes them, is last.
    """
    # A permitted reveal() site (CONTRACT section 5): a token on its way into the Keychain tool's
    # standard input.
    if tokens.refresh is not None:
        secrets.write(service(upstream, "refresh"), tokens.refresh.reveal())
    else:
        secrets.delete(service(upstream, "refresh"))
    secrets.write(service(upstream, "access"), tokens.access.reveal())
    if tokens.client_secret is not None:
        secrets.write(service(upstream, "client-secret"), tokens.client_secret.reveal())
    private_dir(root)
    private_dir(root / "bridge")
    write_private(
        meta_path(root, upstream),
        json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "client_id": tokens.client_id,
                "expires_at": tokens.expires_at,
                "scope": tokens.scope,
                "token_endpoint": tokens.token_endpoint,
                "resource": tokens.resource,
            },
            indent=2,
        )
        + "\n",
    )


def forget(root: Path, secrets: SecretStore, upstream: Upstream) -> bool:
    """Remove the login. True if there was one."""
    had = (
        meta_path(root, upstream).exists() or secrets.read(service(upstream, "access")) is not None
    )
    for part in ("access", "refresh", "client-secret"):
        secrets.delete(service(upstream, part))
    meta_path(root, upstream).unlink(missing_ok=True)
    return had


# --- discovery ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthServer:
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    resource: str | None
    scopes_supported: tuple[str, ...]


def _host_ok(upstream: Upstream, url: str) -> bool:
    # urlsplit and urllib read a backslash or userinfo differently: refuse both outright, and any
    # port but the default, so the host that was checked is the host that is contacted.
    if "\\" in url or any(ord(char) < 33 for char in url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname in upstream.hosts
        and parts.username is None
        and parts.password is None
        and port in (None, 443)
    )


def _checked(upstream: Upstream, url: object, what: str) -> str:
    if not isinstance(url, str) or not _host_ok(upstream, url):
        raise UsageError(
            f"{upstream.name}'s {what} is not on a host this bridge trusts for it",
            _recovery(upstream),
        )
    return url


def _get_json(client: HttpClient, url: str) -> dict[str, object] | None:
    try:
        return client.request(
            "GET", url, headers={"Accept": "application/json"}, timeout_s=15.0
        ).json()
    except HTTPError as exc:
        if exc.status == 404:
            return None
        raise NetworkError(f"{url} answered HTTP {exc.status}", "vibe bridge status") from None


def _strings(raw: object) -> tuple[str, ...]:
    return tuple(item for item in raw if isinstance(item, str)) if isinstance(raw, list) else ()


def discover(client: HttpClient, upstream: Upstream) -> AuthServer:
    """Protected-resource metadata, then the authorization server's, both host-checked."""
    _checked(upstream, upstream.prm_url, "resource metadata URL")
    prm = _get_json(client, upstream.prm_url)
    if prm is None:
        raise NetworkError(f"{upstream.name} serves no resource metadata", "vibe bridge status")
    servers = _strings(prm.get("authorization_servers"))
    if not servers:
        raise NetworkError(
            f"{upstream.name}'s metadata names no authorization server", "vibe bridge status"
        )
    issuer = _checked(upstream, servers[0], "authorization server")
    parts = urllib.parse.urlsplit(issuer)
    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    # RFC 8414 puts the well-known segment between host and path; some servers append it instead.
    candidates = [
        f"{base}/.well-known/oauth-authorization-server{path}",
        f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server",
    ]
    meta: dict[str, object] | None = None
    for url in dict.fromkeys(candidates):
        meta = _get_json(client, url)
        if meta is not None:
            break
    if meta is None:
        raise NetworkError(
            f"{upstream.name}'s authorization server serves no metadata", "vibe bridge status"
        )
    registration = meta.get("registration_endpoint")
    resource = prm.get("resource")
    return AuthServer(
        authorization_endpoint=_checked(
            upstream, meta.get("authorization_endpoint"), "authorize URL"
        ),
        token_endpoint=_checked(upstream, meta.get("token_endpoint"), "token URL"),
        registration_endpoint=None
        if registration is None
        else _checked(upstream, registration, "registration URL"),
        resource=resource if isinstance(resource, str) else None,
        scopes_supported=_strings(meta.get("scopes_supported"))
        or _strings(prm.get("scopes_supported")),
    )


# --- login --------------------------------------------------------------------------------------


class CodeReceiver(Protocol):
    redirect_uri: str

    def wait(self, state: str, timeout_s: float) -> str:
        """The authorization code the browser was redirected with; VibeError otherwise."""
        ...

    def close(self) -> None: ...


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _Server
    # A connection that sends nothing is dropped after this long, and each connection has a thread
    # of its own, so an idle one can neither block the real redirect nor the listener's close.
    timeout = 5

    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != "/callback":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        query = urllib.parse.parse_qs(parts.query)
        self.server.arrive({key: values[0] for key, values in query.items() if values})
        body = b"vibe bridge: you can close this tab and go back to the terminal.\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return  # the redirect carries a code: nothing of it is logged


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, port: int) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self._arrivals: list[dict[str, str]] = []
        self._changed = threading.Condition()

    def arrive(self, result: dict[str, str]) -> None:
        with self._changed:
            self._arrivals.append(result)
            self._changed.notify_all()

    def next_arrival(self, wait_until: float) -> dict[str, str] | None:
        with self._changed:
            while not self._arrivals:
                left = wait_until - time.monotonic()
                if left <= 0:
                    return None
                self._changed.wait(left)
            return self._arrivals.pop(0)


class LoopbackReceiver:
    """A one-shot HTTP listener on 127.0.0.1 for the browser's redirect (RFC 8252).

    Only an answer that carries this login's state counts. Anything else that reaches the port (a
    stray request, a probe, another page) is answered and ignored: it cannot end the login.
    """

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        try:
            self._server = _Server(port)
        except OSError:
            raise UsageError(
                f"port {port} on 127.0.0.1 is busy, and the redirect URL needs one that is free",
                "vibe bridge login <upstream> --port <free port>",
            ) from None
        self.redirect_uri = f"http://127.0.0.1:{self._server.server_address[1]}/callback"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def wait(self, state: str, timeout_s: float) -> str:
        until = time.monotonic() + timeout_s
        while True:
            result = self._server.next_arrival(until)
            if result is None:
                raise UsageError(
                    "no answer from the browser in time", "vibe bridge login <upstream>"
                )
            if result.get("state") != state:
                continue  # not this login's: ignored, and the wait goes on
            if "error" in result:
                raise UsageError(
                    f"the upstream refused the login: {scrub(result['error'])[:80]}",
                    "vibe bridge login",
                )
            code = result.get("code")
            if not code:
                raise UsageError("the browser's answer carried no code", "vibe bridge login")
            return code

    def close(self) -> None:
        self._server.shutdown()  # the accept loop only: handler threads are daemons
        self._server.server_close()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def register_client(
    client: HttpClient, upstream: Upstream, server: AuthServer, redirect_uri: str
) -> str:
    """Dynamic client registration (RFC 7591) as a public client; the new client id."""
    if server.registration_endpoint is None:
        raise UsageError(
            f"{upstream.name} offers no dynamic client registration: log in with --client-id of "
            "an app "
            "of your own",
            _recovery(upstream) + " --client-id <id>",
        )
    body = json.dumps(
        {
            "client_name": "vibemaxxing bridge",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
    ).encode()
    try:
        answer = client.request(
            "POST",
            server.registration_endpoint,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            body=body,
            timeout_s=15.0,
        ).json()
    except HTTPError as exc:
        raise NetworkError(
            f"{upstream.name} refused the client registration (HTTP {exc.status})",
            _recovery(upstream),
        ) from None
    client_id = answer.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise NetworkError(
            f"{upstream.name}'s registration answer has no client id", _recovery(upstream)
        )
    return client_id


def authorize_url(
    upstream: Upstream,
    server: AuthServer,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    verifier: str,
    scopes: Sequence[str],
) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
    }
    if scopes:
        query["scope"] = " ".join(scopes)
    if upstream.send_resource and server.resource:
        query["resource"] = server.resource
    return f"{server.authorization_endpoint}?{urllib.parse.urlencode(query)}"


def _token_request(
    client: HttpClient,
    endpoint: str,
    form: Mapping[str, str],
    upstream: Upstream,
    deadline: float | None = None,
) -> Mapping[str, object]:
    try:
        answer = client.request(
            "POST",
            endpoint,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            body=urllib.parse.urlencode(form).encode(),
            timeout_s=20.0,
            deadline=deadline,
        ).json()
    except HTTPError as exc:
        error = _error_code(exc.body)
        # Only the OAuth error code, never the body: a token endpoint may echo what it was sent.
        raise NeedsLoginError(
            f"{upstream.name} refused the token request (HTTP {exc.status}"
            f"{', ' + error if error else ''})",
            _recovery(upstream),
        ) from None
    if answer.get("ok") is False or "error" in answer:
        code = answer.get("error")
        raise NeedsLoginError(
            f"{upstream.name} refused the token request "
            f"({code if isinstance(code, str) else 'error'})"[:120],
            _recovery(upstream),
        )
    return answer


def _error_code(body: bytes) -> str:
    try:
        parsed: object = jsonutil.loads(body)
    except ValueError:
        return ""
    code = parsed.get("error") if isinstance(parsed, dict) else None
    return code[:40] if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", code) else ""


DEFAULT_LIFETIME_S: Final = 3600.0
MAX_LIFETIME_S: Final = 30 * 86_400.0
# A scope that can change something: write, delete, manage, admin, chat (Slack's chat:write), ...
_WRITE_SCOPE: Final = re.compile(
    r"(?:^|[:._-])(?:write|delete|manage|admin|modify|chat|post|send|upload|create|edit)(?:$|[:._-])",
    re.IGNORECASE,
)


def write_scopes(scopes: Sequence[str]) -> list[str]:
    return [scope for scope in scopes if _WRITE_SCOPE.search(scope)]


def _refuse_write_scope(granted: object, *, client_id: str) -> None:
    """A login that can write is never stored: the token would sit in a Keychain item that any
    process of the user (the extra session's shell included) may be able to read."""
    if not isinstance(granted, str):
        return
    found = write_scopes([part for part in re.split(r"[\s,]+", granted) if part])
    if found:
        raise UsageError(
            f"the upstream granted a scope that can write ({', '.join(found)[:80]}): the bridge "
            "stores read-only logins only, so nothing was stored",
            "vibe bridge login <upstream>   (and remove the write scopes from the app)",
        )


def _lifetime(value: object) -> float:
    """Seconds a token lives, from an untrusted ``expires_in``: finite, positive, at most 30 days.

    A bool, a string, NaN, infinity, a number too large for a float, zero or a negative number
    is "an hour": short enough to be refreshed soon, never an instant that is already past.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_LIFETIME_S
    if isinstance(value, int):
        return float(min(value, int(MAX_LIFETIME_S))) if value > 0 else DEFAULT_LIFETIME_S
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_LIFETIME_S
    return min(value, MAX_LIFETIME_S)


def _tokens_from(
    answer: Mapping[str, object],
    *,
    now_s: float,
    client_id: str,
    client_secret: Secret | None,
    endpoint: str,
    resource: str | None,
    previous: Tokens | None,
) -> Tokens:
    access = answer.get("access_token")
    if not isinstance(access, str) or not access:
        raise NeedsLoginError("the token answer has no access token", "vibe bridge login")
    refresh = answer.get("refresh_token")
    scope = answer.get("scope")
    _refuse_write_scope(scope, client_id=client_id)
    return Tokens(
        access=Secret(access),
        # A refresh answer may leave the refresh token out: then the old one is still the one.
        refresh=Secret(refresh)
        if isinstance(refresh, str) and refresh
        else (previous.refresh if previous else None),
        expires_at=now_s + _lifetime(answer.get("expires_in")),
        client_id=client_id,
        client_secret=client_secret,
        scope=scope if isinstance(scope, str) else (previous.scope if previous else ""),
        token_endpoint=endpoint,
        resource=resource,
    )


def login(
    upstream: Upstream,
    *,
    client: HttpClient,
    secrets: SecretStore,
    root: Path,
    receiver: CodeReceiver,
    open_browser: Callable[[str], object],
    say: Callable[[str], None],
    now_s: float,
    client_id: str | None = None,
    client_secret: Secret | None = None,
) -> Tokens:
    """The whole login: discovery, a client, the browser, the code, the token, the Keychain."""
    server = discover(client, upstream)
    redirect_uri = receiver.redirect_uri
    if upstream.registration == "preregistered":
        if not client_id or client_secret is None:
            raise UsageError(
                f"{upstream.name} has no dynamic client registration: it needs the client id and "
                "secret of a Slack app of your own (internal app, redirect URL "
                f"{redirect_uri})",
                _recovery(upstream) + " --client-id <id>",
            )
        used_id = client_id
    else:
        used_id = client_id or register_client(client, upstream, server, redirect_uri)
    scopes = list(upstream.read_scopes)
    verifier, state = oauth.new_verifier(), oauth.new_state()
    url = authorize_url(
        upstream,
        server,
        client_id=used_id,
        redirect_uri=redirect_uri,
        state=state,
        verifier=verifier,
        scopes=scopes,
    )
    say(
        f"open this to log in to {upstream.name} (it asks only for: "
        f"{', '.join(scopes) or 'its default scopes'}):\n\n{url}\n"
    )
    open_browser(url)
    code = receiver.wait(state, LOGIN_TIMEOUT_S)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": used_id,
        "code_verifier": verifier,
    }
    if upstream.send_resource and server.resource:
        form["resource"] = server.resource
    if client_secret is not None:
        # A permitted reveal() site: the user's own app secret, to its token endpoint.
        form["client_secret"] = client_secret.reveal()
    answer = _token_request(client, server.token_endpoint, form, upstream)
    tokens = _tokens_from(
        answer,
        now_s=now_s,
        client_id=used_id,
        client_secret=client_secret,
        endpoint=server.token_endpoint,
        resource=server.resource,
        previous=None,
    )
    save(root, secrets, upstream, tokens)
    return tokens


# --- keeping the token usable -------------------------------------------------------------------


@contextmanager
def _refresh_lock(root: Path, upstream: Upstream, deadline: float | None) -> Iterator[None]:
    """One refresh at a time across every bridge process: a refresh rotates the refresh token.

    Waited for only until ``deadline``: a call that has timed out must not start a refresh, or a
    request, after the session was told it failed.
    """
    private_dir(root)
    private_dir(root / "locks")
    path = root / "locks" / f"bridge-{upstream.id}.lock"
    with path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise DeadlineError(
                        f"the {upstream.name} login is being refreshed by another process too long",
                        "vibe bridge status",
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def refresh_tokens(
    upstream: Upstream,
    *,
    client: HttpClient,
    secrets: SecretStore,
    root: Path,
    now_s: float,
    force: bool = False,
    deadline: float | None = None,
) -> Tokens:
    """A usable token: the stored one, or a refreshed one written before it is used.

    Under a lock, re-reading what is stored first: another bridge process may have refreshed while
    this one waited, and spending the old refresh token again would log the user out.
    """
    tokens = load(root, secrets, upstream)
    if tokens is None:
        raise NeedsLoginError(f"not logged in to {upstream.name}", _recovery(upstream))
    if not force and not _stale(tokens, now_s):
        return tokens
    with _refresh_lock(root, upstream, deadline):
        tokens = load(root, secrets, upstream)
        if tokens is None:
            raise NeedsLoginError(f"not logged in to {upstream.name}", _recovery(upstream))
        if not force and not _stale(tokens, now_s):
            return tokens
        if tokens.refresh is None:
            raise NeedsLoginError(f"the {upstream.name} login has expired", _recovery(upstream))
        # A permitted reveal() site: the refresh token, to its own token endpoint.
        form = {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh.reveal(),
            "client_id": tokens.client_id,
        }
        if tokens.resource and upstream.send_resource:
            form["resource"] = tokens.resource
        if tokens.client_secret is not None:
            form["client_secret"] = tokens.client_secret.reveal()
        if not _host_ok(upstream, tokens.token_endpoint):
            raise UsageError(
                f"the stored {upstream.name} token endpoint is not trusted", _recovery(upstream)
            )
        answer = _token_request(client, tokens.token_endpoint, form, upstream, deadline)
        fresh = _tokens_from(
            answer,
            now_s=now_s,
            client_id=tokens.client_id,
            client_secret=tokens.client_secret,
            endpoint=tokens.token_endpoint,
            resource=tokens.resource,
            previous=tokens,
        )
        save(root, secrets, upstream, fresh)  # before anything uses it
        return fresh


def _stale(tokens: Tokens, now_s: float) -> bool:
    return tokens.expires_at is not None and tokens.expires_at - REFRESH_SKEW_S <= now_s
