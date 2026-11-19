#!/bin/zsh
# usage: run.sh <name> <item-json> <reject-token> <swap-to-token or "">
set -u
NAME=$1 ITEM=$2 REJECT=$3 SWAP=$4
EXP=/tmp/broker-exp/$NAME; rm -rf $EXP; mkdir -p $EXP/cc
DIR=$EXP/cc
SERVICE="Claude Code-credentials-$(printf %s "$DIR" | shasum -a 256 | cut -c1-8)"
echo '{"hasCompletedOnboarding": true, "oauthAccount": {"accountUuid": "00000000-0000-0000-0000-00000000fake", "emailAddress": "fake@example.invalid", "organizationUuid": "00000000-0000-0000-0000-0000000000or"}}' > $DIR/.claude.json
/usr/bin/security add-generic-password -U -a "$USER" -s "$SERVICE" -w "$ITEM"
EXP_REJECT=$REJECT EXP_SWAP_TO=$SWAP python3 -I /tmp/broker-exp-tools/fakes.py $EXP 18911 18912 $DIR > $EXP/server.out 2>&1 &
SRV=$!; sleep 1
cd $EXP && env -i PATH=/usr/bin:/bin HOME=$HOME USER=$USER TERM=dumb \
  CLAUDE_CONFIG_DIR=$DIR ANTHROPIC_BASE_URL=http://127.0.0.1:18911 \
  HTTPS_PROXY=http://127.0.0.1:18912 HTTP_PROXY=http://127.0.0.1:18912 NO_PROXY=127.0.0.1,localhost \
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_AUTOUPDATER=1 \
  /usr/bin/perl -e "alarm shift; exec @ARGV" 120 /Users/stephanplakunov/.local/share/claude/versions/2.1.318 -p "say pong" --debug > $EXP/claude.out 2> $EXP/claude.err
echo "exit=$?" > $EXP/exit
/usr/bin/security find-generic-password -a "$USER" -s "$SERVICE" -w > $EXP/item-after.json 2>/dev/null
/usr/bin/security delete-generic-password -a "$USER" -s "$SERVICE" > /dev/null 2>&1; echo "item deleted: $?" >> $EXP/exit
kill $SRV 2>/dev/null
