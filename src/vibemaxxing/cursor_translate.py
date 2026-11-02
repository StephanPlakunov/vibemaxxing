"""A Claude Code command line, as a `cursor-agent` one.

`vibe run cursor -- claude <args>` and a `vibe run auto` that picks Cursor both come here.
The rule is the one in the README: every Claude flag either has a mapping below or is
refused by name. Nothing is silently dropped, because a dropped `--permission-mode` or
`--allowedTools` is a run with a different safety posture than the one that was asked for.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from vibemaxxing.cursor import DEFAULT_EFFORT, ModelEntry, resolve_model
from vibemaxxing.errors import UsageError

# Claude Code's own commands; `claude mcp ...` is not a prompt and has no Cursor twin.
CLAUDE_SUBCOMMANDS: Final = frozenset(
    {
        "agents",
        "attach",
        "auth",
        "auto-mode",
        "doctor",
        "gateway",
        "import",
        "install",
        "logs",
        "mcp",
        "plugin",
        "plugins",
        "purge",
        "respawn",
        "rm",
        "setup-token",
        "stop",
        "kill",
        "ultrareview",
        "update",
        "upgrade",
    }
)
_PERMISSION_NOTHING: Final = frozenset({"default", "manual"})
_OUTPUT_FORMATS: Final = ("text", "json", "stream-json")
_RESUME_FLAGS: Final = ("-r", "--resume")
_CONTINUE_FLAGS: Final = ("-c", "--continue")


@dataclass(frozen=True)
class Options:
    """What the caller allows and knows; everything else comes from the Claude arguments."""

    models: Sequence[ModelEntry]
    # The model and effort Claude Code would run without a flag (env, settings), or None.
    default_model: str | None = None
    default_effort: str | None = None
    # True when the run declared --needs: headless Cursor then needs --approve-mcps to use
    # the plugin MCP servers (Slack, Atlassian) without a prompt nobody can answer.
    needs_mcp: bool = False
    allow_force: bool = False
    allow_fast: bool = False
    allow_no_zdr: bool = False
    is_cursor_chat: Callable[[str], bool] = lambda _: False


@dataclass(frozen=True)
class Translation:
    args: tuple[str, ...]  # the cursor-agent arguments, without the program
    interactive: bool
    model: str


def refuse(flag: str, why: str) -> UsageError:
    return UsageError(
        f"Claude flag {flag} has no Cursor equivalent{(': ' + why) if why else ''}; "
        "vibe will not drop it silently",
        "vibe run <account> -- claude <args without it>",
    )


def _split_eq(token: str) -> tuple[str, str | None]:
    if token.startswith("--") and "=" in token:
        flag, value = token.split("=", 1)
        return flag, value
    return token, None


def strip_resume(args: Sequence[str]) -> tuple[list[str], bool]:
    """``args`` without `--resume [id]`, `-r [id]` and `--continue`; and whether any was there.

    For a run moved to another kind of account: a Claude session id cannot be resumed on Cursor,
    nor a Cursor chat id by Claude, so the move is a fresh run and says so.
    """
    kept: list[str] = []
    removed = False
    index = 0
    while index < len(args):
        token = args[index]
        flag, value = _split_eq(token)
        if token == "--":
            kept.extend(args[index:])
            break
        if flag in _CONTINUE_FLAGS:
            removed = True
        elif flag in _RESUME_FLAGS:
            removed = True
            nxt = args[index + 1] if index + 1 < len(args) else None
            if value is None and nxt is not None and not nxt.startswith("-"):
                index += 1  # the id
        else:
            kept.append(token)
        index += 1
    return kept, removed


def resume_id(args: Sequence[str]) -> str | None:
    """The id given to `--resume`/`-r`, if there is one."""
    for index, token in enumerate(args):
        if token == "--":
            return None
        flag, value = _split_eq(token)
        if flag in _RESUME_FLAGS:
            if value is not None:
                return value
            nxt = args[index + 1] if index + 1 < len(args) else None
            return nxt if nxt is not None and not nxt.startswith("-") else None
    return None


def is_not_a_session(args: Sequence[str]) -> bool:
    """`claude --version`, `claude --help`, `claude mcp ...`: Claude Code's own commands."""
    for token in args:
        if token == "--":
            return False
        if token in ("-v", "--version", "-h", "--help"):
            return True
        if not token.startswith("-"):
            return token in CLAUDE_SUBCOMMANDS
    return False


def is_claude_command(command: Sequence[str]) -> bool:
    return bool(command) and re.split(r"[\\/]", command[0])[-1] == "claude"


