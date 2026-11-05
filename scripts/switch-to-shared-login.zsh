#!/bin/zsh
# One-time switch to the shared login: extra's login moves from ~/.claude-extra into vibemaxxing,
# and every conversation that ran on extra is reopened on the shared login, where it left off.
#
# Run it in a NEW Terminal window (not inside a Claude session):  zsh ~/vibemaxxing/scripts/switch-to-shared-login.zsh
#
# What it does, in order:
#   1. Notes every Claude conversation that runs in ~/.claude-extra (id, folder, name).
#   2. Waits until all of them are closed (type /exit in each; nothing is lost: the conversation
#      stays on disk and is resumed in step 4). It never closes or kills anything itself.
#   3. Runs `vibe release extra`: extra's login moves into vibemaxxing, ~/.claude-extra keeps none.
#   4. Reopens each conversation in a new Terminal window with
#      `vibe run auto --no-needs --claude-only -- claude --resume <id>` in its own folder:
#      always Claude Code (a Claude conversation cannot continue in Cursor), on the shared login.
#   5. Shows `vibe auto status` and whether Cursor's status line entry is still there.
# Undo: vibe add extra --from-config-dir ~/.claude-extra --adopt-stored-login
set -u

EXTRA="$HOME/.claude-extra"
NOTES="$HOME/.vibemaxxing/switch-sessions.tsv"
VIBE="$HOME/.local/bin/vibe"

say() { print -P "%B$1%b"; }

if [[ -n "${CLAUDE_CONFIG_DIR:-}" || -n "${CLAUDECODE:-}" ]]; then
  say "Run this in a new Terminal window, not inside a Claude session (this shell is one)."
  exit 1
fi

holders() {
  # Every process of yours that runs with CLAUDE_CONFIG_DIR=~/.claude-extra (sessions and their children).
  ps -E -o pid=,command= -U "$(id -u)" 2>/dev/null | grep -F "CLAUDE_CONFIG_DIR=$EXTRA" | grep -v -e 'grep' -e 'ps -E' | awk '{print $1}'
}

# 1. note the conversations (kept in a file, so a second run still knows them)
if [[ ! -s "$NOTES" ]]; then
  : > "$NOTES"
  for f in "$EXTRA"/sessions/<->.json(N); do
    pid=${${f:t}%.json}
    kill -0 "$pid" 2>/dev/null || continue
    python3 -I - "$f" >> "$NOTES" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"{d['sessionId']}\t{d['cwd']}\t{d.get('pid')}")
EOF
  done
fi
say "Conversations on extra, to reopen afterwards:"
while IFS=$'\t' read -r sid cwd pid; do print "  $sid  in $cwd  (pid $pid)"; done < "$NOTES"

if [[ "${1:-}" == "--dry-run" ]]; then
  say "Dry run: open processes on extra: $(holders | tr '\n' ' ')"
  while IFS=$'\t' read -r sid cwd pid; do
    print "  would reopen: cd ${(q)cwd} && vibe run auto --no-needs --claude-only -- claude --resume $sid"
  done < "$NOTES"
  rm -f "$NOTES"
  exit 0
fi

# 2. wait until every one of them is closed
left=($(holders))
if (( ${#left} )); then
  say "Close each of them now: type /exit in its window (the conversation is kept and reopened by this script)."
  while left=($(holders)); (( ${#left} )); do
    print -n "\r  still open: ${#left} process(es) (${left[*]})   "
    sleep 3
  done
  print
fi
say "All closed."

# 3. move extra's login into vibemaxxing
say "vibe release extra"
if ! "$VIBE" release extra; then
  say "The release did not go through (see above); nothing else was changed. Fix it and run this script again."
  exit 1
fi
rm -f "$NOTES.done"; cp "$NOTES" "$NOTES.done"

# 4. reopen each conversation on the shared login, in its own folder
while IFS=$'\t' read -r sid cwd pid; do
  # single quotes for the shell, nothing AppleScript would read as an escape
  cmd="cd ${(qq)cwd} && vibe run auto --no-needs --claude-only -- claude --resume $sid"
  osascript - "$cmd" >/dev/null <<'APPLESCRIPT' && print "  reopened $sid in $cwd"
on run argv
  tell application "Terminal" to do script (item 1 of argv)
end run
APPLESCRIPT
done < "$NOTES"
rm -f "$NOTES"

# 5. check
say "vibe auto status"
"$VIBE" auto status
if grep -q '"statusLine"' "$HOME/.cursor/cli-config.json" 2>/dev/null; then
  print "Cursor status line: registered"
else
  print "Cursor status line: missing; tell the session (it re-adds it while no cursor-agent runs)"
fi
say "Done. extra now takes turns with main in Claude Code's own login; open sessions follow the switch."
