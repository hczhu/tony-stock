# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Workflow rules

- Never push commits directly to `main`. Always create a branch, open a PR, and merge the PR.
- For code changes, make a plan and get confirmation before implementing. Non-code changes (docs, config) can proceed without confirmation.

## Repo structure

This repo orchestrates a stock investing dashboard. Key sub-repos cloned under the root (gitignored, not submodules):

- `smart-stock/` — the Python app that generates HTML reports; entry point is `smart-stocker.py`
- `code_recipes/` — shared utilities and dotfiles
- `Logseq-files/` — personal notes

## Docker image: tony-stock

The dashboard runs as a Docker container named `tony-stock`.

```bash
bash build-tony-stock.sh    # build image (copies credentials from ~ into context, cleans up on exit)
bash run-tony-stock.sh      # stop any running container, start fresh
bash deploy-tony-stock.sh   # build + install systemd service + start
```

**Runtime mounts:**
- `/home/hc/tony-stock/smart-stock` → `/opt/smart-stock` (live code, no rebuild needed for app changes)
- `/var/www/smart-stocker` → `/var/www/smart-stocker` (HTML output, persisted on host)

**Cron job** (every 15 min inside container): runs `smart-stocker.py`, writes stdout to a temp file, and atomically replaces `portfolio.html` only on success.

**Nginx** listens on port 8888 (host network):
- `http://<host>:8888/` → redirects to `portfolio.html`
- `http://<host>:8888/smart-stocker/` → directory listing of all output files

## Credentials (build-time, never committed)

`build-tony-stock.sh` copies these from `~` into the build context and removes them on exit:
- `~/.smart-stocker-google-api.json` → `/root/` in image (Google Sheets access via gspread/oauth2client)
- `~/.yahoo-finance.api-key.txt` → `/root/` in image (Yahoo Finance API)

## Stock trading spreadsheet

Spreadsheet ID: `1oxtcfl2V4ff3eUMW4954IChpx9eFAoB83QMrZERPSgA`. One sheet per year (`txn.YYYY`), rows in reverse chronological order. When adding rows: copy `Name` and `Diversity` from prior rows with the same ticker; keep `Date` as a date type (not string). Use `gws sheets` commands to read/write via the Google Workspace CLI (`~/.local/bin/gws`).

## Updating the web server after a smart-stock code change

After merging a PR in smart-stock, run inside the container to refresh immediately:

```bash
docker exec tony-stock bash -c "cd /opt/smart-stock && python3 smart-stocker.py > /tmp/portfolio.html.tmp && mv /tmp/portfolio.html.tmp /var/www/smart-stocker/portfolio.html"
```
