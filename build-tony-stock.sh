#!/bin/bash
set -ex

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Credentials are no longer copied into the build context / image; they are
# bind-mounted read-only at runtime by run-tony-stock.sh.

docker build -f "$SCRIPT_DIR/tony-stock.Dockerfile" -t tony-stock "$SCRIPT_DIR"
