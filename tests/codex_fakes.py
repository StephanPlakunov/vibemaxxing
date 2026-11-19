"""Test doubles for the Codex account kind: fake JWTs, a fake login port, a recorded answer.

Nothing here is a real token or a real answer. The answer's shape is the one the Codex CLI's
own backend client reads (openai/codex ``codex-rs/codex-backend-openapi-models``:
``RateLimitStatusPayload``, ``RateLimitWindowSnapshot``); the values are made up.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path

from vibemaxxing.codex import CodexLogin, parse_auth
from vibemaxxing.redact import Secret

FAKE_ACCESS = "CODEXACCESS0000000000"


def fake_jwt(claims: dict[str, object]) -> str:
    def part(value: object) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.c2ln"


def auth_document(
    *,
    email: str = "dev@example.com",
    plan: str = "plus",
    account_id: str = "acct-123",
    exp: float = 1_757_930_000.0 + 3600,
    mode: str | None = "chatgpt",
) -> dict[str, object]:
    """What ``codex login`` writes to auth.json for a ChatGPT login, with fake tokens."""
    document: dict[str, object] = {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": fake_jwt(
                {
                    "email": email,
                    "https://api.openai.com/auth": {
                        "chatgpt_plan_type": plan,
                        "chatgpt_account_id": account_id,
                    },
                }
            ),
            "access_token": fake_jwt({"exp": exp, "jti": FAKE_ACCESS}),
            "refresh_token": "CODEXREFRESH000000000",
            "account_id": account_id,
        },
        "last_refresh": "2026-11-19T08:00:00Z",
    }
    if mode is not None:
        document["auth_mode"] = mode
    return document


def api_key_document() -> dict[str, object]:
    return {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-fake0000000000000000"}


def write_auth(home: Path, document: object) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "auth.json"
    path.write_text(json.dumps(document))
    return path


@dataclass
class StubAuth:
    """A CodexAuthPort that reads no file."""

    document: object | None = field(default_factory=auth_document)
    reads: list[Path | None] = field(default_factory=list)

    def read(self, home: Path | None) -> CodexLogin | None:
        self.reads.append(home)
        return None if self.document is None else parse_auth(self.document)


def login(**kw: object) -> CodexLogin:
    base: dict[str, object] = {
        "auth_mode": "chatgpt",
        "email": "dev@example.com",
        "plan": "plus",
        "account_id": "acct-123",
        "access_token": Secret(FAKE_ACCESS),
        "expires_at": None,
    }
    return CodexLogin(**{**base, **kw})  # type: ignore[arg-type]


def usage_answer(
    *,
    session_percent: int = 12,
    weekly_percent: int = 34,
    session_reset_at: int = 1_757_930_000 + 3 * 3600,
    weekly_reset_at: int = 1_757_930_000 + 4 * 86_400,
    limit_reached: bool = False,
) -> dict[str, object]:
    """The `/wham/usage` answer: a 5 hour and a weekly window, as the Codex CLI reads it."""
    return {
        "plan_type": "plus",
        "rate_limit": {
            "allowed": not limit_reached,
            "limit_reached": limit_reached,
            "primary_window": {
                "used_percent": session_percent,
                "limit_window_seconds": 18_000,
                "reset_after_seconds": 10_800,
                "reset_at": session_reset_at,
            },
            "secondary_window": {
                "used_percent": weekly_percent,
                "limit_window_seconds": 604_800,
                "reset_after_seconds": 345_600,
                "reset_at": weekly_reset_at,
            },
        },
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        "additional_rate_limits": None,
    }
