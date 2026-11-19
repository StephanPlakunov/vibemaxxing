# The token broker: one login per account, fixed and switching sessions side by side

`vibe broker on` makes vibe the **only refresher** of every Claude account it holds. Every Claude Code
login that Claude Code reads becomes a **mirror**: a Keychain item that vibe keeps filled with the
account's current access token and **no refresh token**.

- `vibe run main` / `vibe run extra` start Claude Code in that account's own mirror dir
  (`~/.claude-main`, `~/.claude-extra`). They set `CLAUDE_CONFIG_DIR` and no token variable, so the
  session stays on that account for good, never dies from its token, and the status line shows
  `▶ main (fixed)`.
- `cs`, `cw` and a plain `claude` use Claude Code's default item (`~/.claude`). It mirrors whichever
  account the background switch made live, so they follow the switch as before.
- There is exactly one login per account: the refresh token is in vibe's store and nowhere else, so
  nothing can log anything else out.

Off by default. With the broker off, nothing here changes behaviour.

## Why the old setup could not do it

- Every refresh rotates the refresh token, so two holders of one login log each other out.
- Before the broker, an account lived either in vibe's pool or in its own config dir, never both.
- `vibe run <pool account>` passed a bare `CLAUDE_CODE_OAUTH_TOKEN`. It cannot renew (8 h), and Claude
  Code gives it only `user:inference` scope (bundle 36661–36664: `ZT(["user:inference"])`), which
  means no claude.ai connectors and no Remote Control.

## Evidence: how Claude Code 2.1.318 treats a mirror

### Sources

- **The bundle.** Line numbers are of the bundle extracted from
  `~/.local/share/claude/versions/2.1.318` with `scripts/cc-mirror-experiment/extract_bundle.py`,
  then `LC_ALL=C tr ';' '\n'`. The result is 2879 modules, and the split file's sha256 starts
  `9e228c04dd980324`. Each line below was read in that file.
- **The experiments.** These ran the real 2.1.318 binary against:
  - a scratch config dir under `/tmp` with its own scratch Keychain item, deleted afterwards;
  - fake tokens only (`fake-at-A-not-a-real-token`);
  - a local fake Messages API (`ANTHROPIC_BASE_URL`);
  - a local proxy that refuses every outbound CONNECT and logs its host (`HTTPS_PROXY`), so a
    refresh attempt shows up as a `platform.claude.com` CONNECT and nothing leaves the machine.

  The scripts are in `scripts/cc-mirror-experiment/`.

### Bundle findings

| Question | What the bundle says | Where |
|---|---|---|
| When does it refresh? | `Cx(e)`: `e===null?false:now+300000>=e`, i.e. 5 min before `expiresAt` | 33901 |
| The refresh check, before every API client build | `yl`: `await sC(...)` (re-read, below); `if(U&&!Cx(U.expiresAt))return"not_needed"; if(!U?.refreshToken)return"no_refresh_token"` | 36836–36839 |
| Missing or null `refreshToken` | `!refreshToken` returns before any lock, request or write | 36839 |
| Empty-string `refreshToken` | `bl`/`QT`: `refreshToken===""` means *the login ended* | 36659–36661 |
| Junk refresh token | Gets POSTed; on `invalid_grant` the item is overwritten with `accessToken:"", expiresAt:0` | 36650–36653 |
| Picking up a replaced item without a 401 | `sC` stats `<dir>/.credentials.json`. If it is missing, `ml()` clears the memory cache and re-reads the store. The Keychain read itself is cached for `kon=30000` ms | 36716–36728, 21383 |
| On a 401 | `aj` clears both caches and re-reads. The API caller then compares `Hn()?.accessToken===rn`: a different token resets the retry counter and the client is rebuilt ("Credential rotated since the client was built"). The same token fails after 2 tries. With no refresh token, `aj` itself returns false without a refresh | 36758–36761, 132948–132953 |
| Scopes | Taken from the stored `scopes` array, never from the token. Connectors need `user:inference` and `user:mcp_servers` (79262–79267). Remote Control needs `user:inference`, `user:profile` and `oauthAccount.organizationUuid` (83717–83727) | — |
| How the item is written | Read-modify-write of the whole item (siblings: `mcpOAuth`, `pluginSecrets`, `trustedDeviceToken`, …) under a proper-lockfile lock `<dir>/.storage-write.lock`, stale after 15 s | 21455, 21299 |
| Item name | `"Claude Code-credentials" + "-" + sha256(NFC(CLAUDE_CONFIG_DIR))[0:8]` (no suffix without the variable) | 21379 |

