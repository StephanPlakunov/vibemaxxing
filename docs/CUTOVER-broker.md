# Cutover: the token broker (one login per account, fixed and switching sessions side by side)

**Not run yet.** Each step needs Stephan's OK. Run the steps in order, in a terminal that is not
inside a Claude session on extra. Every step says how to undo it. Design and evidence:
`docs/BROKER.md`.

State before (5 Nov):

- `vibe auto off`.
- main is Claude Code's default login, and Claude Code refreshes it.
- extra lives in `~/.claude-extra`, and Claude Code refreshes it there.
- The launchd tick `de.intra-ai.vibemaxxing-auto` runs every minute.

## 0. Install, and back up

The PR merged into `stephan/ordered-failover`, then:

```
git -C ~/vibemaxxing pull --ff-only
vibe broker status                     # expect: broker       off
vibe auto status | head -5             # expect: auto-switch  off, live main
launchctl print gui/$(id -u)/de.intra-ai.vibemaxxing-auto | grep -m1 'state ='
( umask 077; mkdir -p ~/.vibemaxxing/pre-broker && cp -p ~/.vibemaxxing/settings.json ~/.vibemaxxing/accounts/*.json ~/.vibemaxxing/pre-broker/ )
```

The new code is inert while the broker is off: the tick runs it every minute and changes nothing. The
backup holds tokens (mode 0600). It is only good for an immediate rollback, because every refresh
afterwards makes its refresh tokens stale. Delete it after a week.

**Undo:** `git -C ~/vibemaxxing revert -m 1 <merge sha> && git -C ~/vibemaxxing push` (or check out
the old head `b34f7f9`).

## 1. Move extra's login into vibe (no new sign-in)

```
vibe release extra
# expect: released extra: vibe holds its login now; ~/.claude-extra has no login any more
vibe list                              # extra with its rows
```

It refuses while any Claude Code process runs with `CLAUDE_CONFIG_DIR=~/.claude-extra`, and names
the PIDs. Close those sessions (`/exit`; helpers on extra included) and run it again. Afterwards they
come back on extra with `vibe run extra -- claude --resume <id>` (after step 3: same dir, same
`projects` link, so the resume finds them). Do steps 1–3 back to back: between them,
`vibe run extra` would still hand out a bare token.

**Undo:** `vibe add extra --from-config-dir ~/.claude-extra --adopt-stored-login` puts vibe's copy
back into the dir's own item, with no browser.

## 2. Create the mirror dirs

```
vibe broker mirror main      # creates ~/.claude-main (0700), links CLAUDE.md, settings.json, skills, projects
vibe broker mirror extra     # ~/.claude-extra already has the links: nothing to do
```

Nothing is written to the Keychain while the broker is off.

**Undo:** `rm -rf ~/.claude-main` (links only; plus its `.claude.json` after step 3).

## 3. Turn the broker on

```
vibe broker on
vibe broker status
```

**What it does:**

- Takes main's login from Claude Code's default item into vibe's store, under main's refresh claim.
- Leaves a mirror in the default item: the same access token, no refresh token, MCP logins kept.
- Writes the mirrors of `~/.claude-main` and `~/.claude-extra`, plus `oauthAccount` into their
  `.claude.json`.

**If it refuses with `waiting: main's login renews within 10 min`:** a running session may be renewing
it right now. Wait 15 minutes and run it again.

**Check that no item holds a refresh token any more.** Only names and times are printed, never a token:

```
for d in "" ~/.claude-main ~/.claude-extra; do
  s="Claude Code-credentials${d:+-$(printf %s "$d" | shasum -a 256 | cut -c1-8)}"
  security find-generic-password -a "$USER" -s "$s" -w \
    | python3 -c 'import json,sys,time; m=json.load(sys.stdin)["claudeAiOauth"]; print(sys.argv[1], "refreshToken" in m, time.strftime("%H:%M", time.localtime(m["expiresAt"]/1000)))' "$s"
done
# expect three lines, each with False
```

`vibe broker status` should show main and extra, each with "renewed at" a few hours ahead, their
mirrors (`default` for main) and no problem.

**If `vibe broker on` stopped half way** (an error or a crash part way through): run
`vibe broker off` anyway. It sees that the default item holds vibe's mirror of main while the setting
still says off, and gives main's whole login back. `vibe broker on` refuses a `settings.json` it
cannot save before it writes anything.

**Undo:** `vibe broker off`. Main's whole login goes back into the default item, and Claude Code
renews it again. The dir mirrors are deleted.

## 4. Auto-switch on

```
vibe auto on
vibe auto tick          # "stay: …", plus "| broker: …" when it renewed or wrote something
vibe auto status        # ends with the broker block
```

**Undo:** `vibe auto off`. The broker keeps renewing anyway: it runs in every tick.

## 5. Live checks

**a. Fixed main.** In a new terminal, run `vibe run main`.

- The status line shows `▶ main (fixed)`.
- `/status` shows stepan.plakunov@gmail.com.
- `/mcp` lists the claude.ai connectors (Slack, Atlassian, Microsoft 365) as connected.
- Start Remote Control and open the session from the phone. This proves the mirrored token keeps its
  claude.ai scopes.
- The environment has no token. This prints names only:

  ```
  ps -E -o command= -p "$(pgrep -n -f 'claude/versions/')" | tr ' ' '\n' | grep -E '^(CLAUDE_CONFIG_DIR|CLAUDE_CODE_OAUTH_TOKEN|VIBE_SESSION)=' | cut -d= -f1
  # expect CLAUDE_CONFIG_DIR and VIBE_SESSION, no CLAUDE_CODE_OAUTH_TOKEN
  ```

**b. Fixed extra.** Run `vibe run extra`. Expect `▶ extra (fixed)`, and `/status` shows
claude2@intra-ai.de.

**c. A switching session.**

1. Run `cs`. Expect `▶ main`.
2. Run `vibe auto off; vibe switch extra`.
3. Within about a minute, the `cs` status line shows `▶ extra` and its next prompt works.
4. Run `vibe list` before and after: extra's numbers move.
5. Sessions a and b still show their own accounts and keep working.
6. Go back with `vibe switch main; vibe auto on`.

**d. The first renewal**, at main's "renewed at" time in `vibe broker status` (at most about 4 h):

- `vibe broker status` shows a later "token ends" and no problem.
- Sessions a, b and c answer a prompt.
- The Keychain check from step 3 still prints False three times.

**e. Overnight.** Session a, started before d, still works the next morning, more than 8 hours later.
That is the "never dies from its token" proof.

**Stop signals:**

- A `broker problem:` line in `vibe list`.
- `problem:` in the tick line or in `vibe broker status`.
- A `True` in the Keychain check, meaning Claude Code wrote a refresh token back.

On any of these, do not switch by hand. Report it with `vibe broker status --json` and the tick line.

**Never run `vibe add extra`; report it.** extra cannot sign in again (its mailbox cannot be reached),
and `vibe add` is a new browser sign-in. When vibe says the server rejected a login, it says
"report it; do not run vibe add", for main too: a wrong mark is cleared by looking at it, not by a
new login.

## Full rollback (back to the state before step 1)

1. Close every `vibe run main` / `vibe run extra` session; their mirrors go away in step 4.
2. `vibe auto off`.
3. If `vibe auto status` says live `extra`, run `vibe switch main`.
4. `vibe broker off`. Main's whole login goes back into the default item, and the dir mirrors are
   deleted.
5. `vibe add extra --from-config-dir ~/.claude-extra --adopt-stored-login`. Extra lives in its dir
   again, with no browser.
6. Optional: `rm -rf ~/.claude-main`.
