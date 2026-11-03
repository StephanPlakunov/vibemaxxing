"""Test doubles for the Cursor account kind.

`install_fake_agent` puts a script named `cursor-agent` on a PATH that holds nothing else
cursor-shaped, so no test can reach the real agent, its login or its usage. The script answers
`status` and `models` from files the test wrote and records the argv of any other call.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from vibemaxxing.cursor import AgentStatus, ModelEntry, parse_models
from vibemaxxing.redact import Secret

MODELS_TEXT = """Available models

auto - Auto (default)
gpt-5.2 - GPT-5.2
claude-opus-5-5-low - Claude Opus 5.5 1M Low
claude-opus-5-5-low-fast - Claude Opus 5.5 1M Low Fast
claude-opus-5-5-medium - Claude Opus 5.5 1M
claude-opus-5-5-medium-fast - Claude Opus 5.5 1M Fast
claude-opus-5-5-high - Claude Opus 5.5 1M High
claude-opus-5-5-high-fast - Claude Opus 5.5 1M High Fast
claude-opus-5-5-xhigh - Claude Opus 5.5 1M Extra High
claude-opus-5-5-max - Claude Opus 5.5 1M Max
claude-opus-5-medium - Claude Opus 5 1M Medium
claude-opus-5-thinking-high - Claude Opus 5 1M Thinking
claude-opus-4-8-medium - Claude Opus 4.8 1M Medium
claude-opus-4-8-high - Claude Opus 4.8 1M
claude-fable-5-1-medium - Claude Fable 5.1 1M Medium (NO ZDR)
claude-fable-5-1-high - Claude Fable 5.1 1M (NO ZDR)
claude-sonnet-5-5-low - Claude Sonnet 5.5  Low
claude-sonnet-5-5-medium - Claude Sonnet 5.5  Medium
claude-sonnet-5-5-high - Claude Sonnet 5.5  High
claude-sonnet-5-5-max - Claude Sonnet 5.5  Max
claude-sonnet-5-medium - Claude Sonnet 5 1M Medium
claude-haiku-5-5-low - Claude Haiku 5.5  Low No Thinking
claude-haiku-5-5-medium - Claude Haiku 5.5  Medium No Thinking
claude-haiku-5-5-thinking-low - Claude Haiku 5.5  Low
"""
MODELS: tuple[ModelEntry, ...] = parse_models(MODELS_TEXT)

_SCRIPT = """#!{python}
import json, os, sys
state = json.load(open({state!r}))
args = sys.argv[1:]
if args[:1] == ["status"]:
    print(json.dumps(state["status"]))
    sys.exit(0 if state["status"].get("isAuthenticated") else 1)
if args[:1] == ["models"]:
    sys.stdout.write(state["models"])
    sys.exit(0)
with open(state["record"], "a") as handle:
    handle.write(json.dumps(args) + "\\n")
with open(state["record"] + ".env", "a") as handle:
    handle.write(json.dumps(dict(os.environ)) + "\\n")