### Experiments (fake tokens; scratch item deleted after each run)

| Run | Item | Fake API | Result |
|---|---|---|---|
| e1 | A, no refresh token, ends in 8 h | 401 for A; on that 401 it swaps the item to B | **`pong`**: A, then B; no `platform.claude.com`; item still without a refresh token |
| e2 | B, no refresh token, ends in **60 s** | accepts | **`pong`**; no `platform.claude.com` (no refresh attempt inside the 5-min window) |
| e3 (control) | A, **junk refresh token**, ends in 60 s | accepts | `pong`, and **2 CONNECTs to `platform.claude.com`**: the detector sees a refresh attempt |
| e4 | A, no refresh token | 401 for A, no swap | `Failed to authenticate`, exit 1 after 2 tries; no `platform.claude.com`; **item left as it was** |
| e5 | A, then B **35 s** later, between two turns of one process | accepts both | turn 1 used A, **turn 2 used B**, with no 401 (passive pick-up) |
| e6 | A, then B **3 s** later | accepts both | both turns used A: the 30 s Keychain cache. The old token still works until its end, and e1 shows the 401 path if it did not |

The only other hosts tried were `api.anthropic.com` (the profile fetch), which the proxy refused.

### Choice

A mirror holds the account's real `accessToken`, real `expiresAt`, real `scopes`, `subscriptionType`
and `rateLimitTier`, and **no `refreshToken` key at all**.

- **Leave the key out.** A missing key is "nothing to refresh with" (36839). An empty string means
  "this login ended". A sentinel would be POSTed and would wipe the item on `invalid_grant`.
- **The real expiry, not a far-future one.** vibe renews at half the lifetime and never later than an
  hour before the end, so Claude Code never reaches its 5-minute window while vibe works. e2 shows
  that even inside that window nothing is refreshed. If vibe cannot renew for hours, the real expiry
  lets the token end honestly when it ends. A far-future value would send a dead token on and hide
  the problem until a 401. Both variants avoid the token endpoint; the real value keeps vibe's own
  readers (renewal guard, status) truthful.
- **No `.credentials.json` in a mirror dir**, so Claude Code re-reads the Keychain item at most 30 s
  after vibe replaced it (e5).
- **Sibling members kept, written under Claude Code's own `.storage-write.lock`**, so neither side
  drops the other's write (an MCP server login, say).

Connectors and Remote Control: the mirror carries the same access token and the same `scopes` array as
the login it mirrors, so Claude Code's client-side checks pass as before. **Not verified against the
live service yet**; see the cutover plan.

## The design as built

