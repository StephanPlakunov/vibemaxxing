"""`vibe release ALIAS`: a config dir's login moves into vibe's store, and the dir keeps none.

The inverse of `--adopt-stored-login`, for one shared login: once vibe holds extra, auto-switch can
put it into Claude Code's default login. One login must only ever live in one place, so the dir's
own Keychain item is deleted, and a failed delete puts the account file back. Fakes only: a fake
Keychain per dir, a fake process listing, no request, no real login.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from tests.fakes import FakeKeychain, make_credential, seed_account
from tests.test_run_cursor import NOW_S, World
from vibemaxxing import cli, keychain, oauth, release, store
from vibemaxxing.credentials import Credential, Identity, credential_to_blob, parse_blob
from vibemaxxing.errors import VibeError
from vibemaxxing.keychain import FileKeychain, MacKeychain
from vibemaxxing.models import AccountState

NOW_MS = int(NOW_S * 1000)
FUTURE_MS = NOW_MS + 4 * 3600 * 1000
EXTRA = Identity("claude2@intra-ai.de", "uextra", "Intra AI", "org-intra", None, None, "Claude2")
MAIN = Identity("me@intra-ai.de", "umain", None, None, None, None, None)
SECRETS = ("stored-at-0000", "stored-rt-0000", "dir-at-1111", "dir-rt-1111", "main-live-at")


@dataclass
class FakeProcesses:
    """`ps -E` as text, or a failure to read it."""

    text: str = "  1 /sbin/launchd\n  4242 -zsh TERM=xterm HOME=/Users/x\n"
    broken: bool = False
    calls: int = 0

    def listing(self) -> str:
        self.calls += 1
        if self.broken:
            raise VibeError("/bin/ps could not list the processes (exit 1)")
        return self.text


@dataclass
class StubbornKeychain(FakeKeychain):
    """A dir item whose delete is refused, or answers late, or cannot be read back."""

    refuse: bool = False
    late: bool = False
    unreadable_after: bool = False
    deletes: list[str] = field(default_factory=list)

    def read(self) -> str | None:
        if self.unreadable_after and self.deletes:
            raise VibeError("the macOS Keychain refused to read it")
        return super().read()

    def delete(self) -> None:
        self.deletes.append("delete")
        if self.refuse:
            raise VibeError("the macOS Keychain refused to delete it")
        super().delete()
        if self.late:
            raise VibeError("the macOS Keychain answered late")


@pytest.fixture
def world(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    world = World(tmp_home, monkeypatch)
    # main is the live, active login, untouched by everything here.
    seed_account(
        world.root,
        "main",
        identity=MAIN,
        credential=make_credential(access="main-file-at", refresh="main-file-rt"),
        active=True,
    )
    (tmp_home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "umain"}}))
    world.port.blob = credential_to_blob(
        make_credential(access="main-live-at", refresh="main-live-rt")
    )
    return world


def dir_credential(**kw: object) -> Credential:
    base: dict[str, object] = {
        "access": "dir-at-1111",
        "refresh": "dir-rt-1111",
        "expires_at_ms": FUTURE_MS,
        "subscription_type": "team",
    }
    base.update(kw)
    return make_credential(**base)  # type: ignore[arg-type]


def setup(
    world: World,
    *,
    uuid: str = "uextra",
    credential: Credential | None = None,
    port: FakeKeychain | None = None,
    **account: object,
) -> Path:
    """extra, imported from ~/.claude-extra: the stored copy is old, the dir's is Claude Code's."""
    directory = world.home / ".claude-extra"
    directory.mkdir(exist_ok=True)
    (directory / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": uuid}, "mcpServers": {"vibe-bridge": {}}})
    )
    fields: dict[str, object] = {"config_dir": str(directory)}
    fields.update(account)
    seed_account(
        world.root,
        "extra",
        identity=EXTRA,
        credential=make_credential(
            access="stored-at-0000", refresh="stored-rt-0000", expires_at_ms=1_000
        ),
        **fields,  # type: ignore[arg-type]
    )
    item = port if port is not None else FakeKeychain()
    item.blob = credential_to_blob(credential if credential is not None else dir_credential())
    world.dirs[str(directory)] = item
    return directory


def run(world: World, *argv: str, processes: FakeProcesses | None = None) -> int:
    ctx = replace(world.ctx(), processes=processes if processes is not None else FakeProcesses())
    return cli.main(["release", *argv], context=ctx)


def snapshot(world: World, directory: Path) -> dict[str, object]:
    """Every byte the command could have touched."""
    return {
        "accounts": {
            path.name: path.read_text() for path in sorted((world.root / "accounts").glob("*"))
        },
        "active": store.read_active(world.root),
        "default_blob": world.port.blob,
        "home_profile": (world.home / ".claude.json").read_text(),
        "dir_blob": world.dirs[str(directory)].blob,
        "dir_profile": (directory / ".claude.json").read_text(),
    }


# --- the move -------------------------------------------------------------------------------------


def test_the_dirs_login_moves_into_vibe_and_the_dir_keeps_none(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = setup(world)
    store.write_stash(world.root, "extra", make_credential(refresh="stash-rt-old"))
    before = snapshot(world, directory)

    assert run(world, "extra") == 0

    out = capsys.readouterr()
    assert out.out == (
        "released extra: vibe holds its login now; ~/.claude-extra has no login any more\n"
    )
    assert not any(secret in out.out + out.err for secret in SECRETS)
    account = store.read_account(world.root, "extra")
    assert account.config_dir is None
    assert account.credential == dir_credential()  # the dir's login, not the stale stored copy
    assert account.state is AccountState.OK and account.identity == EXTRA
    assert "config_dir" not in json.loads(store.account_path(world.root, "extra").read_text())
    assert world.dirs[str(directory)].blob is None
    assert "keychain.delete" in world.dirs[str(directory)].log
    assert store.read_stash(world.root, "extra") is None  # an old lineage's successor is gone
    after = snapshot(world, directory)
    # Nothing else: the default login, ~/.claude.json, the dir's profile, main, the pointer.
    for key in ("active", "default_blob", "home_profile", "dir_profile"):
        assert after[key] == before[key], key
    assert after["accounts"]["main.json"] == before["accounts"]["main.json"]  # type: ignore[index]
    assert "keychain.write" not in world.port.log and "keychain.delete" not in world.port.log
    assert world.client.requests == []


def test_json_says_what_was_released(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    directory = setup(world)

    assert run(world, "extra", "--json") == 0

    out = json.loads(capsys.readouterr().out)
    assert (out["ok"], out["action"], out["alias"], out["config_dir"]) == (
        True,
        "release",
        "extra",
        str(directory),
    )


def test_after_it_vibe_may_refresh_it_and_switch_to_it(world: World) -> None:
    setup(world, credential=dir_credential(expires_at_ms=1_000))
    assert run(world, "extra") == 0
    world.client.queue_json(
        200, {"access_token": "new-at-2222", "refresh_token": "new-rt-2222", "expires_in": 28800}
    )

    account = store.read_account(world.root, "extra")
    outcome = oauth.refresh(world.root, "extra", account.credential, world.client, now_ms=NOW_MS)
    store.refuse_owned(store.read_account(world.root, "extra"))  # no longer refused

    assert outcome.error is None and outcome.credential is not None
    (request,) = world.client.requests
    assert request.json_body()["refresh_token"] == "dir-rt-1111"  # the dir's lineage, not stale


# --- refusals: nothing changes --------------------------------------------------------------------


def refused(
    world: World, directory: Path, code: int, *, processes: FakeProcesses | None = None
) -> None:
    before = snapshot(world, directory)

    assert run(world, "extra", processes=processes) == code

    assert snapshot(world, directory) == before
    assert world.client.requests == []


@pytest.mark.parametrize("value", ["{dir}", "{dir}/", "{link}", "~/.claude-extra", "relative/dir"])
def test_a_session_running_in_the_dir_is_refused_and_named(
    world: World, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    directory = setup(world)
    link = world.home / "extra-link"
    link.symlink_to(directory)
    setting = value.format(dir=directory, link=link)
    listing = FakeProcesses(
        "  1 /sbin/launchd\n"
        f"  4711 claude --resume abc TERM=xterm CLAUDE_CONFIG_DIR={setting} HOME=/h\n"
        "  4712 claude CLAUDE_CONFIG_DIR=/elsewhere/.claude-work\n"
    )

    refused(world, directory, 2, processes=listing)

    err = capsys.readouterr().err
    assert "4711" in err and "4712" not in err
    assert "Close those sessions" in err and "vibe release extra" in err


def test_a_process_list_that_cannot_be_read_is_refused(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = setup(world)

    refused(world, directory, 2, processes=FakeProcesses(broken=True))

    assert "could not tell whether a Claude Code session runs" in capsys.readouterr().err


def test_a_dir_profile_naming_another_account_is_refused(world: World) -> None:
    directory = setup(world, uuid="usomeone-else")

    refused(world, directory, 2)


def test_a_dir_profile_naming_no_account_is_refused(world: World) -> None:
    directory = setup(world)
    (directory / ".claude.json").write_text("{}")

    refused(world, directory, 2)


@pytest.mark.parametrize("how", ["lapsed", "rejected"])
def test_a_lapsed_or_rejected_login_is_refused(world: World, how: str) -> None:
    if how == "lapsed":
        directory = setup(world, credential=dir_credential(refresh_expires_at_ms=NOW_MS - 1))
    else:
        directory = setup(world, credential=dir_credential(refresh="stored-rt-0000"))
        stored = store.read_account(world.root, "extra")
        store.write_account(world.root, replace(stored, state=AccountState.NEEDS_LOGIN))

    refused(world, directory, 3)


def test_a_dir_without_a_login_is_refused(world: World) -> None:
    directory = setup(world)
    world.dirs[str(directory)].blob = None

    refused(world, directory, 3)


def test_an_account_vibe_holds_already_is_refused(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = setup(world, config_dir=None)

    refused(world, directory, 2)

    assert "is not a config-dir login" in capsys.readouterr().err


def test_the_active_account_is_refused(world: World) -> None:
    directory = setup(world)
    store.write_active(world.root, "extra")

    refused(world, directory, 2)


def test_the_account_claude_code_runs_as_is_refused(world: World) -> None:
    directory = setup(world)
    # `active` still says main, but ~/.claude.json and the port say Claude Code runs as extra.
    (world.home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "uextra"}})
    )

    refused(world, directory, 2)


def test_an_unknown_account_is_not_found(world: World) -> None:
    assert run(world, "nobody") == 4


def test_a_claim_held_by_a_refresh_stops_it_with_nothing_written(world: World) -> None:
    directory = setup(world)
    assert store.claim_refresh(world.root, "extra", now_s=NOW_S + 10**6)

    refused(world, directory, 2)


# --- a delete that fails puts the file back -------------------------------------------------------


def test_a_refused_delete_puts_the_account_file_back(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = setup(world, port=StubbornKeychain(refuse=True))
    store.write_stash(world.root, "extra", make_credential(refresh="stash-rt-old"))
    before = snapshot(world, directory)

    assert run(world, "extra") == 5

    assert snapshot(world, directory) == before  # config_dir and the old credential are back
    assert store.read_account(world.root, "extra").config_dir == str(directory)
    assert world.dirs[str(directory)].blob is not None  # the dir still holds its login, alone
    assert store.read_stash(world.root, "extra") is not None  # nothing else changed either
    err = capsys.readouterr().err
    assert "could not delete" in err and "back as it was" in err
    assert not any(secret in err for secret in SECRETS)


def test_a_delete_that_says_it_failed_but_removed_the_item_is_a_release(world: World) -> None:
    directory = setup(world, port=StubbornKeychain(late=True))

    assert run(world, "extra") == 0

    assert store.read_account(world.root, "extra").config_dir is None
    assert world.dirs[str(directory)].blob is None


def test_a_delete_whose_outcome_cannot_be_told_gives_the_login_back_to_the_dir(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = setup(world, port=StubbornKeychain(late=True, unreadable_after=True))

    assert run(world, "extra") == 5

    account = store.read_account(world.root, "extra")
    # vibe must not believe it holds a login that may still be in the dir: it never refreshes it.
    assert account.config_dir == str(directory)
    assert account.credential == dir_credential()  # with the newest copy, should the item be gone
    outcome = oauth.refresh(world.root, "extra", account.credential, world.client, now_ms=NOW_MS)
    assert outcome.error == "owned"
    assert "could not tell whether" in capsys.readouterr().err


def test_an_interrupted_delete_leaves_the_login_with_the_dir(world: World) -> None:
    @dataclass
    class Interrupted(FakeKeychain):
        def delete(self) -> None:
            raise KeyboardInterrupt

    directory = setup(world, port=Interrupted())

    with pytest.raises(KeyboardInterrupt):
        run(world, "extra")

    account = store.read_account(world.root, "extra")
    assert account.config_dir == str(directory) and account.credential == dir_credential()
    assert world.dirs[str(directory)].blob is not None


# --- the process listing --------------------------------------------------------------------------


def test_holders_reads_the_variable_from_each_process_environment(tmp_path: Path) -> None:
    directory = tmp_path / ".claude-extra"
    directory.mkdir()
    listing = (
        "  101 claude CLAUDE_CONFIG_DIR=" + str(directory) + "\n"
        "  102 claude CLAUDE_CONFIG_DIR=" + str(directory) + "-2\n"
        "  103 claude XCLAUDE_CONFIG_DIR=" + str(directory) + "\n"
        "  104 /usr/bin/python vibe release extra CLAUDE_CONFIG_DIR=" + str(directory) + "\n"
        "  105 zsh CLAUDE_CONFIG_DIR=\n"
        "garbage line\n"
    )

    assert release.holders(listing, directory, own_pid=104) == (101,)


def test_ps_is_asked_for_this_users_processes_with_their_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        calls.append((argv, env))
        return subprocess.CompletedProcess(argv, 0, "  1 /sbin/launchd\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere")

    assert release.PsProcesses().listing() == "  1 /sbin/launchd\n"

    ((argv, env),) = calls
    assert argv == ["/bin/ps", "-E", "-o", "pid=,command=", "-U", str(os.getuid())]
    assert "CLAUDE_CONFIG_DIR" not in env  # ps would otherwise list itself


@pytest.mark.parametrize(("code", "stdout"), [(1, "  1 x\n"), (0, "")])
def test_a_ps_that_fails_or_says_nothing_is_an_error(
    monkeypatch: pytest.MonkeyPatch, code: int, stdout: str
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, code, stdout, "")
    )

    with pytest.raises(VibeError):
        release.PsProcesses().listing()


# --- the Keychain delete --------------------------------------------------------------------------


@pytest.mark.parametrize(("code", "ok"), [(0, True), (44, True), (51, False)])
def test_mac_keychain_delete_removes_that_item_only(
    monkeypatch: pytest.MonkeyPatch, tmp_home: Path, code: int, ok: bool
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    directory = tmp_home / ".claude-extra"
    port = keychain.config_dir_port(directory)
    assert isinstance(port, MacKeychain)

    if ok:
        port.delete()
    else:
        with pytest.raises(VibeError):
            port.delete()

    ((argv),) = calls
    assert argv[:2] == ["/usr/bin/security", "delete-generic-password"]
    assert argv[argv.index("-s") + 1] == keychain.config_dir_service(directory)
    assert "-w" not in argv


def test_file_keychain_delete_removes_the_file(tmp_home: Path) -> None:
    path = tmp_home / ".claude-extra" / ".credentials.json"
    port = FileKeychain(path)
    port.write('{"claudeAiOauth": {}}')

    port.delete()
    port.delete()  # already gone is fine

    assert port.read() is None and not path.exists()


def test_the_released_credential_round_trips(world: World) -> None:
    setup(world)
    assert run(world, "extra") == 0

    blob = credential_to_blob(store.read_account(world.root, "extra").credential)

    assert parse_blob(blob) == dir_credential()


def test_only_claude_code_holds_the_dir_not_what_a_session_started() -> None:
    from vibemaxxing.release import holders, is_claude_code

    env = "CLAUDE_CONFIG_DIR=/Users/x/.claude-extra"
    listing = "\n".join(
        [
            f"101 claude --mcp-config=/x.json {env}",
            f"102 /Users/x/.local/share/claude/versions/2.1.318 --resume abc {env}",
            f"103 /opt/homebrew/bin/python3 -I scripts/search_all.py {env}",
            f"104 /bin/zsh -c source snapshot.sh {env}",
            f"105 /usr/bin/python3 -m vibemaxxing bridge serve {env}",
        ]
    )
    assert holders(listing, Path("/Users/x/.claude-extra"), own_pid=1) == (101, 102)
    assert is_claude_code("claude -p hi") and not is_claude_code("python3 claude_helper.py")