print(json.dumps({{"result": json.dumps({{"from": "cursor"}})}}))
sys.exit(state.get("exit", 0))
"""


@dataclass
class FakeAgent:
    """A fake `cursor-agent` binary and what it saw."""

    home: Path
    bin_dir: Path
    state_path: Path
    record: Path

    def calls(self) -> list[list[str]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def envs(self) -> list[dict[str, str]]:
        """The environment each cursor-agent run was started with."""
        path = Path(str(self.record) + ".env")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def set_state(self, **changes: object) -> None:
        state = json.loads(self.state_path.read_text())
        state.update(changes)
        self.state_path.write_text(json.dumps(state))

    def make_chat(self, chat_id: str) -> None:
        (self.home / ".cursor" / "chats" / "workspacehash" / chat_id).mkdir(
            parents=True, exist_ok=True
        )


def install_fake_agent(
    tmp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    logged_in: bool = True,
    models_text: str = MODELS_TEXT,
    exit_code: int = 0,
) -> FakeAgent:
    bin_dir = tmp_home / "fakebin"
    bin_dir.mkdir()
    state_path = tmp_home / "fake-agent-state.json"
    record = tmp_home / "fake-agent-calls.jsonl"
    status: dict[str, object] = (
        {
            "isAuthenticated": True,
            "userInfo": {"email": "dev@example.com", "teamName": "Zvoove"},
        }
        if logged_in
        else {"isAuthenticated": False}
    )
    state_path.write_text(
        json.dumps(
            {"status": status, "models": models_text, "record": str(record), "exit": exit_code}
        )
    )
    script = bin_dir / "cursor-agent"
    script.write_text(_SCRIPT.format(python=sys.executable, state=str(state_path)))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    # Nothing but the fake dir and the system basics: the real ~/.local/bin is not on it.
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return FakeAgent(tmp_home, bin_dir, state_path, record)


@dataclass
class StubAgent:
    """An AgentPort that never starts a process."""

    logged_in: bool = True
    installed: bool = True
    models_text: str = MODELS_TEXT
    chats: frozenset[str] = frozenset()
    calls: list[str] = field(default_factory=list)

    def binary(self) -> str:
        if not self.installed:
            from vibemaxxing.errors import UsageError

            raise UsageError("cursor-agent is not installed (or not on PATH)", "install it")
        return "/fake/cursor-agent"

    def status(self) -> AgentStatus:
        self.calls.append("status")
        return (
            AgentStatus(self.logged_in, "dev@example.com", "Zvoove")
            if self.logged_in
            else (AgentStatus(False, None, None))
        )

    def models(self) -> tuple[ModelEntry, ...]:
        self.calls.append("models")
        return parse_models(self.models_text)

    def chat_exists(self, chat_id: str) -> bool:
        return chat_id in self.chats


@dataclass
class StubToken:
    """A CursorTokenPort holding a fake token."""

    value: str | None = "CURSORTOKEN0000000000"

    def read(self) -> Secret | None:
        return None if self.value is None else Secret(self.value)


def usage_answer(
    *,
    percent: float = 12.0,
    on_demand_used: int = 0,
    on_demand_limit: int = 25_000,
    cycle_end_ms: int = 1_762_209_900_000,
) -> dict[str, object]:
    """What `GetCurrentPeriodUsage` answers, in the shape cursor-agent's own `/usage` reads."""
    return {
        "billingCycleStart": str(cycle_end_ms - 30 * 86_400_000),
        "billingCycleEnd": str(cycle_end_ms),
        "planUsage": {"limit": 2000, "includedSpend": 240, "totalPercentUsed": percent},
        "spendLimitUsage": {
            "limitType": "team",
            "individualLimit": on_demand_limit,
            "individualUsed": on_demand_used,
        },
    }


def json_text(value: Mapping[str, object]) -> str:
    return json.dumps(value)


_CLAUDE_SCRIPT = """#!__PYTHON__
import json, os, sys, time
argv = sys.argv[1:]
seen = {
    "argv": argv,
    "has_token": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
    "has_config_dir": "CLAUDE_CONFIG_DIR" in os.environ,
    "cwd": os.getcwd(),
    "env": dict(os.environ),
}
with open("__RECORD__", "a") as handle:
    handle.write(json.dumps(seen) + "\\n")
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
if "stream-json" in argv:
    # A probe: print the init event the way Claude Code does. Everything the connectors offer
    # (a recorded real listing) minus what --disallowedTools denies; denied tools are not offered.
    offered = json.load(open("__TOOLS__"))["tools"]
    flag = "--disallowedTools"
    denied = set(argv[argv.index(flag) + 1].split(",")) if flag in argv else set()
    extra = os.environ.get("FAKE_CLAUDE_EXTRA_TOOLS", "")
    tools = [t for t in offered if t not in denied] + [t for t in extra.split(",") if t]
    if os.environ.get("FAKE_CLAUDE_NO_PROFILE"):
        tools = [t for t in tools if not t.endswith("slack_read_user_profile")]
    servers = sorted({t.split("__")[1] for t in offered})
    counter = "__RECORD__.pending"
    pending = 0
    if os.environ.get("FAKE_CLAUDE_PENDING"):
        done = int(open(counter).read()) if os.path.exists(counter) else 0
        open(counter, "w").write(str(done + 1))
        pending = 1 if done < int(os.environ["FAKE_CLAUDE_PENDING"]) else 0
    slack = "claude_ai_Slack"
    status = {s: ("pending" if pending and s == slack else "connected") for s in servers}
    if pending:
        tools = [t for t in tools if "claude_ai_Slack" not in t]
    mode = os.environ.get("FAKE_CLAUDE_MODE", "default")
    listed = [{"name": s, "status": status[s]} for s in servers]
    print(json.dumps({"type": "system", "subtype": "init", "permissionMode": mode,
                      "tools": tools, "mcp_servers": listed}))
    print(json.dumps({"type": "result", "result": "ok"}))
    sys.exit(0)
print(json.dumps({"result": json.dumps({"from": "claude"})}))
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
"""


def install_fake_claude(fake: FakeAgent) -> Path:
    """A `claude` next to the fake cursor-agent: records its argv and whether a token was set."""
    record = fake.home / "fake-claude-calls.jsonl"
    script = fake.bin_dir / "claude"
    tools = Path(__file__).with_name("fixtures") / "init_tools_2026-11-03.json"
    text = (
        _CLAUDE_SCRIPT.replace("__PYTHON__", sys.executable)
        .replace("__RECORD__", str(record))
        .replace("__TOOLS__", str(tools))
    )
    script.write_text(text)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return record
