# Claude Code mirror experiment (docs/BROKER.md, "Experiments")

These are measurement tools, not part of vibe, and excluded from ruff. They never touch a real login:

- **Fake tokens only.** `fake-at-A-not-a-real-token`, etc.
- **One scratch config dir per run** under `/tmp/broker-exp/<name>`. Its own Keychain item,
  `Claude Code-credentials-<sha256 of that dir>`, is created at the start and **deleted at the end**.
- **A local fake Messages API** (`ANTHROPIC_BASE_URL=http://127.0.0.1:18911`).
- **A local proxy that refuses every CONNECT** (`HTTPS_PROXY=http://127.0.0.1:18912`) and logs the
  host. A refresh attempt shows up as `CONNECT platform.claude.com:443`, and nothing leaves the machine.

## Files

- `extract_bundle.py`: dumps the JS bundle of a Claude Code binary, read only.
- `fakes.py`: the fake API and the proxy.
  - `EXP_REJECT=<token>` answers 401 for that token.
  - `EXP_SWAP_TO=<token>` also swaps the scratch item to that token on the first 401 (what vibe does).
- `run.sh <name> <item-json> <reject-token> <swap-to-token>`: one `claude -p "say pong" --debug` run.
- `passive.py <exp-dir> <config-dir> <seconds>`: two turns in one process, with the item swapped between them.

Logs: `api.log` (which token each request carried), `proxy.log` (which hosts were tried), `item-after.json`.
