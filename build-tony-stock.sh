#!/bin/bash
set -ex

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

trap 'rm -f "$SCRIPT_DIR/.smart-stocker-google-api.json" "$SCRIPT_DIR/.yahoo-finance.api-key.txt" "$SCRIPT_DIR/.aws-credentials"' EXIT

cp "${HOME}/.smart-stocker-google-api.json" "$SCRIPT_DIR/.smart-stocker-google-api.json"
cp "${HOME}/.yahoo-finance.api-key.txt" "$SCRIPT_DIR/.yahoo-finance.api-key.txt"
cp "${HOME}/.aws/credentials" "$SCRIPT_DIR/.aws-credentials"

docker build -f "$SCRIPT_DIR/tony-stock.Dockerfile" -t tony-stock "$SCRIPT_DIR"
