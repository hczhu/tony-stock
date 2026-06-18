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

cd "$HOME/tony-stock" || { echo "cannot cd to repo" >>"$LOG"; exit 1; }

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
  claude -p "$(cat "$PROMPT_FILE")" \
    --model sonnet \
    --dangerously-skip-permissions \
    2>&1
  echo
} >>"$LOG" 2>&1