def translate(claude_args: Sequence[str], options: Options) -> Translation:
    """The `cursor-agent` arguments for ``claude <claude_args>``; UsageError for what has none."""
    print_mode = False
    output_format: str | None = None
    model: str | None = None
    effort: str | None = None
    permission: str | None = None
    skip_permissions = False
    resume: str | None = None
    extra: list[str] = []
    prompt: list[str] = []
    partial = False
    args = list(claude_args)
    index = 0

    def take(flag: str, inline: str | None) -> str:
        nonlocal index
        if inline is not None:
            return inline
        index += 1
        if index >= len(args) or args[index].startswith("-"):
            raise UsageError(f"Claude flag {flag} needs a value", "vibe run --help")
        return args[index]

    while index < len(args):
        token = args[index]
        if token == "--":
            prompt.extend(args[index + 1 :])
            break
        if not token.startswith("-") or token == "-":
            if not prompt and token in CLAUDE_SUBCOMMANDS:
                raise refuse(f"`claude {token}`", "it is a Claude Code command, not a prompt")
            prompt.append(token)
            index += 1
            continue
        flag, inline = _split_eq(token)
        if flag in ("-p", "--print"):
            print_mode = True
        elif flag == "--output-format":
            output_format = take(flag, inline)
        elif flag == "--model":
            model = take(flag, inline)
        elif flag == "--effort":
            effort = take(flag, inline)
        elif flag == "--permission-mode":
            permission = take(flag, inline)
        elif flag == "--dangerously-skip-permissions":
            skip_permissions = True
        elif flag in _RESUME_FLAGS:
            nxt = args[index + 1] if index + 1 < len(args) else None
            if inline is None and (nxt is None or nxt.startswith("-")):
                raise refuse(
                    flag, "Claude's session picker lists Claude sessions, not Cursor chats"
                )
            resume = take(flag, inline)
        elif flag in _CONTINUE_FLAGS:
            raise refuse(flag, "it would continue a Cursor chat, not this Claude conversation")
        elif flag == "--include-partial-messages":
            partial = True
        elif flag == "--add-dir":
            # Claude takes every following non-flag word as a directory.
            if inline is not None:
                extra += ["--add-dir", inline]
            else:
                taken = 0
                while index + 1 < len(args) and not args[index + 1].startswith("-"):
                    index += 1
                    extra += ["--add-dir", args[index]]
                    taken += 1
                if not taken:
                    raise UsageError("Claude flag --add-dir needs a directory", "vibe run --help")
        else:
            raise refuse(flag, _why(flag))
        index += 1

    if output_format is not None:
        if output_format not in _OUTPUT_FORMATS:
            raise UsageError(f"--output-format {output_format!r} is not text, json or stream-json")
        if not print_mode:
            raise UsageError("--output-format only works with -p/--print", "vibe run --help")
    if partial and not (print_mode and output_format == "stream-json"):
        raise UsageError("--include-partial-messages needs -p and --output-format stream-json")

    out: list[str] = []
    if print_mode:
        out.append("-p")
    if output_format is not None:
        out += ["--output-format", output_format]
    if partial:
        out.append("--stream-partial-output")

    if resume is not None:
        if not options.is_cursor_chat(resume):
            raise refuse(
                f"--resume {resume}",
                "that is not a Cursor chat id, and a Claude session cannot be resumed on Cursor "
                "(start a fresh run with the original brief instead)",
            )
        out += ["--resume", resume]

    if skip_permissions or permission == "bypassPermissions":
        if not options.allow_force:
            raise refuse(
                "--dangerously-skip-permissions" if skip_permissions else "bypassPermissions",
                "it would need cursor-agent --force; pass --allow-force to vibe run to allow it",
            )
        out.append("--force")
    elif permission == "auto":
        out.append("--auto-review")
    elif permission == "plan":
        out.append("--plan")
    elif permission is not None and permission not in _PERMISSION_NOTHING:
        raise refuse(f"--permission-mode {permission}", "only auto, plan and default are mapped")

    chosen = model if model is not None else options.default_model
    if chosen is None:
        raise UsageError(
            "cannot tell which Claude model this run would use (no --model, no ANTHROPIC_MODEL, "
            "no `model` in settings)",
            "vibe run cursor -- claude --model sonnet ...",
        )
    level = effort or options.default_effort or DEFAULT_EFFORT
    cursor_model = resolve_model(
        chosen,
        level,
        options.models,
        allow_fast=options.allow_fast,
        allow_no_zdr=options.allow_no_zdr,
    )
    out += ["--model", cursor_model]
    if options.needs_mcp and print_mode:
        out.append("--approve-mcps")
    out += extra
    if prompt:
        out += ["--", *prompt] if any(word.startswith("-") for word in prompt) else prompt
    return Translation(tuple(out), interactive=not print_mode, model=cursor_model)


_WHY: Final = {
    "--allowedTools": "tool permissions are Claude Code's; Cursor has its own in cli-config.json",
    "--allowed-tools": "tool permissions are Claude Code's; Cursor has its own in cli-config.json",
    "--disallowedTools": "tool permissions are Claude Code's",
    "--disallowed-tools": "tool permissions are Claude Code's",
    "--mcp-config": "Cursor reads its own .cursor/mcp.json",
    "--strict-mcp-config": "Cursor reads its own .cursor/mcp.json",
    "--settings": "Cursor does not read Claude Code settings",
    "--system-prompt": "cursor-agent has no system prompt flag",
    "--append-system-prompt": "cursor-agent has no system prompt flag",
    "--agents": "Claude subagents do not exist in Cursor",
    "--plugin-dir": "Claude plugins are not Cursor plugins",
    "--session-id": "Cursor assigns its own chat ids",
    "--fork-session": "there is no Claude session to fork on Cursor",
    "--input-format": "cursor-agent has no stream input",
}


def _why(flag: str) -> str:
    return _WHY.get(flag, "")
