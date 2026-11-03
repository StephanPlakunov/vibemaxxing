"""Claude Code arguments as cursor-agent arguments: every mapping, and every refusal by name."""

from __future__ import annotations

import pytest

from tests.cursor_fakes import MODELS
from vibemaxxing import cursor_translate
from vibemaxxing.cursor import resolve_model
from vibemaxxing.cursor_translate import Options, Translation, translate
from vibemaxxing.errors import UsageError

CHAT = "8ecd5bf4-84eb-4551-a928-e0f5e14044d0"


def options(**kw: object) -> Options:
    base: dict[str, object] = {
        "models": MODELS,
        "default_model": "sonnet",
        "is_cursor_chat": lambda chat: chat == CHAT,
    }
    return Options(**{**base, **kw})  # type: ignore[arg-type]


def run(*args: str, **kw: object) -> Translation:
    return translate(list(args), options(**kw))


# --- the mappings ---------------------------------------------------------------------


def test_print_and_a_prompt() -> None:
    result = run("-p", "fix the bug")

    assert result.args == ("-p", "--model", "claude-sonnet-5-5-medium", "fix the bug")
    assert not result.interactive


@pytest.mark.parametrize("fmt", ["text", "json", "stream-json"])
def test_output_format_is_passed_through(fmt: str) -> None:
    assert run("-p", "--output-format", fmt, "x").args[:3] == ("-p", "--output-format", fmt)
    assert run("--print", f"--output-format={fmt}", "x").args[:3] == ("-p", "--output-format", fmt)


def test_output_format_needs_print_and_a_known_value() -> None:
    with pytest.raises(UsageError, match="only works with -p"):
        run("--output-format", "json")
    with pytest.raises(UsageError, match="not text, json or stream-json"):
        run("-p", "--output-format", "xml")


def test_partial_messages_map_to_stream_partial_output() -> None:
    result = run("-p", "--output-format", "stream-json", "--include-partial-messages", "x")

    assert "--stream-partial-output" in result.args
    with pytest.raises(UsageError, match="stream-json"):
        run("-p", "--include-partial-messages", "x")


def test_interactive_claude_is_interactive_cursor() -> None:
    result = run()

    assert result.interactive
    assert result.args == ("--model", "claude-sonnet-5-5-medium")
    assert run("start here").interactive


def test_permission_mode_auto_is_auto_review_and_never_force() -> None:
    result = run("-p", "--permission-mode", "auto", "x")

    assert "--auto-review" in result.args
    assert "--force" not in result.args and "--yolo" not in result.args


@pytest.mark.parametrize("mode", ["default", "manual"])
def test_permission_mode_default_adds_nothing(mode: str) -> None:
    assert run("-p", "--permission-mode", mode, "x").args == run("-p", "x").args


def test_permission_mode_plan_is_plan_mode() -> None:
    assert "--plan" in run("-p", "--permission-mode=plan", "x").args


@pytest.mark.parametrize(
    "argv",
    [
        ("--permission-mode", "bypassPermissions"),
        ("--dangerously-skip-permissions",),
    ],
)
def test_bypass_is_refused_unless_force_is_allowed(argv: tuple[str, ...]) -> None:
    with pytest.raises(UsageError, match="--allow-force"):
        run("-p", *argv, "x")

    assert "--force" in run("-p", *argv, "x", allow_force=True).args


@pytest.mark.parametrize("mode", ["acceptEdits", "dontAsk"])
def test_other_permission_modes_are_refused_by_name(mode: str) -> None:
    with pytest.raises(UsageError, match=mode):
        run("-p", "--permission-mode", mode, "x")


def test_add_dir_takes_every_following_directory() -> None:
    result = run("-p", "--add-dir", "../a", "../b", "--", "go")

    assert result.args.count("--add-dir") == 2
    assert result.args[-1] == "go"


# --- the model ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("sonnet", None, "claude-sonnet-5-5-medium"),
        ("opus", None, "claude-opus-5-5-medium"),
        ("haiku", "low", "claude-haiku-5-5-low"),
        ("claude-opus-5-5", None, "claude-opus-5-5-medium"),
        ("claude-opus-5-5-20260101", "high", "claude-opus-5-5-high"),
        ("claude-opus-4-8", "high", "claude-opus-4-8-high"),
        ("claude-opus-5", None, "claude-opus-5-medium"),
        ("opus[1m]", "max", "claude-opus-5-5-max"),
        ("claude-sonnet-5-5", "xhigh", None),
    ],
)
def test_model_and_effort_become_one_cursor_model(
    model: str, effort: str | None, expected: str | None
) -> None:
    flags = ["--model", model] + (["--effort", effort] if effort else [])
    if expected is None:
        with pytest.raises(UsageError, match="claude-sonnet-5-5"):
            run("-p", *flags, "x")
        return
    assert run("-p", *flags, "x").model == expected


def test_default_model_and_effort_come_from_the_caller() -> None:
    result = run("-p", "x", default_model="opus", default_effort="high")

    assert result.model == "claude-opus-5-5-high"


def test_an_explicit_model_beats_the_default() -> None:
    assert (
        run("-p", "--model", "haiku", "x", default_model="opus").model == "claude-haiku-5-5-medium"
    )


def test_no_model_anywhere_is_refused() -> None:
    with pytest.raises(UsageError, match="cannot tell which Claude model"):
        run("-p", "x", default_model=None)


def test_a_fast_variant_is_never_picked_unless_allowed() -> None:
    # "-fast" siblings exist for opus 5.5 medium; the plain one wins, and fast needs the flag.
    assert not run("-p", "--model", "opus", "x").model.endswith("-fast")
    only_fast = [m for m in MODELS if m.id == "claude-opus-5-5-medium-fast"]
    with pytest.raises(UsageError, match="fast"):
        resolve_model("opus", "medium", only_fast)
    assert (
        resolve_model("opus", "medium", only_fast, allow_fast=True) == "claude-opus-5-5-medium-fast"
    )


