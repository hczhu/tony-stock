#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

clone_or_pull() {
  local url="$1"
  local dir="$2"
  if [ -d "$SCRIPT_DIR/$dir/.git" ]; then
    echo "[$dir] already cloned, pulling latest..."
    git -C "$SCRIPT_DIR/$dir" pull
  else
    echo "[$dir] cloning..."
    git clone "$url" "$SCRIPT_DIR/$dir"
  fi
}

clone_or_pull git@github.com:hczhu/Logseq-files.git        Logseq-files
clone_or_pull https://github.com/hczhu/code_recipes         code_recipes
clone_or_pull git@github.com:hczhu/smart-stock.git          smart-stock
clone_or_pull git@github.com:hczhu/stock-research-memos.git stock-research-memos

echo "Done."
