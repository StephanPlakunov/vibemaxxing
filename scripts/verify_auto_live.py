#!/usr/bin/env python3
"""The live check of what an automatic switch writes to the Keychain. By hand, never from pytest.

pytest has to pass on Linux, where there is no Keychain, and the autouse guard in
tests/conftest.py fails any test that spawns `security`. So the one thing a tick
does that nothing automated can show -- write the incoming blob into the real
Keychain item Claude Code reads -- has this script as its only evidence.

    VIBE_LIVE_OK=1 uv run python scripts/verify_auto_live.py <a> <b>

<a> and <b> are two THROWAWAY accounts already stored with `vibe add`, one of them the
login Claude Code holds right now (`vibe switch <a>` first). It switches to the other
through store.switch, the only path a tick writes the Keychain by, asserts that the port
holds the incoming account's credential with the blob's siblings intact, that
~/.claude.json and `active` follow, then switches back and asserts the original blob
returned. On any failure it restores the Keychain from a backup it took first.

It refuses to run without VIBE_LIVE_OK=1: it writes the real Keychain. No token value is
ever printed; credentials are compared and reported by SHA-256 prefix only. It does not
say anything about a `claude` session that is already running -- see docs/CONTRACT.md
section 17: that is untested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from vibemaxxing import store
from vibemaxxing.credentials import parse_blob, read_claude_identity, write_claude_identity
from vibemaxxing.keychain import CLAUDE_CODE_KEYCHAIN_SERVICE, MacKeychain, default_port


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def token_digest(blob: str) -> str:
    return digest(parse_blob(blob).access_token.reveal())


def stored_digest(root: Path, alias: str) -> str:
    return digest(store.read_account(root, alias).credential.access_token.reveal())


def restore(root: Path, port: MacKeychain, backup: Path, live: str) -> bool:
    """Put back what the Keychain, `active` and ~/.claude.json held before the script ran.

    First the guarded way, a switch back to `live`, which restores all three and holds
    the claims. Only if that does not bring the exact blob back is the backup written
    straight into the Keychain, and then `active` and ~/.claude.json are repointed too,
    since a bare Keychain write would leave them naming the other account.
    """
    saved = backup.read_text()
    try:
        store.switch(root, live, port)
    except Exception as exc:
        print(f"  switching back failed ({type(exc).__name__}); writing the backup")
    back = port.read()
    if back is None or json.loads(back) != json.loads(saved):
        port.write(saved)
        store.write_active(root, live)
        write_claude_identity(Path.home() / ".claude.json", store.read_account(root, live).identity)
        back = port.read()
    ok = back is not None and json.loads(back) == json.loads(saved)
    print(f"  restored: {'verified' if ok else 'FAILED TO VERIFY'}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("a", help="a throwaway alias")
    parser.add_argument("b", help="another throwaway alias")
    args = parser.parse_args()

    if os.environ.get("VIBE_LIVE_OK") != "1":
        print("refusing: this writes the real Keychain. Re-run with VIBE_LIVE_OK=1 to allow it.")
        return 2
    if sys.platform != "darwin":
        print("this script only means anything on macOS")
        return 2
    if args.a == args.b:
        print("pass two different aliases")
        return 2

    port = default_port()
    if not isinstance(port, MacKeychain):
        print("the default port is not the macOS Keychain; refusing")
        return 2

    root = store.store_root()
    before = port.read()
    if before is None:
        print("the Keychain holds no Claude Code credential; nothing to verify")
        return 2
    live = store.live_alias(root, port)
    if live not in (args.a, args.b):
        print(
            f"Claude Code is logged in as {live or 'an account vibe cannot name'}, not as "
            f"{args.a} or {args.b}. This only touches the two throwaway accounts: run\n"
            f"  vibe switch {args.a}\nfirst, then this again."
        )
        return 2
    target = args.b if live == args.a else args.a
    print(f"live now     : {live}   will switch to {target}")

    # A backup first, owner-only, outside the store: the Keychain is about to be written.
    fd, name = tempfile.mkstemp(prefix="vibe-keychain-backup-", suffix=".json")
    backup = Path(name)
    with os.fdopen(fd, "w") as handle:
        handle.write(before)
    backup.chmod(0o600)
    print(f"backup       : {backup}  (sha256 {digest(before)}, {len(before)} bytes)")
    print(f"token before : sha256 {token_digest(before)}")
    print(f"siblings     : {sorted(json.loads(before))}")

    try:
        print(f"\nswitch -> {target}   (store.switch, the path a tick writes by)")
        store.switch(root, target, port)
        mid = port.read()
        if mid is None:
            raise AssertionError("the Keychain came back empty after the switch")
        got = token_digest(mid)
        print(f"  token now  : sha256 {got}")
        if got != stored_digest(root, target):
            raise AssertionError(f"the Keychain does not hold {target}'s credential")
        if sorted(json.loads(mid)) != sorted(json.loads(before)):
            raise AssertionError("a sibling member of the blob was dropped by the switch")
        print(f"  siblings   : {sorted(json.loads(mid))}  (preserved)")
        if store.read_active(root) != target:
            raise AssertionError(f"`active` does not name {target}")
        wanted_uuid = store.read_account(root, target).identity.account_uuid
        named = read_claude_identity(Path.home() / ".claude.json").account_uuid
        if wanted_uuid is not None and named != wanted_uuid:
            raise AssertionError(f"~/.claude.json does not name {target}")
        # An account stored without a uuid cannot be named from ~/.claude.json at all.
        if wanted_uuid is not None and store.live_alias(root, port) != target:
            raise AssertionError(f"live_alias does not answer {target}")
        print(f"  active     : {store.read_active(root)}   ~/.claude.json follows")

        print(f"\nswitch -> {live}   (back)")
        store.switch(root, live, port)
        after = port.read()
        if after is None:
            raise AssertionError("the Keychain came back empty after switching back")
        if json.loads(after) != json.loads(before):
            raise AssertionError("the blob did not come back equal to the original")
        print(f"  blob equals the original: yes  (sha256 {digest(after)})")
    except BaseException as exc:
        # BaseException: a ctrl-C between the two switches must put the Keychain back too.
        print(f"\nFAILED: {type(exc).__name__}: {exc}")
        print("restoring the Keychain, `active` and ~/.claude.json")
        if restore(root, port, backup, live):
            backup.unlink(missing_ok=True)  # it holds plaintext tokens
        else:
            print(f"the backup is still at {backup}; it holds plaintext tokens, delete it after")
        return 1

    backup.unlink(missing_ok=True)
    print("\nverified: a switch writes the incoming blob, keeps its siblings and returns clean.")
    print(f"not verified: a `claude` session already running when {CLAUDE_CODE_KEYCHAIN_SERVICE!r}")
    print("changes may keep its old account, and one that later refreshes its own token may")
    print("overwrite the Keychain with it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
