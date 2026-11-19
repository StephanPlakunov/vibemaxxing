#!/bin/zsh
# One-time switch to the shared login: extra's login moves from ~/.claude-extra into vibemaxxing,
# and every conversation that ran on extra is reopened on the shared login, where it left off.
#
# Run it in a NEW Terminal window (not inside a Claude session):  zsh ~/vibemaxxing/scripts/switch-to-shared-login.zsh
# With --after-pid <pid of the session you will restart> it closes the other extra sessions itself.
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

# 2. close every Claude session on extra as soon as it is idle, one loop for all of them
# Only Claude Code holds the login (a script or shell a session started merely inherits the
# variable). An interactive session is closed with SIGTERM once it has been idle for IDLE_S seconds
# (its conversation is on disk by then; a busy one is never interrupted); a session that starts
# meanwhile is noted and handled the same way; a headless `claude -p` helper is left to finish.
# --after-pid N first waits until session N has exited (the one you restart yourself).
after=""
[[ "${1:-}" == "--after-pid" ]] && after="${2:-}"
IDLE_S=30
idle_for() {  # seconds session $1 has been idle, from its own state file; -1 if busy or unknown
  python3 -I - "$EXTRA/sessions/$1.json" <<'EOF2'
import json, sys, time
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(-1); raise SystemExit
print(int(time.time() - d.get("statusUpdatedAt", 0) / 1000) if d.get("status") == "idle" else -1)
EOF2
}
claude_holders() {  # Claude Code processes running with CLAUDE_CONFIG_DIR=~/.claude-extra
  ps -E -o pid=,command= -U "$(id -u)" 2>/dev/null | grep -F "CLAUDE_CONFIG_DIR=$EXTRA" \
    | awk '{n=split($2,a,"/"); if (a[n]=="claude" || $2 ~ /\/claude\/versions\//) print $1}'
}
note() {  # remember session $1 for step 4, once
  local f="$EXTRA/sessions/$1.json"
  [[ -f "$f" ]] || return 1
  python3 -I - "$f" "$NOTES" <<'EOF3'
import json, sys
d = json.load(open(sys.argv[1]))
line = f"{d['sessionId']}\t{d['cwd']}\t{d.get('pid')}"
known = open(sys.argv[2]).read().splitlines() if __import__("os").path.exists(sys.argv[2]) else []
if not any(k.split("\t")[0] == d["sessionId"] for k in known):
    open(sys.argv[2], "a").write(line + "\n")
    print("  noted a new session:", d.get("name") or d["sessionId"])
EOF3
}
touch "$HOME/.vibemaxxing/releasing-extra"  # `vibe run extra` refuses to start a session there now
trap 'rm -f "$HOME/.vibemaxxing/releasing-extra"' EXIT
if [[ -n "$after" ]]; then
  say "Waiting until session $after has exited (restart it now)..."
  while kill -0 "$after" 2>/dev/null; do sleep 2; done
fi
say "Closing each session on extra once it has been idle for ${IDLE_S}s (busy ones finish first)."
while left=($(claude_holders)); (( ${#left} )); do
  waiting=()
  for pid in $left; do
    if note "$pid" >/dev/null 2>&1 || [[ -f "$EXTRA/sessions/$pid.json" ]]; then
      note "$pid"
      if (( $(idle_for "$pid") >= IDLE_S )); then
        kill -TERM "$pid" 2>/dev/null && print "  closed $(python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1])).get("name") or "")' "$EXTRA/sessions/$pid.json" 2>/dev/null) (pid $pid; idle, conversation kept)"
      else
        waiting+=("$pid")
      fi
    else
      waiting+=("$pid(headless)")
    fi
  done
  (( ${#waiting} )) && print -n "\r  waiting for: ${waiting[*]}            "
  sleep 5
done
print
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
