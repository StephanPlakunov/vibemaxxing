# Changelog

Notable changes per release. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - fork `stephan/ordered-failover`

### Fixed

- **A running Claude session no longer loses its login because vibe started or polled something.** A long
  interactive session on extra stopped with "OAuth token revoked": it had been started with
  `CLAUDE_CODE_OAUTH_TOKEN` (an access token that cannot renew itself) while vibe's own 5-minute tick refreshed the
  same lineage and rotated its refresh token. An account imported with `--from-config-dir` now records that dir,
  `vibe run` starts Claude Code with `CLAUDE_CONFIG_DIR=<dir>` and no token (Claude Code refreshes the login itself;
  env scrub and bridge injection unchanged), and nothing in vibe refreshes it: `oauth.refresh` refuses it for every
  caller (run, list, TUI, web, tick, `run auto`), `vibe switch` and auto-switch never copy it into the default login,
  and its usage is read with the token in the dir's own Keychain item. Importing the same dir again converts an
  alias imported earlier. An interactive run on a vibe-owned account prints when its token ends.

### Added

- **`vibe add ALIAS --from-config-dir DIR --adopt-stored-login`** hands the login vibe has stored for ALIAS to a config
  dir without a browser: it is written into the dir's own Claude Code Keychain item and `<dir>/.claude.json`, and the
  account is marked as the dir's *before* Claude Code can refresh it (checked again under the refresh claim, so no
  earlier-started refresh can post it). Never overwrites a dir's login; a dead lineage is found before anything is handed
  over; a failed write gives the lineage back to vibe. Polls (tick, list, TUI, web) now refresh a vibe-owned account only
  once its access token has ended, not in the five minutes before.

### Changed

- **The bridge reaches `vibe run` sessions by itself.** `vibe run` / `vibe run auto` start claude with a token
  override and no `CLAUDE_CONFIG_DIR`, so a bridge registered in `~/.claude-extra` was never seen. A Claude
  session on an account that lacks a connector the bridge offers now gets `--mcp-config=<vibe-owned file with
  only vibe-bridge>` (one line printed; never for Cursor; `--no-bridge` turns it off), and `vibe bridge
  add-command` says so.

- **Atlassian allowlist = the live server.** Aligned with the real `tools/list` of the Atlassian MCP (read
  with a real read-only login): the nine read-only tools it offers (incl. `discover` and `executeRead`),
  with its own argument names, types and required; names it does not offer (`listJiraProjects`, ...) are
  gone; `executeWrite`/`executeDestructive` stay unreachable, and `executeRead` may only name a read
  operation (enforced in code). One real read through `vibe bridge serve` worked.

