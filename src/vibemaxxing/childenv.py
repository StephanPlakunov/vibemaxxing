"""The environment of every child vibe starts: built on purpose, never inherited whole.

A session vibe started on one account carries that account's ``CLAUDE_CODE_OAUTH_TOKEN`` (and
often an API key or config dir). A child that inherits it either runs as the wrong account or,
for Cursor's agent, hands a Claude credential to a third party's shell tool. So the child gets an
explicit environment: the parent's, minus the credentials that decide an account, minus (for a
child that is not Claude Code) anything secret-looking the person did not pass on purpose, plus
exactly what the chosen account needs. Only names are ever reported, never values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from vibemaxxing.errors import UsageError

TOKEN_VAR: Final = "CLAUDE_CODE_OAUTH_TOKEN"
# What decides which Claude account a Claude Code process is, or which provider it talks to.
CLAUDE_FAMILY: Final = frozenset(
    {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    }
)
SECRET_SUFFIXES: Final = (
    "_TOKEN",
    "_API_KEY",
    "_APIKEY",
    "_SECRET",
    "_SECRET_KEY",
    "_ACCESS_KEY",
    "_PASSWORD",
    "_PASSWD",
)


def looks_secret(name: str) -> bool:
    upper = name.upper()
    return upper in CLAUDE_FAMILY or upper.endswith(SECRET_SUFFIXES)


@dataclass(frozen=True)
class ChildEnv:
    env: dict[str, str]
    # Names (never values) taken out of the parent's environment.
    dropped: tuple[str, ...]


def parent_carries_claude_account(environ: Mapping[str, str]) -> bool:
    """Whether this process was started inside a Claude Code session pinned to an account."""
    return TOKEN_VAR in environ or "CLAUDE_CONFIG_DIR" in environ


def build(
    environ: Mapping[str, str],
    *,
    strict: bool,
    pass_env: Iterable[str] = (),
    set_vars: Mapping[str, str] | None = None,
) -> ChildEnv:
    """The child's environment.

    ``strict`` (a child that is not Claude Code, or a nested run with no business holding the
    caller's secrets): drop the Claude family and every name that looks like a secret, except the
    names in ``pass_env``. Not strict (Claude Code on a chosen account): drop the Claude family
    only; the person's other variables are theirs. ``set_vars`` is applied last, so whatever the
    account needs is set explicitly and never left to what was inherited.
    """
    passed = {name for name in pass_env}
    blocked = sorted(name for name in passed if name.upper() in CLAUDE_FAMILY)
    if strict and blocked:
        raise UsageError(
            f"--pass-env {', '.join(blocked)}: a Claude credential is never passed to this child",
            "vibe run --help",
        )
    env: dict[str, str] = {}
    dropped: list[str] = []
    for name, value in environ.items():
        drop = name.upper() in CLAUDE_FAMILY or (
            strict and looks_secret(name) and name not in passed
        )
        if drop:
            dropped.append(name)
        else:
            env[name] = value
    env.update(set_vars or {})
    return ChildEnv(env, tuple(sorted(dropped)))