| Part | What it does |
|---|---|
| `settings.json` → `"broker": {"on": bool, "mirrors": {alias: dir}, "refresh_left": 0.5}` | Off, and unreadable values, mean off. An alias with no entry mirrors into `~/.claude-<alias>`. `vibe auto on/off` keep the member. |
| `~/.vibemaxxing/broker.json` | Per account: issue time of the current token, last refresh, last problem, failures, next retry, and the mirrors written. |
| `broker.run` (each tick, before auto-switch, **also while auto-switch is off**) | Per account vibe holds (not Cursor, not a config-dir account): **take over** a full login Claude Code made in a mirror (`claude /login`) if it is the same account, newer, and not within 10 min of its end; **refresh** when due (half the lifetime left, at the latest 1 h before the end; through `oauth.refresh` with its claim, stash and dead-token mark); **rewrite every mirror** (its dir once the dir exists, plus the default item if it is the active account) when its content differs. |
| A failed refresh | Writes nothing: every mirror keeps its token until that really ends. Reported in `vibe broker status`, `vibe auto status`, `vibe list` (`broker problem: …`) and the tick line. Retries after 1, 2, 4, 8, then every 15 min. A rejected refresh token is marked dead and never posted again; the message says to report it and not to run `vibe add` (a new sign-in, impossible for extra). |
| A login vibe did not write (another account's) | Never written over; reported. |
| `oauth.refresh` | Refuses the active account only while the broker is off; with it on, the default item holds no refresh token and vibe refreshes every account. |
| `store.switch` | Resyncs the outgoing account from the default item only if that item holds a refresh token (never copies a mirror into the store). With the broker on, writes the incoming account into the default item as a mirror. |
| `auto.renewal_wait` | No wait for a mirror: Claude Code cannot renew one. |
| Polls (`vibe list`, TUI, web, tick) | With the broker on, the active account is read from the store like any other. |
| `vibe run <alias>` (and `vibe run auto` when it picks an account that is not live) | Broker on and the account is vibe's: create `~/.claude-<alias>` (0700) if missing, link `CLAUDE.md`, `settings.json`, `skills` and `projects` from `~/.claude`, refresh if due, write the mirror and `oauthAccount` (with `organizationUuid`) into `<dir>/.claude.json`, then start Claude Code with `CLAUDE_CONFIG_DIR=<dir>`, `VIBE_SESSION=<alias>`, and **no** `CLAUDE_CODE_OAUTH_TOKEN` (inherited ones are dropped). `--dry-run` writes nothing. A dead login starts nothing. |
| Status line | A session in a mirror dir is `▶ <alias> (fixed)`, from `VIBE_SESSION` or, without it, from the mirror dir in `settings.json`. `cs`/`cw`/plain `claude` show the live account. |
| `vibe broker on` | Needs vibe's active account to be the live one. Waits while the live login ends within 10 min (`auto.renewal_wait`). Under that account's refresh claim: takes the login Claude Code holds in the default item into the store, writes the mirror over it (siblings kept), sets `on`, then runs one pass. |
| `vibe broker off` | Under the active account's claim and Claude Code's write lock: refuses, changing nothing, when a switch landed since it read the active account or `~/.claude.json` names another account; otherwise sets `off` first, folds a refresh stash into the account file, then gives the live account's **whole** login back to the default item (Claude Code renews it again). Deletes every dir mirror (they could not renew). Leaves a login vibe did not write alone. Also undoes a `broker on` that stopped half way (the default item a mirror of the active account, the setting off). |
| Races | Every write of the default item (`switch`, `broker on/off`, a mirror) holds Claude Code's own `~/.claude/.storage-write.lock`; a mirror write asks again under that lock whether the broker is still on and (default item) the account still active, so a `broker off` or a switch in another process mid-pass is never undone. `broker on` sets `on` under the claim and the lock. |
| Guards | `vibe run <alias>` refuses a mirror dir holding a full login vibe did not take over, or whose `.claude.json` names another account. A dir an account lives in (`--from-config-dir`) is never a mirror. With the broker on, `vibe remove <live account>` and `vibe alias` of a brokered account are refused (vibe holds the only refresh token; the mirrors are keyed by name). |
| `vibe broker mirror <alias> [dir]` | Names the dir (default `~/.claude-<alias>`), creates and links it, and writes the mirror if the broker is on. Refuses `~/.claude` and a dir that already mirrors another account. |
| `vibe broker status` | Per account: when the token ends, when vibe renews it, its mirrors, and the last problem. |

### Limits and risks that remain

- **Each mirror write puts an access token in `security`'s argv** for one exec (README, Known
  limitations). Refresh tokens never go into a mirror. This happens once per renewal per mirror.
- **Running out of renewals:** if the tick does not run (launchd job unloaded, laptop asleep past a
  token's end), sessions fail with a 401 until the next tick. Then they recover by themselves within
  about 30 s (e1, e5).
- **`/logout` inside a mirror dir** removes the mirror (no refresh token to revoke); the next tick
  writes it again. **`/login` inside a mirror dir** makes a second login; the broker takes it over if
  it is the same account (and the newer one), else reports it.
- The first interactive start in a brand-new `~/.claude-main` may show Claude Code's onboarding
  (theme) once, because vibe writes only `oauthAccount` into its `.claude.json`.
- Not yet proven live: the server accepts a mirrored token for connectors and Remote Control, and a
  fixed session survives a renewal hours later. Both are in the cutover checks.
