#!/usr/bin/env bash
# Daily sync of IBKR trade-confirmation emails (Gmail label:IB-trades) into the
# Transactions Google spreadsheet, via a headless Claude Code run.
# Installed as a weekday 1:30pm local cron job (see `crontab -l`).
# Logs to ~/.cron-logs/sync-ib-trades.log.
set -u

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
HERE="$(cd "$(dirname "$0")" && pwd)"
PROMPT_FILE="$HERE/sync-ib-trades.prompt.md"
LOG="$HOME/.cron-logs/sync-ib-trades.log"
mkdir -p "$(dirname "$LOG")"

# Keep the log bounded: rotate when it exceeds ~1 MB, keeping one previous file.
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  mv -f "$LOG" "$LOG.1"
fi

cd "$HOME/tony-stock" || { echo "cannot cd to repo" >>"$LOG"; exit 1; }

# Authenticate with the long-lived token from `claude setup-token` rather than
# the interactive login, which expires and made this job fail on 2026-09-30 and
# 10-01. The file lives outside the repo, mode 600. If it is absent, fall back
# to the interactive login.
TOKEN_FILE="$HOME/.config/claude/cron-oauth-token"
if [ -r "$TOKEN_FILE" ]; then
  export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$TOKEN_FILE")"
fi

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
  claude -p "$(cat "$PROMPT_FILE")" \
    --model sonnet \
    --dangerously-skip-permissions \
    </dev/null \
    2>&1
  echo
} >>"$LOG" 2>&1
