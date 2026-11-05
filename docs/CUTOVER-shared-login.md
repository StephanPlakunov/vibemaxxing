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

In a **new** Terminal window (not inside a Claude session):

```
zsh ~/vibemaxxing/scripts/switch-to-shared-login.zsh
```

It notes every conversation that runs in `~/.claude-extra`, waits while you type `/exit` in each
(it never closes or kills anything), runs `vibe release extra`, reopens every conversation in its
own folder on the shared login (`vibe run auto --no-needs --claude-only -- claude --resume <id>`:
always Claude Code, so nothing is lost), and shows `vibe auto status`. `--dry-run` shows what it
would do and changes nothing.

After it: `~/.claude-extra` has no login; don't log in there again (a second login of the same
account in two places is what revoked sessions before).

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
