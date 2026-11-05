# vibemaxxing

Run several Claude Code accounts from one machine, switch between them in a second, and
see all their plan limits in one place.

If you have more than one Claude subscription, you already know the problem: the only way
to change account is `/logout`, `/login`, browser, paste. And nothing anywhere tells you
how much weekly headroom you have left across all of them.

```
vibe switch work
```

![The dashboard: every account's limits on the left, the week ahead on the right](https://raw.githubusercontent.com/jskarbecki/vibemaxxing/main/docs/media/dashboard.png)

## Install

```
uv tool install vibemaxxing     # or: pipx install vibemaxxing
```

macOS and Linux. Python 3.11+. Two commands, same program: `vibe` and `vibemaxxing`.

## Start

```
vibe add                  # store the account you are already logged in to
vibe add side             # log a second one in, in the browser
vibe list                 # see both, and what is left on them
vibe switch side          # Claude Code is now that account
```

`vibe add` with no name takes the login already in Claude Code, no browser and no
network. That is the fastest way in: one command and you have your first account stored.

### Import a login from a second config dir

If you already run a second account under its own Claude Code config dir
(`CLAUDE_CONFIG_DIR=~/.claude-work claude`), take that login over without a browser:

```
vibe add work --from-config-dir ~/.claude-work
```

It reads the Keychain item Claude Code keeps for that dir (`Claude Code-credentials-`
plus the first 8 hex digits of the sha256 of the dir's absolute path; on Linux
`<dir>/.credentials.json`) and the `oauthAccount` in `<dir>/.claude.json`, and stores the
account like `vibe add` does. No request, no refresh, no Keychain write, and the active
account does not change. It refuses a dir with no login, a profile with no account id
(auto-switch could never tell that account apart), the active account's name, and a name
another account already holds.

**From then on that login is Claude Code's, in that dir, and vibe never refreshes it.** The account records
the dir. `vibe run work -- claude` starts Claude Code with `CLAUDE_CONFIG_DIR=<dir>` and no token override (the other
Claude variables are still dropped and the bridge is still handed over), so Claude Code reads and renews the login in
the dir's own Keychain item, and a session in it keeps working for as long as the login lives, whatever else vibe
starts or polls. (Before this, vibe copied the login, refreshed the copy itself and ran sessions on a bare access
token: a refresh on either side logged the other out, and a long session died with "OAuth token revoked".)
`vibe list`, the TUI and the web page read that account's usage with the token in the dir's item; once it has expired
they show the last numbers until the next session in the dir renews it. `vibe switch work` is refused (it would copy
the refresh token into the default login) and auto-switch never switches onto it: use `vibe run work -- claude`. A
dir with no login, or logged in as another account, stops the run and names the command that fixes it.

**A config dir has its own settings.** The `CLAUDE.md`, `settings.json`, `skills` and `projects` of `~/.claude` do not
apply there unless linked (`ln -s ~/.claude/CLAUDE.md ~/.claude-work/`); `vibe run` names what is missing. Linking
`projects` also lets `claude --resume` find sessions that were started in `~/.claude`.

An alias imported before this change becomes owned by running the same import again: the login is already stored, so
vibe records the dir, drops its stash and stops refreshing.

### Hand the stored login to a config dir (no browser, no mailbox)

When the only working login of an account is the copy vibe stored (nobody can log in again), give that copy to the
config dir instead of importing one from it:

```
vibe add extra --from-config-dir ~/.claude-extra --adopt-stored-login
```

It writes the stored login (access and refresh token, expiry, scopes, plan) into the dir's own Claude Code Keychain
item, and the account (`oauthAccount`: uuid, email, organisation) into `<dir>/.claude.json`, keeping every other key
there; then `vibe run extra -- claude` works as above. **vibe stops refreshing before Claude Code can**: the account
is marked as the dir's first, under the refresh claim, and `oauth.refresh` checks that mark again under the same claim,
so even a tick that read the account a moment earlier cannot post the token Claude Code now holds. An access token that
has ended is refreshed once first (a dead login is found then, with nothing handed over); one that still works is
handed over as it is. It refuses: the active account or the one Claude Code runs as, a dir that already has a login
(never overwritten; take that one over with the plain import), a profile naming another account, a lapsed or rejected
login, an account that lives in another dir. If the Keychain write fails and the item is not there, the mark is taken
back (vibe may refresh again); if it may be there, the mark stays and running the command again finishes the job. No
token is printed.

**Polls and refreshes, until an account is adopted.** The tick, `vibe list`, the TUI and the web page refresh an account
vibe owns only once its access token has really ended, never in the five minutes before (a session started with that
token may still be using it); `vibe run`, whose child cannot renew a token, keeps the window. That is as seldom as a
refresh can be: once per access token. It cannot keep a `CLAUDE_CODE_OAUTH_TOKEN` session alive past that token's
end, which no refresh can; an adopted account can.

## Every day

```
vibe                          open the dashboard in the terminal
vibe usage web                open it in a browser at http://127.0.0.1:8787
vibe list                     every account, its limits, the pooled total
vibe switch <name>            change the active account
vibe run <name> -- <cmd>      run one command as one account (`cursor`: translated for Cursor)
vibe run auto -- claude       pick the account by the routing rules, then run
vibe shell-init               print a zsh snippet that routes `claude` through that
vibe remove <name>            forget an account
vibe alias <old> <new>        rename one
vibe auto on|off|status       switch accounts by itself, see below
vibe statusline               one line for Claude Code's status line, see below
vibe help                     what each command is for
```

### switch, or run?

`vibe switch work` changes the account **globally**, the same as logging out and back in.
Claude Code sessions already running keep the old one until they restart.

`vibe run work -- claude` pins **one command** to one account and leaves everything else
alone. Two terminals can hold two different accounts at once:

```
vibe run work -- claude          # this window is "work"
vibe run side -- claude          # that window is "side"
```

### Switch automatically

```
vibe auto on              # off until you say so
vibe auto status          # who is live, the last switch, where the next one would go
vibe auto install         # macOS: writes the launchd job and prints the line that loads it
```

With it on, a check (`vibe auto tick`, which the launchd job runs every minute) leaves the
live account for the one with the most room as soon as its **session** or **weekly** row
reaches 99%. It prints one line and exits 0 whether or not it switched, so a scheduler can
call it freely.

It is deliberately timid:

- It only acts on numbers it has. A live account with no fresh answer, or an unknown live
  login, is a `stay`, never a guess.
- It does not switch onto an account whose login has lapsed, whose refresh token the server
  already rejected, or whose expired access token will not refresh, nor onto one whose
  numbers are older than about three minutes or that has a row at the limit. A refresh token
  the server would reject but has not been asked about passes until it is. The account it
  just left stays out until the row that limited it has reset.
- It does not overwrite a login vibe does not know. If you ran `claude /login` as an account
  you have not stored, it stops and tells you to `vibe add` it first. That check needs every
  account you stored to have an account id: if one has none, vibe cannot tell and falls back
  to the old behaviour.
- If vibe's active account and the live login disagree (after a `claude /login`), it points
  vibe at the live account first and carries on. It stays when that repair is refused, when
  the live account is stored without an account id, and never switches onto such an account.
- 15 minutes between switches, and `vibe auto undo` goes back.
- While the live account is at 90% or more, it is asked for fresh numbers about every one to
  two minutes instead of three. That is a lot of requests against a budget measured at about
  28 to 30 an hour for one account, and has not been measured against the live endpoint; a
  429 is obeyed and slows it down.

`vibe auto exclude <name>` keeps an account out of the running; `include` lets it back in.
The 99% and the 15 minutes live in `~/.vibemaxxing/settings.json`. The dashboards show
whether it is on and the last switch, and cannot change it.

Every automatic switch raises a notification titled "vibemaxxing", such as `Switched to
extra: main at 85% weekly`, through `/opt/homebrew/bin/terminal-notifier` when it is
installed and through `osascript` otherwise.

### An ordered policy

Instead of "the one with the most room at 99%", you can give the order accounts are used
in. Add a `policy` to `~/.vibemaxxing/settings.json`:

```json
{"schema": 1, "auto_switch": true, "threshold": 99, "cooldown_s": 900, "excluded": [],
 "policy": {"mode": "ordered",
            "steps": [{"account": "main", "until": 85},
                      {"account": "extra", "until": 99},
                      {"account": "main", "until": 99}],
            "then": "earliest_reset"}}
```

That reads: main until 85%, then extra until 99%, then main until 99%, then whichever
account gets room back first. Each tick walks the steps and puts Claude Code on the first
account whose session and weekly rows are all below that step's `until`, on fresh numbers
and a usable login. Model rows such as "Weekly · Fable" count only when named:
`"block_models": ["Fable"]`. When no step has room, it picks the account whose limiting
rows reset first.

- **Leaving an account at or over one of its limits is never delayed**, even right after a
  switch. The 15-minute cooldown holds only a switch to an account that outranks one where
  the live account still has room, such as going back to main after its weekly reset.
- Every guard above still holds. It stays when it has no fresh numbers for the live account,
  when the live account is unknown or the pointer is wrong, and when the live account is not
  in the policy (that was your choice). It never switches onto an account that is excluded,
  dead, stored without an account id, or has stale numbers. An account left at 85% stays
  out of the 85% step until what limited it has reset; its 99% step is still open.
- The policy is followed, so **a `vibe switch` to an account in the policy is undone by the
  next tick** when the policy says otherwise. Run `vibe auto off` to hold an account by hand.
- A policy that is wrong anywhere leaves the whole file at the defaults, so auto-switch is
  off. `vibe auto status` shows the policy, the step it is on now and the next one:

```
$ vibe auto status
auto-switch  on
policy       ordered: main until 85% -> extra until 99% -> main until 99%, then earliest reset
cooldown     900s
excluded     none
live         main
step         1 of 3: main until 85%
last switch  none
next step    2 of 3: extra until 99%
```

The live account is asked for numbers every minute while it is within 9 points below one of
its own limits (76-84% and 90-98% above), not every three.

`vibe auto install` only **writes** `~/Library/LaunchAgents/de.intra-ai.vibemaxxing-auto.plist`.
Loading it is yours to do, with the one `launchctl bootstrap` line it prints, so nothing
starts running behind your back. Anywhere else, run `vibe auto tick` from cron or a systemd
timer every minute.

**Not verified: what a switch does to a `claude` session that is already running.** Like
`vibe switch`, it changes the credential Claude Code reads and makes no attempt to signal a
live process, so a running session is not guaranteed to pick the new account up. And a
session that stays on the old account and later refreshes its own token may overwrite the
Keychain with it. Both are untested. `scripts/verify_auto_live.py` is the check for the part
that can be tested by hand: it does a real switch between two throwaway accounts and asserts
the Keychain holds the incoming credential.

### Status line

Every command that collects usage (`vibe list`, the dashboards, `vibe auto status`) and
every `vibe auto tick` writes `~/.vibemaxxing/status.json`: the live account, each
account's rows with their reset times, when they were fetched, the policy and the last
switch. `vibe statusline` prints one line from that file and nothing else: no Keychain, no
request, about 25 ms.

```
▶ extra · main: 5h 100% (resets 19:30) wk 93% (resets Mon 00:00) · extra: 5h 28% (resets 23:10)
```

Times are local: `HH:MM` today, weekday and time otherwise. A row whose window has rolled
over since it was read is left out, and numbers older than 15 minutes get a `?`. To show it
in Claude Code, add this to `~/.claude/settings.json`:

```json
{"statusLine": {"type": "command", "command": "vibe statusline"}}
```

### See what is left

```
$ vibe list
* work      you@example.com   max 20x
    Session                   34%
    Weekly · all models       61%
    Weekly · Fable            12%

  side      side@example.com  max
    Session                    2%
    Weekly · all models        9%

pool  1.36 account-weeks across 2 accounts
```

**account-weeks** is the pooled number: one account with a fresh weekly limit is `1.00`.
Two accounts each 50% spent is also `1.00`. It answers "how much Claude do I have left in
total", which no single account's percentage can.

The web dashboard adds the week ahead: one lane per account, a marker where that
account's weekly limit resets, in your own timezone. So you can see whether a reset lands
before your work does.

### Scripting

`--json` works on every command and prints one documented envelope, the same bytes the
dashboard serves at `/api/usage`:

```
vibe list --json | jq '.pool.remaining_account_weeks'
vibe list --json | jq -r '.resets[] | "\(.alias) \(.at)"'
```

The schema is frozen in [`docs/CONTRACT.md`](docs/CONTRACT.md) §10.

## Cursor accounts

Fork feature. A Cursor seat (the terminal agent `cursor-agent`, running the same Claude
models) is a second kind of account:

```
cursor-agent login                  # once; Cursor keeps that login, vibe never sees it
vibe add cursor --kind cursor       # checks `cursor-agent status`, stores a marker, no credential
vibe list                           # the seat's real numbers, next to the Claude accounts
vibe run cursor -- claude -p "fix the bug" --model sonnet --permission-mode auto
```

### What `vibe run cursor -- claude ...` does

It turns the Claude Code command line into a `cursor-agent` one. Every flag is mapped or
**refused by name**: nothing is dropped silently.

| Claude Code | cursor-agent |
| --- | --- |
| `-p`, `--output-format text\|json\|stream-json` | the same |
| `--include-partial-messages` | `--stream-partial-output` |
| `--permission-mode auto` | `--auto-review` (never `--force`) |
| `--permission-mode default` / `manual`, headless | `--mode ask` (Cursor's most restrictive mode, read-only exploration), see below |
| `--permission-mode default` / `manual`, interactive | nothing (what it may do without asking is Cursor's own setting) |
| `--permission-mode plan` | `--plan` |
| `--permission-mode bypassPermissions`, `--dangerously-skip-permissions` | refused; `--force` only with `vibe run --allow-force` |
| `--model`, `--effort` (or the model and effort Claude Code would use) | `--model claude-<family>-<version>-<effort>`, picked from the live `cursor-agent models` |
| `--resume <id>` | `--resume <id>` for a Cursor chat id; a Claude session id is refused |
| `--add-dir` | `--add-dir` |
| no `-p` (interactive) | cursor-agent's own terminal UI, and vibe says so |
| anything else (`--allowedTools`, `--mcp-config`, `-c`, `claude mcp ...`, ...) | refused, naming the flag |

Headless Claude Code denies whatever it was not allowed to do. Cursor's own help says its `-p`
"has access to all tools, including write and shell", and its docs say only that changes are
proposed, not applied, without `--force`; vibe could not check more than that. So a headless run
with no permission mode becomes `--mode ask`, and `vibe run` prints a "what it may do" line for
every Cursor run. Use `--permission-mode auto` for an agent that may act (`--auto-review`).

A model that is not in `cursor-agent models` is refused with the closest names. A `-fast`
variant and any model marked `(NO ZDR)` (no zero data retention) are never picked unless you
pass `--allow-fast` / `--allow-no-zdr`. `vibe run cursor -- <anything but claude>` runs the
command unchanged. `--dry-run` prints what would run and runs nothing.

### What it is not

It is not Claude Code. A Cursor session has no Claude skills, no Claude hooks, no Remote
Control, no `CLAUDE.md` memory; Cursor reads its own rules and its own MCP config. Do not
route a program that relies on Claude Code hooks for its safety policy: run it with
`--claude-only`. `vibe switch` and `vibe auto tick` never touch a Cursor account (the Keychain
login cannot point at Cursor).

### Where Cursor shows

The seat appears wherever the Claude accounts do. `vibe` (the TUI) and the web dashboard
(`vibe usage web`) show it as one more entry: included allowance plus bonus, on-demand against the
cap, when the cycle resets, and whether routing may use it (it is not in the pool or the week chart).
`vibe statusline` appends a compact segment, e.g. `cursor: 0.1% $0/$250`; `?` means no numbers, numbers
older than 15 minutes, or a finished cycle, and `spent (resets Wed 00:00)` means routing counts the
seat as used up. The status line and the TUI never make a request or read a Keychain on a redraw: the
line reads `~/.vibemaxxing/status.json`, which `vibe list`, the TUI, the web page and the background
tick (`vibe auto tick`, via launchd) keep current; the tick re-reads a seat's usage only when its cached
numbers are older than three minutes. Unknown usage is shown as unknown, never as a number. If your
Claude Code status line already runs `vibe statusline`, the new segment needs no change to it.

### The child's environment

Every child vibe starts gets an environment built on purpose. For Cursor vibe drops `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR`, `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN`, the Bedrock/Vertex switches and every `*_TOKEN`, `*_API_KEY`, `*_SECRET`,
`*_PASSWORD` you did not pass on purpose (`vibe run cursor --pass-env NAME,NAME`; never a Claude
credential). A Claude Code child loses only the Claude family and is then given its account's
token explicitly. Output names what was dropped, never a value. A shell that carries a Claude
token or config dir (a session vibe started on extra) never decides the account: a run on main
uses main's own token, set explicitly, not "the live login". Claude's own commands
(`claude --version`, `claude mcp ...`) keep your environment untouched.

### Usage

`vibe list` / `vibe usage` show the included allowance plus Cursor's bonus (a percent), the
on-demand spend against your personal cap, and when the cycle resets. `cursor-agent` has no
usage command, so vibe sends the same two calls its interactive `/usage` sends
(`GetCurrentPeriodUsage`, `GetHardLimit` on `api2.cursor.sh`; checked live on 3 Nov 2026, it
answers) with the access token
`cursor-agent login` stored: read at that moment, held in memory, sent in one header, never
stored or printed. It is an undocumented API, parsed defensively; any shape change reads as
`usage: unknown (...); see cursor.com/dashboard`, and unknown counts as unavailable. **This
has not been verified beyond one read-only call on one seat.**

### Routing: `vibe run auto`

`vibe run auto [--needs slack,atlassian | --no-needs] [--claude-only] -- claude <args>` picks the
account when the run starts and never again for that run (a running session keeps its login).
Defaults, all configurable under `routing` in `~/.vibemaxxing/settings.json`:

- **needs connectors**: `main` while its weekly usage is within today's budget, else `cursor`
  while it has usage left, else `extra` (no connectors: the run goes without and says so), else
  `main` whatever the budget.
- **needs none**: `extra`, else `main` within budget, else `cursor`, else `main` whatever the
  budget.
- **Budget**: the share of main's weekly usage allowed by the end of each day, Berlin time:
  Mon 20%, Tue 40%, Wed 60%, Thu 80%, Fri-Sun 100%.
- **Cursor has usage left** while its included usage and bonus last, and then its personal
  on-demand cap (minus a margin, default $10). vibe also holds the cap to its own $250 ceiling
  (`routing.cursor_cap_cents`) whatever Cursor reports, uses an answer at most two minutes old
  (30 seconds close to the cap; an older one is shown, never routed by), and refuses even
  `vibe run cursor` at the limit. **On-demand is billed to the company.** It
  counts as spent after that, until the cycle resets.
- `--needs m365` can only go to main. `--claude-only` never picks Cursor.
- An account with unknown usage is unavailable, and vibe prints why it was skipped.
- When a run moves between Claude and Cursor, vibe prints which account and why. An interactive
move prints one last line before Cursor's own UI starts: why main was passed over, which model,
that there are no Claude hooks, skills or Remote Control there, and `vibe run main -- claude` to
override. A Claude session
  id cannot be resumed on Cursor, nor the reverse: it starts a fresh run with `--brief "<task>"`
  and `--handover <file>` instead, and says so.

Routing names three **roles**, `main`, `extra` and `cursor`, and `routing.roles` maps each to the
alias it is stored under (`{"main": "stephan", "extra": "shared", "cursor": "zvoove"}`; default:
the same names). A Claude role that names no stored account makes the run refuse, not fall
through to Cursor. A Cursor step cannot be the last resort.

```json
{"routing": {"weekday_budget": [20, 40, 60, 80, 100, 100, 100], "timezone": "Europe/Berlin",
             "default_needs": ["slack", "atlassian"],
             "connectors": {"main": ["slack", "atlassian", "m365"],
                            "cursor": ["slack", "atlassian", "gcal"], "extra": []},
             "order": {"needs": [{"account": "main", "budget": true}, {"account": "cursor"},
                                 {"account": "extra", "degraded": true},
                                 {"account": "main", "last_resort": true}]},
             "cursor_margin_cents": 1000}}
```

`vibe shell-init` prints (and never writes) a zsh snippet: `claude` becomes
`vibe run auto -- claude`, and `claude-code` is `vibe run auto --no-needs -- claude`.
`claude --version` and `claude mcp ...` pass through untouched. A Claude account that is already
Claude Code's own login runs without a token override; any other runs on its own token, which
cannot refresh itself (see Known limitations).

### Connectors (MCP) on Cursor

A Cursor session does not get Claude's claude.ai connectors; those belong to the Claude account.
What the CLI agent has comes from Cursor's own MCP config and your Cursor plugins. Checked with
the interactive `/mcp`: Slack (`https://mcp.slack.com/mcp`, the same 27 tools), Atlassian and
Google Calendar are there; Microsoft 365 is not. So: main has slack, atlassian, m365; cursor has
slack, atlassian, gcal; extra has none. To add one yourself (vibe adds none):

- **Slack**: official hosted server `https://mcp.slack.com/mcp`, per Slack's
  [Cursor guide](https://docs.slack.dev/ai/slack-mcp-server/connect-to-cursor); your workspace
  admin must approve the integration.
- **Atlassian**: official Rovo MCP server `https://mcp.atlassian.com/v1/mcp/authv2` (OAuth), see
  [Atlassian's IDE setup](https://support.atlassian.com/atlassian-rovo-mcp-server/docs/setting-up-ides/).
  The old `/v1/sse` endpoint stops on 30 June 2026.
- **Microsoft 365**: Microsoft's own route is the Work IQ MCP (Entra OAuth, a global admin must
  grant consent, it may need a Microsoft 365 Copilot licence; locally `npx -y @microsoft/workiq
  mcp`), see [Microsoft Learn](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/overview).
  Treat M365 as main-only until that works for you.

In headless runs (`-p`) the agent may need `--approve-mcps`; vibe adds it only to runs that
declared `--needs`. Headless `cursor-agent -p` also stops at a workspace nobody trusted (exit 1,
no output: seen live); `vibe run cursor --trust-workspace` passes `--trust`, and vibe never adds it
on its own. In a headless run the Slack MCP answered "needs auth" even though the interactive agent
is logged in to it, so check `--needs` runs on Cursor before relying on them.

## The connector bridge (`vibe-bridge`)

A session on the extra account has no Slack or Atlassian connector. `vibe bridge serve` is a small
local stdio MCP server you register for extra only. Each call is forwarded **by vibe itself, as an MCP
client, straight to the vendor's own remote MCP server**, as exactly one allowlisted upstream tool with
the arguments given. **No model is in the path**: nothing sees the upstream's tool list, so there is
nothing for a prompt injection to widen and nothing to fence with a prompt or a deny list.

- **The allowlist is code** (`bridge.TOOLS`): **reads only. No draft tool, no write tool of any kind.**
  A name not on it is refused before any network is touched, whatever the upstream offers. `tools/list`
  shows our own fixed descriptions and argument schemas (written in this repo, no network), never the
  vendor's text.
- **Read-only logins only.** vibe asks for read scopes, and **refuses to store a login whose granted
  scope can write** (`write`, `delete`, `manage`, `admin`, `chat`, ...): a token that can write must never
  sit in a Keychain item the extra session's shell might be able to read.
- **Upstreams**: Slack's official MCP (`https://mcp.slack.com/mcp`) and Atlassian Rovo MCP v2
  (`https://mcp.atlassian.com/v2/mcp`). **Microsoft 365 is left out**: Microsoft's own remote MCP (Work IQ)
  needs a Global Administrator's consent, which is not your own OAuth.
- **Your own OAuth login per upstream** (the MCP authorization spec: resource and server metadata,
  dynamic client registration if offered, PKCE S256, the browser, a one-shot listener on `127.0.0.1`
  for the redirect). Tokens live only in the macOS Keychain under `vibemaxxing-bridge-<upstream>-*`:
  handed to the Keychain tool on its standard input, never a file, a log, an error or a command line.
  Refresh is handled under a lock and is crash-safe: the new refresh token is stored first, so a kill
  at any point after that leaves a login that refreshes on its own (the one instant before the first
  write, the length of a Keychain call, is not recoverable by any ordering). Every endpoint a
  discovery document names must be on that upstream's own hosts, or nothing is sent.
- **Each call**: a deadline (60 s) that binds every request, the body read and the wait for the
  refresh lock, so a timed-out call is not left running; at most 30 calls per hour per bridge process, a
  result size cap (100 kB: refused, not cut), results returned as data, and one log line in `~/.vibemaxxing/bridge.log`
  (upstream, tool, duration, bytes, outcome; never arguments or content). `routing.bridge` can set
  `timeout_s`, `max_calls_per_hour`, `max_result_bytes`; it cannot add a tool or an upstream.

What you run yourself, once:

```
# Atlassian: registers a public client by itself; asks for read scopes only
vibe bridge login atlassian

# Slack has NO dynamic client registration: it needs a Slack app of your own (an internal app in
# your workspace, approved by its admin) with the redirect URL below. vibe asks for its client id and
# secret (the secret is typed hidden, never an argument). Reads only.
vibe bridge login slack [--client-id ID] [--port N]
#   redirect URL to register on the Slack app: http://127.0.0.1:8765/callback

vibe bridge status        # who is logged in, token expiry, allowlist size; never a token
vibe bridge tools slack   # which allowlisted tools the live upstream offers, and whether our
                          # argument names match its own (names only; none of its text)
vibe bridge logout slack|atlassian|all
vibe bridge add-command   # prints the line that registers the bridge for extra; it never runs it
```

**You do not need to register it for `vibe run` sessions.** `vibe run <account> -- claude ...` (and
`vibe run auto`) start claude with a token override and without `CLAUDE_CONFIG_DIR` (for an account that lives in
its own config dir: with `CLAUDE_CONFIG_DIR` and no token), so Claude Code may not read the one in `~/.claude-extra`. So when the Claude run is on an account that lacks a
connector the bridge offers (by `routing.connectors`: extra has none) and the bridge has a login for that
upstream, vibe adds `--mcp-config=<~/.vibemaxxing/bridge/mcp-config.json>` to the claude arguments itself: a
vibe-owned file holding only `vibe-bridge` (not `--strict-mcp-config`, so the account's own servers stay) and
prints one line saying so. Never for Cursor runs; `vibe run --no-bridge ...` turns it off.

`vibe bridge add-command` still prints the registration line, for a session you start **without** `vibe
run`, for `~/.claude-extra`:

```
CLAUDE_CONFIG_DIR=~/.claude-extra claude mcp add --scope user vibe-bridge -- vibe bridge serve
```

Nothing in the bridge can write, so no Slack app needs a write scope: give it the read user scopes only.
Text an upstream returns (a Slack message, a Jira
description) reaches the extra session as the tool result: data, and the same prompt-injection surface any
connector has. It does not widen what the bridge can do.

## How it works

It swaps **credentials**, plus the one line of config that names them. One `~/.claude`
history and one MCP config, shared by every account, exactly as if you had logged in and
out by hand. On macOS the active credential is Claude Code's own Keychain item; on Linux
it is `~/.claude/.credentials.json`.

`vibe switch` also repoints `oauthAccount` in `~/.claude.json` and clears its
`profileFetchedAt`, because Claude Code caches that profile for 24 h and would otherwise
run on the new token while still showing the old account's email, org and limits. It
refetches the rest itself on the next start, so a fresh machine needs no setup beyond
`vibe add` and `vibe switch`.

Tokens refresh on demand about five minutes before expiry, behind a consume gate: a
refresh rotates the refresh token, so the successor is written to disk **before** the
predecessor is treated as spent. A crash mid-refresh leaves a recoverable successor
rather than a dead account.

It talks to three hosts and no others — `claude.ai`, `platform.claude.com`,
`api.anthropic.com` — and refuses to contact anything else, redirects included. (Fork: with a
Cursor account stored, a second client also reaches `api2.cursor.sh`, for the seat's usage, and
only that host.) No
version pings, no analytics, no telemetry. The web dashboard binds `127.0.0.1`, and
`--host` with anything else is a hard error.

It is not a third-party client. Claude Code goes on talking to Anthropic as itself, using
a credential you got by logging in the normal way — the same state you would reach by
hand with `/logout` and `/login`. Several subscriptions of your own is a question between
you and Anthropic; sharing one login between people is not what this is for, and not
something it helps you do.

## Other tools that do this

Several Claude Code account switchers exist. [claude-swap](https://github.com/realiti4/claude-swap)
is the mature one, and it also switches for you when an account nears its limit.
vibemaxxing does that too (`vibe auto`, off until you turn it on), so the difference is
not the switching.

Two things I could not find anywhere else, which is why this exists:

**A pooled number.** `account-weeks` answers "how much Claude do I have left in total".
Three percentages of three differently-sized weekly windows do not.

**The week ahead.** One lane per account, a marker where each weekly limit resets, in
your timezone — so you can see whether the headroom comes back before the deadline does.

## Known limitations

Real and deliberate. None of these is a bug report.

**Your tokens sit in plaintext JSON at rest.** `~/.vibemaxxing/accounts/*.json`, mode
`0600`, directories `0700`. Anyone who can read your files can read your tokens — which
is equally true of `~/.claude` itself.

**The macOS Keychain write puts the credential in `argv`.** `vibe switch` shells out to
`/usr/bin/security add-generic-password -w <blob>`, and macOS shows any local user a
process's full argv through `ps`. For the length of one `exec`, the credential is
readable cross-uid. The alternatives were measured and are worse: `security -i` silently
truncates at ~4005 bytes and stores the truncated value, and the Security framework
refuses to read Claude Code's item from a process outside its ACL, which would put an
authorization prompt in front of every switch. Multi-user macOS is out of scope for v1.

**`vibe run` cannot refresh its own token, for an account vibe owns.** It sets `CLAUDE_CODE_OAUTH_TOKEN` for the
child only, which is what lets two shells hold two accounts, but that is an access token (about 8 hours) and nothing
in the session can renew it: a session that outlives it fails, which is why an interactive run prints "this session
carries a token that ends at HH:MM". Every grandchild inherits the variable. **For a session that lives longer, give
the account its own config dir** (`vibe add NAME --from-config-dir DIR`, see "Import a login from a second config
dir"): `vibe run` then starts Claude Code with `CLAUDE_CONFIG_DIR=DIR` and no token, and Claude Code refreshes the
login itself.

**Switching does not reach a running session.** `vibe switch` makes no attempt to signal
a live Claude Code process. Restart it, or use `vibe run`. Automatic switching has the same
limit, and the untested caveat above it. An ordered policy switches more often than the
default rule (at 85% and back after a reset), so it meets that caveat more often.

**`vibe switch` can now refuse.** It will not write an account whose login has lapsed or
was rejected by the server (`vibe add <name>` fixes it), and it will not overwrite a Claude
Code login vibe has not stored (`vibe add` adopts it). Switching to an account whose access
token has expired makes one request to confirm it is alive, so that one case no longer
works offline.

**Usage numbers can be a few minutes old.** Anthropic's usage endpoint allows roughly 30
requests per account per hour, shared by everything that asks. Every `vibe` process
reuses one answer per account for about 3 minutes. An account that gets rate limited waits
1 to 8 minutes before its next try, longer if the server asks but never more than an hour,
and shows its last numbers and the time they are from in the meantime. Numbers more than
an hour old are not shown at all.

**The active account's token is Claude Code's to refresh.** Neither the dashboard nor
`vibe run` refreshes it, because refreshing one token from two places kills whichever side
goes second. If no Claude Code session has run since the token expired (about 8 hours),
the active account shows its last numbers for up to an hour and then a message saying
so, and `vibe run` on it asks you to start `claude` once. `vibe switch` and `vibe alias`
refuse while a token refresh or another switch is under way; run them again, or wait
30 seconds if one was killed mid-way.

**Windows is unsupported.** It says so rather than half-working.

## Uninstall

```
vibe auto uninstall         # only if you ran `vibe auto install`; also run the bootout line it prints
uv tool uninstall vibemaxxing
rm -rf ~/.vibemaxxing
```

That is everything it creates. Your Claude Code login is untouched; whichever account was
active stays active.

## Contributing

Questions go to [Discussions → Q&A](https://github.com/jskarbecki/vibemaxxing/discussions/categories/q-a);
bugs and pull requests to issues. [`CONTRIBUTING.md`](CONTRIBUTING.md) has the setup and
the one command CI runs. [`docs/CONTRACT.md`](docs/CONTRACT.md) is the frozen design and
the authority on any question of intent.

MIT licensed. Not affiliated with Anthropic.
