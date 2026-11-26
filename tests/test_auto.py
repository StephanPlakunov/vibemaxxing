"""`vibe auto`: the tick, its status, and the launchd plist. vibe never switches by itself.

A tick is exercised end to end against the shared fakes. The usage caches under
`usage/<alias>.json` are seeded fresh, which is exactly what a real tick finds a
minute after the last one, so most cases make no HTTP request at all and the
ones that do count them.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient, FakeKeychain, make_credential, seed_account
from vibemaxxing import auto, cli, store
from vibemaxxing.credentials import Identity, credential_to_blob

NOW_S = 1_757_930_000.0
ADDED_AT = 1_757_930_000.0  # tests.fakes.seed_account's default


def _iso(at_s: float) -> str:
    return datetime.fromtimestamp(at_s, UTC).isoformat()


def payload(
    session: int, weekly: int, *, now: float = NOW_S, scoped: int | None = None
) -> dict[str, object]:
    limits: list[dict[str, object]] = [
        {
            "kind": "session",
            "percent": session,
            "severity": "normal",
            "resets_at": _iso(now + 3 * 3600),
        },
        {
            "kind": "weekly_all",
            "percent": weekly,
            "severity": "normal",
            "resets_at": _iso(now + 2 * 86400),
        },
    ]
    if scoped is not None:
        limits.append(
            {
                "kind": "weekly_scoped",
                "percent": scoped,
                "severity": "normal",
                "resets_at": _iso(now + 2 * 86400),
                "scope": {"model": {"display_name": "Fable"}},
            }
        )
    return {"limits": limits}


def cache(
    root: Path, alias: str, body: dict[str, object], *, fetched_at: float = NOW_S - 10.0
) -> None:
    store.write_usage_cache(
        root,
        alias,
        store.UsageCache(ADDED_AT, fetched_at, body, retry_at=None, failures=0, message=None),
    )


@dataclass
class World:
    root: Path
    port: FakeKeychain
    client: FakeHttpClient = field(default_factory=FakeHttpClient)

    def context(self) -> cli.Context:
        return cli.Context(
            root=self.root,
            client=self.client,
            port=self.port,
            now_s=NOW_S,
            clock=lambda: NOW_S,
            prompt=lambda _: "",
            browser=lambda _: True,
        )

    def tick(self, now_s: float = NOW_S) -> auto.TickResult:
        return auto.tick(self.root, client=self.client, port=self.port, now_s=now_s)


def world(
    tmp_home: Path,
    usage_by_alias: dict[str, tuple[int, int]],
    *,
    live: str = "a",
    seed_cache: bool = True,
) -> World:
    """Every alias stored with uuid u<alias> and a fresh cache; `live` is what Claude Code runs."""
    root = tmp_home / ".vibemaxxing"
    for alias, (session, weekly) in usage_by_alias.items():
        seed_account(
            root,
            alias,
            identity=Identity(None, f"u{alias}", None, None, None, None, None),
            credential=make_credential(access=f"{alias}-file", refresh=f"{alias}-rt"),
            active=alias == live,
        )
        if seed_cache:
            cache(root, alias, payload(session, weekly))
    (tmp_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": f"u{live}"}})
    )
    blob = credential_to_blob(
        make_credential(access=f"{live}-live", refresh=f"{live}-live-rt"),
        base={"mcpOAuth": {"kept": True}},
    )
    return World(root, FakeKeychain(blob=blob))


# --- one tick -----------------------------------------------------------------


def test_a_tick_never_switches_however_full_the_live_account_is(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world(tmp_home, {"a": (100, 100), "b": (0, 0)})
    before = w.port.blob

    assert cli.main(["auto", "tick"], context=w.context()) == 0

    assert capsys.readouterr().out == "live a\n"
    assert store.read_active(w.root) == "a" and w.port.blob == before
    assert w.client.requests == [], "the fresh caches answered; no request was needed"
    assert set(w.port.log) <= {"keychain.read"}, "a tick writes no login"
    assert not (w.root / "auto.json").exists()


def test_a_tick_ignores_the_switch_settings_an_old_file_still_has(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (100, 100), "b": (0, 0)})
    old = {
        "schema": 1,
        "auto_switch": True,
        "threshold": 50,
        "cooldown_s": 0,
        "excluded": [],
        "policy": {"mode": "ordered", "steps": [{"account": "b", "until": 99}]},
        "routing": {"shared_login": True, "order": {}, "roles": {}, "spent_percent": 99},
    }
    (w.root / "settings.json").write_text(json.dumps(old))
    before = w.port.blob

    assert w.tick().line == "live a"
    assert store.read_active(w.root) == "a" and w.port.blob == before


def test_a_tick_keeps_the_usage_numbers_fresh(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (100, 100), "b": (0, 0)}, seed_cache=False)
    w.client.queue_json(200, payload(41, 42))
    w.client.queue_json(200, payload(7, 8))

    assert w.tick().line == "live a"

    assert len(w.client.requests) == 2
    a, b = store.read_usage_cache(w.root, "a"), store.read_usage_cache(w.root, "b")
    assert a is not None and a.payload == payload(41, 42)
    assert b is not None and b.payload == payload(7, 8)


def test_a_tick_asks_nothing_when_the_pointer_is_off_the_live_account(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (9, 9), "b": (9, 9)}, live="b", seed_cache=False)
    store.write_active(w.root, "a")

    line = w.tick().line

    assert line == (
        "Claude Code runs as b but vibe's active account is a: no numbers fetched"
        " - run: vibe switch b"
    )
    assert w.client.requests == [] and store.read_active(w.root) == "a"


def test_a_tick_fetches_nothing_when_it_cannot_tell_who_is_live(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (100, 100), "b": (0, 0)}, seed_cache=False)
    (tmp_home / ".claude.json").unlink()

    assert w.tick().line == "live account unknown: no numbers fetched"
    assert w.client.requests == []


def test_a_tick_leaves_the_status_file_behind(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (10, 20), "b": (30, 40)})

    w.tick()

    status = json.loads((w.root / "status.json").read_text())
    assert (status["live"], status["active"]) == ("a", "a")
    assert [entry["alias"] for entry in status["accounts"]] == ["a", "b"]
    assert "policy" not in status and "last_switch" not in status


# --- status and JSON --------------------------------------------------------------


def test_status_says_who_is_live_and_fetches_nothing(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world(tmp_home, {"a": (10, 10), "b": (40, 40)}, seed_cache=False)

    assert cli.main(["auto", "status"], context=w.context()) == 0
    assert capsys.readouterr().out == "live         a\n"

    assert cli.main(["auto", "status", "--json"], context=w.context()) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["live"], out["active"]) == ("a", "a")
    assert not {"enabled", "threshold", "next", "last_switch", "policy"} & set(out)
    assert w.client.requests == []


def test_status_says_how_to_repair_a_stale_pointer(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world(tmp_home, {"a": (10, 10), "b": (99, 99)}, live="b")
    store.write_active(w.root, "a")

    assert cli.main(["auto", "status"], context=w.context()) == 0

    out = capsys.readouterr().out
    assert out == "live         b  (vibe's active account is a - run: vibe switch b)\n"
    assert w.client.requests == []


def test_status_with_nothing_known_is_still_an_answer(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_home / ".vibemaxxing"
    ctx = cli.Context(
        root=root,
        client=FakeHttpClient(),
        port=FakeKeychain(),
        now_s=NOW_S,
        prompt=lambda _: "",
        browser=lambda _: True,
    )

    assert cli.main(["auto", "status", "--json"], context=ctx) == 0

    out = json.loads(capsys.readouterr().out)
    assert (out["live"], out["active"]) == (None, None)


def test_every_auto_command_speaks_json(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    _no_subprocess(monkeypatch)
    w = world(tmp_home, {"a": (10, 10), "b": (40, 40)})

    for command in ("tick", "status", "install", "uninstall"):
        capsys.readouterr()
        assert cli.main(["auto", command, "--json"], context=w.context()) == 0, command
        out = json.loads(capsys.readouterr().out)
        assert (out["schema"], out["ok"], out["action"]) == (1, True, f"auto {command}")


@pytest.mark.parametrize("command", ["on", "off", "undo", "exclude", "include"])
def test_the_switching_commands_are_gone(
    tmp_home: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world(tmp_home, {"a": (10, 10)})

    with pytest.raises(SystemExit) as exc:
        cli.main(["auto", command], context=w.context())

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


# --- launchd ------------------------------------------------------------------


def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    seen: list[object] = []

    def refuse(*args: object, **kwargs: object) -> None:
        seen.append(args)
        raise AssertionError(f"a subprocess was started: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    return seen


def test_auto_install_writes_plist_only(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    seen = _no_subprocess(monkeypatch)
    w = world(tmp_home, {"a": (10, 10)})

    assert cli.main(["auto", "install"], context=w.context()) == 0

    plist = tmp_home / "Library" / "LaunchAgents" / "de.intra-ai.vibemaxxing-auto.plist"
    assert plist.exists()
    body = plistlib.loads(plist.read_bytes())
    assert body["StartInterval"] == 60
    assert body["Label"] == "de.intra-ai.vibemaxxing-auto"
    assert body["ProgramArguments"][-2:] == ["auto", "tick"]
    assert body["ProgramArguments"][0] == sys.executable
    assert seen == [], "install must not run launchctl; the operator loads the job"
    out = capsys.readouterr().out
    assert "launchctl bootstrap gui/" in out
    assert str(plist) in out


def test_auto_uninstall_removes_the_plist_and_names_the_unload(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    seen = _no_subprocess(monkeypatch)
    w = world(tmp_home, {"a": (10, 10)})
    assert cli.main(["auto", "install"], context=w.context()) == 0
    capsys.readouterr()

    assert cli.main(["auto", "uninstall"], context=w.context()) == 0

    assert not (
        tmp_home / "Library" / "LaunchAgents" / "de.intra-ai.vibemaxxing-auto.plist"
    ).exists()
    assert "launchctl bootout gui/" in capsys.readouterr().out
    assert seen == []
    assert cli.main(["auto", "uninstall"], context=w.context()) == 0, "already gone is not an error"


def test_install_is_macos_only(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    w = world(tmp_home, {"a": (10, 10)})

    for command in ("install", "uninstall"):
        assert cli.main(["auto", command], context=w.context()) == 2
        assert "macOS" in capsys.readouterr().err
    assert not (tmp_home / "Library").exists()
