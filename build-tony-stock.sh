#!/bin/bash
set -ex

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

trap 'rm -f "$SCRIPT_DIR/.smart-stocker-google-api.json" "$SCRIPT_DIR/.yahoo-finance.api-key.txt"' EXIT

cp "${HOME}/.smart-stocker-google-api.json" "$SCRIPT_DIR/.smart-stocker-google-api.json"
cp "${HOME}/.yahoo-finance.api-key.txt" "$SCRIPT_DIR/.yahoo-finance.api-key.txt"

docker build -f "$SCRIPT_DIR/tony-stock.Dockerfile" -t tony-stock "$SCRIPT_DIR"