- **vibe-bridge v2: no model in the bridge.** `vibe bridge serve` now forwards each call, as an MCP client,
  straight to Slack's and Atlassian's own remote MCP servers with the user's own OAuth login (`vibe bridge
  login|logout|status|tools`), exactly one allowlisted upstream tool per call (reads only), tokens only in the Keychain, a timeout, a per-hour cap, a size cap and a content-free
  log. The v1 nested-`claude -p` bridge, its fence check and its disabled switch are removed. No
  Microsoft 365 (its remote MCP needs admin consent). Read-only: no draft tool, no `--drafts`, and a login
  whose granted scope can write is refused; `tools/list` shows our own fixed schemas (no network); a
  kill during a token refresh no longer costs a browser login; no input (deeply nested JSON, an oversized
  `expires_in`) can end `vibe bridge serve`; a call's deadline binds every request and the refresh lock;
  the login listener ignores stray requests and cannot be blocked by an idle connection. `docs/CONTRACT.md`
  section 20.

- **Cursor on every surface.** The TUI and the web dashboard show a Cursor seat as one more ledger
  entry (included + bonus, on-demand against the cap, reset, routing state), `vibe statusline` appends
  `cursor: 0.1% $0/$250` (`?` for unknown or old numbers, `spent (resets ...)`), and `status.json` has a
  `cursor` array. The line and the TUI use cached numbers only; the background tick re-reads a seat
  older than three minutes. `docs/CONTRACT.md` section 10 and 19.

- Every child of `vibe run` gets an explicit environment (`childenv.py`): Claude credentials and
  config dir are never inherited, Cursor and the bridge's nested run also lose secret-looking
  variables (`--pass-env` to pass one). Routing uses roles (`routing.roles`) and refuses a run that
  would reach Cursor only because main/extra are misnamed. Headless Cursor runs default to
  `--mode ask` and say what they may do; Cursor's cap is held to vibe's $250 ceiling with fresh
  readings; an interactive move to Cursor prints one clear line first. The bridge pins setting
  sources (`user`: measured, `project` alone hides the connectors) and permission mode, refuses a
  user settings file with an MCP allow rule, and fails on a denial. It is disabled
  (`bridge.ENABLED = False`) until a live run shows a non-allowed tool denied or absent: an init
  listing showed the model is offered every connector tool. Its tool names and write-tool lists now
  follow the live connectors (the Atlassian server is
  `Atlassian_Rovo`). A `Fence` check (built on fakes) denies every tool the
  connectors offer except the allowed one and verifies the init event before each call. It stays off:
  a final live init still offered six more Slack tools (send, draft, schedule, reaction, canvases)
  that an earlier listing of the connected server had not shown, so a listing proves nothing complete.

### Added

- **`vibe bridge serve` (vibe-bridge).** A local stdio MCP server that lets a session on extra
  borrow Slack, Atlassian or Microsoft 365 for one read or draft at a time: one headless Claude
  Code run on main (never Cursor, never extra: fixed in code), with only that tool allowed (plus
  `--permission-prompts none` and a deny list of the connector's write tools), on a cheap model,
  under a timeout and a calls-per-hour cap, logged without content to `~/.vibemaxxing/bridge.log`. Sends, posts,
  ticket changes, deletes and reactions are refused ("ask the lead"). `vibe bridge add-command`
  prints the `claude mcp add` line for extra's config dir. `docs/CONTRACT.md` §20.
- **Cursor accounts.** `vibe add [alias] --kind cursor` registers a Cursor seat (no credential
  of vibe's: `cursor-agent login` keeps it). `vibe run cursor -- claude <args>` translates the
  Claude Code command line to `cursor-agent` (every flag mapped or refused by name; the model is
  picked from the live `cursor-agent models`, never a `-fast` or `(NO ZDR)` one unless allowed).
  `vibe list` shows the seat's usage, read the way cursor-agent's own `/usage` reads it
  (unverified against the live endpoint; unknown on any shape change). `vibe run auto` routes a
  run by need (connectors or none) with a weekday budget on main, Cursor's on-demand cap and
  extra; `vibe shell-init` prints the zsh snippet. `vibe run cursor --trust-workspace` passes
  `--trust` for headless runs. `docs/CONTRACT.md` §19.
- **An ordered failover policy.** An optional `policy` in `~/.vibemaxxing/settings.json`
  (`{"mode": "ordered", "steps": [{"account", "until"}...], "then": "earliest_reset"}`)
  replaces "the most room at 99%": each tick puts Claude Code on the first step whose
  account has every session and weekly row below that step's limit, on fresh numbers and a
  usable login, and on the account that gets room back first when no step has any. Model
  rows block only when named in `block_models`. The cooldown holds only a switch to an
  account that outranks one where the live account still has room; leaving an account at or
  over a limit is never delayed. Every existing stay still holds, and an account outside the
  policy is left alone. The fence records the limit an account was left at (`auto.json`
  `limit`), so an account left at 85% can still be used at 99%. The live account is polled
  every minute within 9 points below each of its own limits. `vibe auto status` shows the
  policy, the step and the next step (`--json`: `policy`, `step`, `next_step`); `vibe alias`
  renames steps. Without a `policy` nothing changes. `docs/CONTRACT.md` §18.
- **`vibe add [alias] --from-config-dir DIR`** imports the login Claude Code keeps for
  `CLAUDE_CONFIG_DIR=DIR` (its Keychain item `Claude Code-credentials-<8 hex of
  sha256(DIR)>`, or `DIR/.credentials.json` off macOS) and the `oauthAccount` in
  `DIR/.claude.json`. No browser, no request, no refresh, no Keychain write; `active` does
  not move.
- **`~/.vibemaxxing/status.json` and `vibe statusline`.** Every command that collects usage
  and every tick writes the live account, each account's rows (percent, reset), when they
  were fetched, the policy and the last switch. `vibe statusline` prints one line from that
  file alone, for Claude Code's `statusLine`: `▶ extra · main: 5h 100% (resets 19:30) wk 93%
  (resets Mon 00:00) · extra: 5h 28% (resets 23:10)`. About 25 ms: the console scripts start
  in `vibemaxxing.entry`, which answers it before the CLI imports anything heavy.
- **terminal-notifier.** The notification on every automatic switch goes through
  `/opt/homebrew/bin/terminal-notifier` when it is installed (title "vibemaxxing"), and through
  `osascript` when it is not or fails.

### Changed

- The switch notification reads `Switched to <to>: <reason>` instead of the tick's line.
- `vibemaxxing.__version__` is looked up on first use instead of at import.

### Fixed

- Two tests failed on Python 3.14, which warns when the collector closes an unclosed
  `HTTPError`; the client and the test now close it.

### Not verified

- Everything in "Not verified" under 0.3.0 still holds, and an ordered policy switches more
  often than the default rule.
- The Keychain item name for a config dir was checked against one ASCII path on one machine,
  not against Claude Code's source; the NFC step for non-ASCII paths is from memory of it.
- No real Keychain, running `claude`, loaded launchd job or real terminal-notifier
  notification was exercised.

## [0.3.0] - 2026-10-22

### Added

- **`vibe auto`: switch accounts by itself.** With `vibe auto on`, a check
  (`vibe auto tick`, the unit a scheduler calls) leaves the live account for the one with
  the most room once its session or weekly row reaches 99%. It only acts on numbers it has,
  does not switch onto an account whose login lapsed or was rejected, or onto a stale one
  or one at its limit, waits 15 minutes between switches, does not overwrite a login vibe
  does not know (while every stored account has an account id), stays while `active` and
  the live login disagree, and prints one line (`stay: …` or `switched a -> b: …`) with
  exit 0 either way. Also `status`, `exclude`/`include`, `undo`,
  `off`, and on macOS `install`/`uninstall`, which write and remove the launchd job and
  print the `launchctl` line instead of running it. Settings live in
  `~/.vibemaxxing/settings.json`; an invalid file means the defaults and never stops
  `vibe list`. `docs/CONTRACT.md` §17.
- The live account is polled about every one to two minutes, not every three, once a session
  or weekly row is at 90%, and only while no backoff is running. That can exceed the ~28-30
  requests an hour measured for one account; a 429 is obeyed.
- A `auto_switch` block in the `--json` envelope (`enabled`, `threshold`, `last_switch`),
  and a line in the TUI footer and on the web page, both read-only. Additive; the schema is
  still 1.
- `scripts/verify_auto_live.py`: a real switch between two throwaway accounts on a real
  Keychain, refusing to run without `VIBE_LIVE_OK=1`, restoring the Keychain, `active` and
  `~/.claude.json` on any failure. Never run from pytest.

### Fixed

- **`vibe switch` could overwrite a login vibe did not know and lose it.** When
  `~/.claude.json` named an account vibe had never stored (someone ran `claude /login`), the
  resync was skipped but the Keychain was still written. It now refuses, with `vibe add`,
  which adopts that login.
- **`vibe switch` could put a dead account in the Keychain and log Claude Code out.** It
  now refuses an account whose login has lapsed or was rejected, with `vibe add <name>`, and
  refreshes an expired access token first to be sure.
- **A stale `active` pointer lost the live account's rotated token.** `active` is only what
  vibe last wrote; when Claude Code ran as another stored account, the switch skipped its
  resync and overwrote it. The resync now goes to the account that owns the login, and
  `vibe switch <that account>` repairs the pointer without loss.
- **A dead refresh token was posted on every poll.** `invalid_grant` was never kept, so a
  dashboard left open spent a request a minute on each dead account. It is now recorded on
  the account and not posted again; a fresh `vibe add` clears it.

### Changed

- `vibe switch` to an account whose access token has expired makes one request, so that case
  no longer works offline.

### Not verified

- What a switch does to a `claude` session that is already running, and whether an old
  session refreshing its own token overwrites the Keychain afterwards.
- A real Keychain switch (`scripts/verify_auto_live.py` is the check) and a loaded launchd job.

## [0.2.2] - 2026-09-17

### Fixed

- **The active account showed "the login has lapsed" hours after `vibe add`, and could
  log Claude Code out.** Claude Code refreshes the active account's token in the Keychain
  and the server rotates the refresh token each time, but the dashboard kept refreshing
  its own stored copy of the same lineage. Whichever refreshed second held a spent token.
  Nothing refreshes the active account any more, checked again under the refresh lock
  that `vibe switch` now also holds. The dashboard and `vibe run` use the Keychain's token
  when `~/.claude.json` names the same account, else the stored one, until it expires.
  `vibe switch` and `vibe alias` hold the refresh locks of both accounts and refuse while
  one is held.
- **`vibe switch` could put a spent token into the Keychain.** A refresh interrupted after
  the server rotated the token leaves the live successor in a stash; switching to that
  account wrote the account file's spent predecessor instead. The stash now goes in.
  `CONTRACT.md` §8.
- A non-UTF-8 `~/.claude/.credentials.json` no longer crashes every poll on Linux, nor a
  non-UTF-8 Keychain item on macOS.

## [0.2.1] - 2026-09-16

### Fixed

- **Accounts showed `the usage endpoint answered HTTP 429` instead of their limits.**
  Every process polled on its own timer, a rate-limited account was polled again on the
  very next cycle, and a 429 threw away the numbers fetched a minute earlier. Usage
  answers now live in one cache per account under `~/.vibemaxxing/usage/`, shared by the
  web dashboard, the TUI and `vibe list`: an answer under ~3 minutes old is reused
  without a request, a 429 or 5xx backs that account off at 60/120/240/480 s (or the
  server's `Retry-After`, capped at an hour), and while it waits the account keeps showing
  its last numbers, up to an hour old, with the time they are from.
- An account without a known plan no longer asks the profile endpoint on every poll.
- A usage answer carrying a non-finite percentage no longer fails every account's view.
- **Two dashboard tabs could double the request rate.** The web cache checked freshness
  under its lock but fetched outside it, so two tabs whose timers had drifted together
  both fetched. The fetch now happens under the lock.

## [0.2.0] - 2026-09-16

### Added

- `vibe help`: a command that explains what each command is for and when to use it.
  Before it existed, `vibe help` answered `invalid choice: 'help'` and then listed the
  commands it had just refused to explain.
- The web dashboard shows **the week ahead** beside the account ledger: one lane per
  account, a marker where that account's weekly limit resets, seven days from right now.
  Times render in the reader's own timezone, and day boundaries are local midnights, so
  the grid does not drift across a DST change.
- Every account's plan (`max 20x`, `pro`) is shown in the dashboard's ledger.
- A top-level `resets` array in the `--json` envelope, listing each account's weekly
  rollover soonest first. Documented in `docs/CONTRACT.md` §10.

### Fixed

- **Login failed at the token exchange with HTTP 403.** Cloudflare fronts all three
  allowed hosts and answers urllib's default `Python-urllib/3.x` user agent with
  `error code: 1010` before the request reaches the API. Every request now names a real
  user agent.
- **Login failed at the Authorize click with "Invalid request format".** The PKCE
  `state` was 22 characters; the authorize endpoint accepts the consent page but rejects
  the submission below 43. The failure only surfaced at the last step, which is what made
  it look like a redirect problem.
- **`vibe switch` changed the credential but not the account.** Claude Code caches the
  profile in `~/.claude.json` under `oauthAccount` and trusts it for 24 h, so it ran on
  the new token while still showing the previous account's email, org and limits. The
  switch now repoints that object and drops `profileFetchedAt`, which makes Claude Code
  refetch the rest itself. No priming needed on a machine that has never run it.

## [0.1.0] - 2026-09-15

First release. Multi-account store, OAuth login with PKCE, credential switching through
the macOS Keychain or `~/.claude/.credentials.json`, `vibe run` for per-command accounts,
a Textual dashboard, a loopback web dashboard, and a documented `--json` envelope.

Published to TestPyPI only, to prove the release pipeline.

[0.2.2]: https://github.com/jskarbecki/vibemaxxing/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/jskarbecki/vibemaxxing/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jskarbecki/vibemaxxing/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jskarbecki/vibemaxxing/releases/tag/v0.1.0
