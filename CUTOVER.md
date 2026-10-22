# Cutover: from the claude-failover relay to vibemaxxing

Tonight's steps, in order, each with how to undo it. Run them yourself in one terminal; none
of them has been run on this machine yet. `vibe` is already installed:
`~/.local/bin/vibe -> ~/vibemaxxing/.venv/bin/vibe`, branch `stephan/ordered-failover`.

## Before you start

1. **Finish or close every Claude Code session**, including `claude-run` workers, the desktop
   app and IDE sessions. They talk to the relay through `ANTHROPIC_BASE_URL`, and every
   request they make fails once the relay stops in step 1.
2. Back up the three files the steps change:

   ```
   cp ~/.claude/settings.json ~/.claude/settings.json.pre-vibe
   cp ~/.zshrc ~/.zshrc.pre-vibe
   cp ~/.claude.json ~/.claude.json.pre-vibe
   ```

3. Check that the install answers: `vibe --version` should print `0.3.0`.

## 1. Stop and remove the old relay

```
launchctl bootout gui/$(id -u)/com.claude-failover.relay
launchctl print gui/$(id -u)/com.claude-failover.relay      # expect: "Could not find service"
mv ~/Library/LaunchAgents/com.claude-failover.relay.plist ~/.claude-failover/
```

Two more things point at the relay and must go with it, or Claude Code will keep trying to
reach a relay that is gone.

**`~/.claude/settings.json` sets `env.ANTHROPIC_BASE_URL` to `http://127.0.0.1:18765`.**
Remove that one key:

```
python3 - <<'EOF'
import json, pathlib
path = pathlib.Path.home() / ".claude" / "settings.json"
settings = json.loads(path.read_text())
env = settings.get("env", {})
print("removed:", env.pop("ANTHROPIC_BASE_URL", None))
if not env:
    settings.pop("env", None)
path.write_text(json.dumps(settings, indent=2) + "\n")
EOF
grep -n ANTHROPIC_BASE_URL ~/.claude/settings.json      # expect: no output
```

**`~/.zshrc` aliases `claude`, `claude-accounts` and `planner` to the relay's wrapper.** That
wrapper falls back to `CLAUDE_CONFIG_DIR=~/.claude-extra`, which must not be used after
step 3. Remove the first two and point `planner` at the real `claude`:

```
python3 - <<'EOF'
from pathlib import Path
path = Path.home() / ".zshrc"
text = path.read_text()
for old, new in (
    ("# Claude Code two-account failover (see ~/.claude-failover)\n", ""),
    ('alias claude="$HOME/.claude-failover/bin/claude"\n', ""),
    ('alias claude-accounts="python3 $HOME/.claude-failover/failover.py status"\n', ""),
    ("$HOME/.claude-failover/bin/claude --agent planner", "claude --agent planner"),
):
    assert text.count(old) == 1, f"not found exactly once: {old!r}"
    text = text.replace(old, new)
path.write_text(text)
EOF
unalias claude claude-accounts 2>/dev/null; source ~/.zshrc
type claude                     # expect: claude is /Users/stephanplakunov/.local/bin/claude
grep -n claude-failover ~/.zshrc  # expect: no output
```

**Undo:**

```
mv ~/.claude-failover/com.claude-failover.relay.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.claude-failover.relay.plist
cp ~/.claude/settings.json.pre-vibe ~/.claude/settings.json
cp ~/.zshrc.pre-vibe ~/.zshrc && source ~/.zshrc
```

## 2. `vibe add main`: store the current login

`vibe add main` with a name would open a **browser** login. To store the login Claude Code
already has, without a browser, adopt it and then rename it:

```
vibe add                         # prints: adopted the login already in Claude Code as "<alias>"
vibe alias <alias> main          # <alias> is probably stepan.plakunov
```

Check: `vibe list` shows `* main  stepan.plakunov@gmail.com  max …` with its rows. The `*`
means vibe's active account.

**Undo:** `vibe remove main`. This forgets the account. The Keychain and Claude Code's login
are untouched.

## 3. `vibe add extra --from-config-dir ~/.claude-extra`

```
vibe add extra --from-config-dir ~/.claude-extra
```

Expect `imported the login in /Users/stephanplakunov/.claude-extra as "extra"`. It reads the
Keychain item `Claude Code-credentials-702813c4` and `~/.claude-extra/.claude.json`. It
writes no Keychain item, does not change the active account and makes no request.

- `Claude Code has no login for ...`: the item is not there. Log in under that dir with the
  real binary, then run the import again:
  `CLAUDE_CONFIG_DIR=~/.claude-extra ~/.local/bin/claude /login`.
- `... names no account (oauthAccount.accountUuid)`: start
  `CLAUDE_CONFIG_DIR=~/.claude-extra ~/.local/bin/claude` once so it writes its profile,
  then import again.

From here on **nothing may use `~/.claude-extra`.** vibe refreshes extra's login, and a
refresh retires the refresh token in that dir's item. Whichever copy refreshes second is
logged out.

**Undo:** `vibe remove extra`. Once vibe has refreshed extra (any `vibe list` after its access
token expired), the copy in `~/.claude-extra` is dead. To use that dir again:
`CLAUDE_CONFIG_DIR=~/.claude-extra ~/.local/bin/claude /login`.

## 4. `vibe list`

```
vibe list
```