def test_a_no_zdr_model_is_never_picked_unless_allowed() -> None:
    with pytest.raises(UsageError, match="NO ZDR"):
        run("-p", "--model", "fable", "x")
    with pytest.raises(UsageError, match="NO ZDR"):
        run("-p", "--model", "claude-fable-5-1", "x")

    assert run("-p", "--model", "fable", "x", allow_no_zdr=True).model == "claude-fable-5-1-medium"


def test_an_unlisted_model_is_refused_with_the_closest_names() -> None:
    with pytest.raises(UsageError) as caught:
        run("-p", "--model", "claude-opus-9-9", "x")

    assert "no Cursor model for claude-opus-9-9" in caught.value.message
    assert "claude-opus-5-5" in caught.value.message  # a close name, from the live list

    with pytest.raises(UsageError, match="not a Claude model name"):
        run("-p", "--model", "gpt-5", "x")
    with pytest.raises(UsageError, match="effort"):
        run("-p", "--effort", "ludicrous", "x")


def test_the_live_list_decides_not_a_hard_coded_one() -> None:
    newer = [m for m in MODELS if m.id != "claude-sonnet-5-5-medium"]

    with pytest.raises(UsageError):
        translate(["-p", "--model", "claude-sonnet-5-5", "x"], options(models=newer))


# --- resume ---------------------------------------------------------------------------


def test_resume_passes_a_cursor_chat_id() -> None:
    result = run("-p", "--resume", CHAT, "go on")

    assert result.args[:3] == ("-p", "--resume", CHAT)
    assert "go on" in result.args


def test_resume_refuses_a_claude_session_id() -> None:
    claude_session = "0a1b2c3d-1111-2222-3333-444455556666"

    with pytest.raises(UsageError) as caught:
        run("-p", "-r", claude_session, "x")

    assert "not a Cursor chat id" in caught.value.message
    assert "Claude session cannot be resumed on Cursor" in caught.value.message


def test_resume_without_an_id_and_continue_are_refused() -> None:
    with pytest.raises(UsageError, match="session picker"):
        run("--resume")
    with pytest.raises(UsageError, match="--continue"):
        run("-p", "--continue", "x")
    with pytest.raises(UsageError, match="-c"):
        run("-p", "-c", "x")


def test_strip_resume_removes_every_form() -> None:
    assert cursor_translate.strip_resume(["-p", "--resume", "abc", "go"]) == (["-p", "go"], True)
    assert cursor_translate.strip_resume(["--resume=abc", "go"]) == (["go"], True)
    assert cursor_translate.strip_resume(["-c", "-p", "go"]) == (["-p", "go"], True)
    assert cursor_translate.strip_resume(["-p", "go"]) == (["-p", "go"], False)
    assert cursor_translate.strip_resume(["--", "--resume"]) == (["--", "--resume"], False)
    assert cursor_translate.resume_id(["-p", "--resume", "abc"]) == "abc"
    assert cursor_translate.resume_id(["-p"]) is None


# --- refusals --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag",
    [
        "--allowedTools",
        "--disallowedTools",
        "--mcp-config",
        "--system-prompt",
        "--append-system-prompt",
        "--agents",
        "--plugin-dir",
        "--session-id",
        "--settings",
        "--max-budget-usd",
        "--json-schema",
        "--bare",
        "--verbose",
        "--chrome",
        "--name",
        "--fallback-model",
        "--totally-new-flag",
    ],
)
def test_any_unmapped_flag_is_refused_and_named(flag: str) -> None:
    with pytest.raises(UsageError) as caught:
        run("-p", flag, "x")

    assert flag in caught.value.message
    assert "has no Cursor equivalent" in caught.value.message
    assert "silently" in caught.value.message


def test_claude_subcommands_are_refused() -> None:
    with pytest.raises(UsageError, match="claude mcp"):
        run("mcp", "list")


def test_a_flag_that_needs_a_value_says_so() -> None:
    with pytest.raises(UsageError, match="needs a value"):
        run("--model")


def test_a_prompt_that_looks_like_a_flag_survives_after_the_separator() -> None:
    assert run("-p", "--", "--not-a-flag").args[-2:] == ("--", "--not-a-flag")


# --- connectors ------------------------------------------------------------------------


def test_approve_mcps_only_for_headless_runs_that_declared_needs() -> None:
    assert "--approve-mcps" in run("-p", "x", needs_mcp=True).args
    assert "--approve-mcps" not in run("-p", "x").args
    assert "--approve-mcps" not in run("x", needs_mcp=True).args  # interactive: asked in the TUI


def test_what_counts_as_not_a_session() -> None:
    assert cursor_translate.is_not_a_session(["--version"])
    assert cursor_translate.is_not_a_session(["mcp", "list"])
    assert cursor_translate.is_not_a_session(["-h"])
    assert not cursor_translate.is_not_a_session(["-p", "hello"])
    assert not cursor_translate.is_not_a_session([])
    assert not cursor_translate.is_not_a_session(["--", "mcp"])
    assert cursor_translate.is_claude_command(["claude", "-p"])
    assert cursor_translate.is_claude_command(["/usr/local/bin/claude"])
    assert not cursor_translate.is_claude_command(["ls"])


def test_trust_workspace_adds_trust_only_to_headless_runs() -> None:
    assert "--trust" in run("-p", "x", trust_workspace=True).args
    assert "--trust" not in run("-p", "x").args
    assert "--trust" not in run("x", trust_workspace=True).args  # interactive asks in the TUI
