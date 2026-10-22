"""`vibe add <alias> --from-config-dir <dir>`: take over a login Claude Code keeps for a
second config dir, with no browser, no network and no refresh.

Claude Code started with CLAUDE_CONFIG_DIR=<dir> keeps its credential in its own
Keychain item and its profile in <dir>/.claude.json. The import reads both and
writes one account file; it never writes a Keychain item and never moves `active`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient, FakeKeychain, make_credential, seed_account
from vibemaxxing import cli, keychain, store
from vibemaxxing.credentials import Identity, credential_to_blob
from vibemaxxing.keychain import FileKeychain, MacKeychain

NOW_S = 1_757_930_000.0
EXTRA = Identity(
    "claude2@intra-ai.de", "uextra", "Intra AI", "org-intra", "team", "stripe", "Claude Two"
)


@dataclass
class Rig:
    home: Path
    directory: Path
    port: FakeKeychain = field(default_factory=FakeKeychain)
    imported: FakeKeychain = field(default_factory=FakeKeychain)
    client: FakeHttpClient = field(default_factory=FakeHttpClient)
    asked: list[Path] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return self.home / ".vibemaxxing"

    def port_for(self, directory: Path) -> FakeKeychain:
        self.asked.append(directory)
        return self.imported

    def context(self) -> cli.Context:
        return cli.Context(
            root=self.root,
            client=self.client,
            port=self.port,
            now_s=NOW_S,
            prompt=lambda _: pytest.fail("the import asked for input"),
            browser=lambda _: pytest.fail("the import opened a browser"),
            port_for=self.port_for,
        )


def _profile(directory: Path, identity: Identity | None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    account = (
        {}
        if identity is None
        else {
            "emailAddress": identity.email,
            "accountUuid": identity.account_uuid,
            "organizationName": identity.organization_name,
            "organizationUuid": identity.organization_uuid,
            "displayName": identity.display_name,
        }
    )
    (directory / ".claude.json").write_text(json.dumps({"oauthAccount": account}))


@pytest.fixture
def rig(tmp_home: Path) -> Rig:
    directory = tmp_home / ".claude-extra"
    _profile(directory, EXTRA)
    found = Rig(tmp_home, directory)
    found.imported.blob = credential_to_blob(
        # Expired an hour ago: the import must store it as it is, not refresh it.
        make_credential(access="extra-at", refresh="extra-rt", expires_at_ms=1),
        base={"mcpOAuth": {"theirs": True}},
    )
    return found


def test_the_item_name_is_the_one_claude_code_uses_for_a_config_dir() -> None:
    # The item Claude Code created for CLAUDE_CONFIG_DIR=/Users/stephanplakunov/.claude-extra
    # on the machine this fork was made for is "Claude Code-credentials-702813c4".
    service = keychain.config_dir_service(Path("/Users/stephanplakunov/.claude-extra"))

    assert service == "Claude Code-credentials-702813c4"


def test_import_stores_the_login_and_touches_nothing_else(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_account(rig.root, "main", active=True)
    before = rig.imported.blob

    code = cli.main(
        ["add", "extra", "--from-config-dir", str(rig.directory)], context=rig.context()
    )

    assert code == 0
    account = store.read_account(rig.root, "extra")
    assert account.credential.refresh_token.reveal() == "extra-rt"
    assert account.credential.expires_at_ms == 1, "stored as found: no refresh"
    assert account.identity == Identity(
        "claude2@intra-ai.de", "uextra", "Intra AI", "org-intra", None, None, "Claude Two"
    )
    assert rig.client.requests == [], "no network"
    assert rig.port.log == [], "Claude Code's own login was not read or written"
    assert rig.imported.log == ["keychain.read"] and rig.imported.blob == before
    assert rig.asked == [rig.directory]
    assert store.read_active(rig.root) == "main", "an imported login is not the live one"
    out = capsys.readouterr().out
    assert "imported the login in" in out and '"extra"' in out
    assert "extra-at" not in out and "extra-rt" not in out


def test_import_resolves_the_dir_as_claude_code_was_given_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(rig.home)

    # The second and third are the same login again, so only the first one stores it.
    for spelled in ("~/.claude-extra/", ".claude-extra", str(rig.directory) + "/"):
        rig.asked.clear()
        assert cli.main(["add", "extra", "--from-config-dir", spelled], context=rig.context()) == 0
        assert rig.asked == [rig.directory], spelled


def test_import_without_a_name_takes_one_from_the_email(rig: Rig) -> None:
    assert cli.main(["add", "--from-config-dir", str(rig.directory)], context=rig.context()) == 0

    assert store.list_aliases(rig.root) == ["claude2"]


def test_import_speaks_json(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["add", "extra", "--from-config-dir", str(rig.directory), "--json"]

    assert cli.main(argv, context=rig.context()) == 0

    assert json.loads(capsys.readouterr().out) == {
        "schema": 1,
        "ok": True,
        "action": "add",
        "alias": "extra",
        "adopted": True,
        "config_dir": str(rig.directory),
    }


def test_a_login_already_stored_is_not_stored_twice(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_account(rig.root, "other", credential=make_credential(refresh="extra-rt"))

    assert (
        cli.main(["add", "extra", "--from-config-dir", str(rig.directory)], context=rig.context())
        == 0
    )

    assert store.list_aliases(rig.root) == ["other"]
    assert 'already stored as "other"' in capsys.readouterr().out


def test_a_dir_with_no_login_names_the_item_and_the_way_to_log_in(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.imported.blob = None

    code = cli.main(
        ["add", "extra", "--from-config-dir", str(rig.directory)], context=rig.context()
    )

    assert code == 3
    err = capsys.readouterr().err
    assert keychain.config_dir_location(rig.directory) in err
    assert f"CLAUDE_CONFIG_DIR={rig.directory} claude /login" in err
    assert store.list_aliases(rig.root) == []


def test_a_profile_without_an_account_id_is_refused(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    # Stored without a uuid, auto-switch could never tell this account apart (CONTRACT §17).
    _profile(rig.directory, None)

    code = cli.main(
        ["add", "extra", "--from-config-dir", str(rig.directory)], context=rig.context()
    )

    assert code == 2
    assert ".claude.json" in capsys.readouterr().err
    assert store.list_aliases(rig.root) == []


def test_the_active_account_is_never_overwritten(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_account(rig.root, "extra", active=True)

    code = cli.main(
        ["add", "extra", "--from-config-dir", str(rig.directory)], context=rig.context()
    )

    assert code == 2
    assert "active" in capsys.readouterr().err
    assert store.read_account(rig.root, "extra").credential.refresh_token.reveal() != "extra-rt"


def test_another_account_under_that_name_is_never_overwritten(rig: Rig) -> None:
    other = Identity("x@example.com", "uother", None, None, None, None, None)
    seed_account(rig.root, "extra", identity=other)

    code = cli.main(
        ["add", "extra", "--from-config-dir", str(rig.directory)], context=rig.context()
    )

    assert code == 2
    assert store.read_account(rig.root, "extra").identity.account_uuid == "uother"


def test_the_same_account_is_replaced_and_its_old_stash_dropped(rig: Rig) -> None:
    seed_account(rig.root, "extra", identity=EXTRA, credential=make_credential(refresh="old-rt"))
    store.write_stash(rig.root, "extra", make_credential(refresh="old-successor"))

    assert (
        cli.main(["add", "extra", "--from-config-dir", str(rig.directory)], context=rig.context())
        == 0
    )

    assert store.read_account(rig.root, "extra").credential.refresh_token.reveal() == "extra-rt"
    assert store.read_stash(rig.root, "extra") is None, "a stash would be consumed as a successor"


def test_an_invalid_alias_is_refused(rig: Rig) -> None:
    code = cli.main(
        ["add", "Extra", "--from-config-dir", str(rig.directory)], context=rig.context()
    )

    assert code == 2
    assert store.list_aliases(rig.root) == []


# --- the ports ----------------------------------------------------------------


def test_config_dir_port_reads_the_hashed_item_on_macos(
    monkeypatch: pytest.MonkeyPatch, tmp_home: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"claudeAiOauth": {}}\n', "")

    monkeypatch.setattr(subprocess, "run", run)
    directory = tmp_home / ".claude-extra"

    port = keychain.config_dir_port(directory)

    assert isinstance(port, MacKeychain)
    assert port.read() == '{"claudeAiOauth": {}}'
    (argv,) = calls
    assert argv[argv.index("-s") + 1] == keychain.config_dir_service(directory)
    assert "find-generic-password" in argv


def test_config_dir_port_reads_the_dirs_own_file_elsewhere(
    monkeypatch: pytest.MonkeyPatch, tmp_home: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    directory = tmp_home / ".claude-extra"
    directory.mkdir()
    (directory / ".credentials.json").write_text('{"claudeAiOauth": {}}')

    port = keychain.config_dir_port(directory)

    assert isinstance(port, FileKeychain)
    assert port.read() == '{"claudeAiOauth": {}}'
    assert keychain.config_dir_location(directory) == str(directory / ".credentials.json")


def test_the_default_item_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    MacKeychain().write("blob")

    assert calls[0][calls[0].index("-s") + 1] == "Claude Code-credentials"