Expect both accounts, `main` with the `*`, each with Session and Weekly rows.

If `extra` reads `the refresh token for "extra" was rejected - run: vibe add extra` (or
`login lapsed`), the token in the Keychain item was already spent. That happens if the relay
refreshed it without writing the new one back, which I could not check. Log extra in fresh
in the browser, choosing claude2@intra-ai.de:

```
vibe add extra
```

**Undo:** nothing; `vibe list` only reads and refreshes extra's (non-active) token.

## 5. Write the policy

```
( umask 077; cat > ~/.vibemaxxing/settings.json <<'EOF'
{"schema": 1, "auto_switch": false, "threshold": 99, "cooldown_s": 900, "excluded": [],
 "policy": {"mode": "ordered",
            "steps": [{"account": "main", "until": 85},
                      {"account": "extra", "until": 99},
                      {"account": "main", "until": 99}],
            "then": "earliest_reset"}}
EOF
)
vibe auto status
```

Expect `auto-switch  off` and
`policy       ordered: main until 85% -> extra until 99% -> main until 99%, then earliest reset`.
If you see a `threshold` line instead of `policy`, the file did not parse and vibe is using
the defaults. Compare it with the block above.

**Undo:** `rm ~/.vibemaxxing/settings.json` (back to the defaults: auto-switch off).

## 6. Turn it on and load the job

```
vibe auto on
vibe auto install
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/de.intra-ai.vibemaxxing-auto.plist
launchctl print gui/$(id -u)/de.intra-ai.vibemaxxing-auto | head -20
vibe auto tick                  # one by hand; expect e.g. "stay: main is below 85% (step 1 of 3)"
vibe statusline                 # the same numbers as one line
```

`vibe auto install` prints the same `launchctl bootstrap` line. The job runs
`~/vibemaxxing/.venv/bin/python3.14 -m vibemaxxing auto tick` every 60 s and has no log file.
To see it working, check that `vibe statusline` changes, or that the `written_at` in
`~/.vibemaxxing/status.json` moves every minute.

**Undo:**

```
vibe auto off
launchctl bootout gui/$(id -u)/de.intra-ai.vibemaxxing-auto
vibe auto uninstall
```

## 7. Live test: switch under a running session

The ordered policy is followed on every tick, so a hand switch to extra while main has room
would be switched back within a minute. **Turn it off for the test:**

```
vibe auto off
vibe list                       # write down main's and extra's Session %
```

**Terminal A:** start a session with a debug log, and give it a long, harmless task that
needs many requests. Tool calls are what make it many requests: one long answer is a single
request and shows nothing.

```
cd ~/vibemaxxing && claude --debug
> Read every file in src/vibemaxxing one at a time. After each, write two lines about it before reading the next. Do not edit anything.
```

**Terminal B,** while A is working:

```
date                            # note the time of the switch
vibe switch extra
vibe list                       # right after
sleep 300; vibe list            # five minutes later, while A is still working
```

What to look for:

- **The session continues:** terminal A keeps working, with no login prompt and no 401.
- **Usage lands on extra:** between the two later `vibe list`s, extra's Session % goes up
  and main's does not. If main's goes up instead, the running session is still using main's
  token from memory. It may move to extra only when that token expires (hours), or never
  until restarted. That is the untested part, and worth knowing either way.
- **The debug log:** `claude --debug` writes under `~/.claude/debug/` (newest file; your
  version may also print the path). Look at the lines after the switch time:
  `grep -niE "401|refresh|keychain|oauth|token" <that file> | tail -40`. You are checking for
  any refresh, and whether a refresh wrote main back.
- **Nothing was overwritten:** `vibe list` after the session ends still shows `*` on extra,
  and `vibe auto status` says `live         extra` with no "vibe's active account is"
  warning. If it says Claude Code runs as main while vibe says extra, the old session wrote
  its own login back. Stop there and do not run `vibe switch` until you have looked: see the
  report.

Then go back and turn it on again:

```
vibe switch main
vibe auto on
vibe auto tick
```

**Undo:** `vibe switch main`.

## Optional, later: the status line in Claude Code

Add to `~/.claude/settings.json` (the same file as step 1):

```json
"statusLine": {"type": "command", "command": "vibe statusline"}
```

**Undo:** remove that key.

## Full rollback to the relay

1. `vibe auto off; launchctl bootout gui/$(id -u)/de.intra-ai.vibemaxxing-auto; vibe auto uninstall`
2. `vibe switch main`. The relay expects main in the default Keychain item, and auto-switch
   may have left extra there.
3. Undo step 1 (plist back, bootstrap, restore `settings.json` and `.zshrc`).
4. Log extra back in under its dir. vibe has very likely retired that copy:
   `CLAUDE_ACCOUNT=extra ~/.claude-failover/bin/claude auth login`.
5. If you want vibe gone: `rm ~/.local/bin/vibe; rm -rf ~/.vibemaxxing`.

## Open after tonight

- `claude-run` (`~/.local/bin/claude-run -> ~/.claude-failover/claude_run.py`) was built
  around the relay. I did not read it, so I cannot say whether it still works without the
  relay. Check before relying on it, and update the delegation section in
  `~/.claude/CLAUDE.md`.
- `~/.claude-failover/app-shim.sh` may be wired into the desktop app or an IDE. Check before
  removing `~/.claude-failover`.
