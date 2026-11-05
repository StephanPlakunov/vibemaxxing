# Cutover: one shared login (main and extra take turns in Claude Code's own login)

What it changes: `extra` stops living in `~/.claude-extra`. vibe holds its login, and the background
tick (`vibe auto tick`, launchd, every minute) moves Claude Code's own login between main and extra by
the policy. Every session on that login (`cw`, `cs`, plain `claude`, the desktop app) follows a switch
within about a minute, without a restart.

## Already done (by the session that built it)

- Code merged into `stephan/ordered-failover` and installed (`~/.local/bin/vibe`).
- `~/.zshrc`: `cw` (connectors) and `cs` (solo); `claude` is plain Claude Code again.
- `~/.vibemaxxing/settings.json`: `routing.shared_login: true` and the policy
  `main until 99% (weekly: today's budget) -> extra until 99% -> main until 99%, then earliest reset`.
  Until step 2 below it cannot switch to extra (extra still lives in its own dir), so it stays on main.
- Cursor's status line: `~/.cursor/statusline.sh`, registered in `~/.cursor/cli-config.json`
  (`statusLine`). A `cursor-agent` that was open while it was written may drop the entry when it
  exits; check with `grep statusLine ~/.cursor/cli-config.json`, and re-add it with no
  `cursor-agent` running if it is gone (backup: `~/.cursor/cli-config.json.pre-statusline`).

## What you do

1. **Close every session that runs in `~/.claude-extra`.** They hold extra's login and renew it;
   the release refuses while one runs and names their PIDs. To see them:

   ```
   for p in $(pgrep -x claude); do ps -E -o command= -p $p | grep -q 'CLAUDE_CONFIG_DIR=/Users/stephanplakunov/.claude-extra' && ps -o pid=,command= -p $p; done
   ```

2. **Release extra** in a fresh terminal (`source ~/.zshrc` first, or open a new window):

   ```
   vibe release extra
   ```

   It moves extra's login from `~/.claude-extra`'s Keychain item into vibe and deletes that item,
   so the login exists in one place only. `~/.claude-extra` has no login afterwards; don't log in
   there again (a second login of the same account in two places is what revoked sessions before).

3. **Check:**

   ```
   vibe auto status     # policy with "today's budget", live main, next step extra
   vibe list            # extra is listed without a config dir
   ```

4. **Continue in a `cw` session** and ask it to verify the live switch (below); the session that
   built this can be resumed with `cw --resume 715f45b0-0bf9-424c-bae3-f671814a0d10`.

## Live check (not done yet)

With one `cw` session open and busy on a harmless task:

1. `vibe switch extra` in another terminal.
2. Within about a minute the session's status line shows `▶ extra`, its next requests count on
   extra (`vibe list` before and after), and the session keeps working.
3. `vibe switch main` and the same back.

Not known until then: whether claude.ai connectors (Slack, Atlassian, Microsoft 365, which belong
to main) keep working in a session after the login moved to extra. Expect them to fail while extra
is live; Atlassian reads still work through vibe-bridge.

## Undo

- Back to per-session accounts: set `routing.shared_login` to `false` (or remove it).
- Give extra its own dir again (no browser): `vibe add extra --from-config-dir ~/.claude-extra --adopt-stored-login`.
- Old shell names: `claude() { vibe run auto -- claude "$@"; }` and
  `claude-code() { vibe run auto --no-needs -- claude "$@"; }` in `~/.zshrc`.
- Settings before this change: `~/.vibemaxxing/settings.json.pre-shared-login`.
