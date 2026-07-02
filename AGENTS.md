# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Workflow rules

- Never push commits directly to `main` or `master` on **any repo** in this project (tony-stock, smart-stock, Logseq-files, code_recipes, stock-research, learning-notes). Always create a branch, open a PR, and merge the PR.
- For code changes, make a plan and get confirmation before implementing. Non-code changes (docs, config) can proceed without confirmation.

## Repo structure

This repo orchestrates a stock investing dashboard. Key sub-repos cloned under the root (gitignored, not submodules):

- `smart-stock/` — the Python app that generates HTML reports; entry point is `smart-stocker.py`
- `code_recipes/` — shared utilities and dotfiles
- `Logseq-files/` — personal notes and trading logs
- `stock-research/` — all stock memo files (moved from Logseq-files/pages/stock-memos/)
- `learning-notes/` — personal technical learning notes (AI/ML, inference, semiconductors, tools) in Logseq md format

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

**Cron jobs** (inside container, defined in `tony-stock.Dockerfile`):
- `smart-stocker` (every 15 min): runs `smart-stocker.py`, writes stdout to a temp file, and atomically replaces `portfolio.html` only on success.
- `screening-cube` (daily 06:00 UTC): runs `screening_cube_viz.py --fetch` to regenerate the stock-screening trend reports (see below).

**Nginx** listens on port 8888 (host network):
- `http://<host>:8888/` → redirects to `portfolio.html`
- `http://<host>:8888/smart-stocker/` → directory listing of all output files
- `http://<host>:8888/smart-stocker/screening/` → stock-screening trend reports (`index.html` + one per sheet)

## Credentials (runtime bind-mounts, never baked into the image)

`run-tony-stock.sh` bind-mounts these from `~` into the container **read-only**
at runtime, so secrets never end up in an image layer:
- `~/.smart-stocker-google-api.json` → `/root/.smart-stocker-google-api.json` (Google Sheets via gspread/oauth2client)
- `~/.yahoo-finance.api-key.txt` → `/root/.yahoo-finance.api-key.txt` (Yahoo Finance API)
- `~/.aws` → `/root/.aws` (AWS CLI credentials/config)

## Stock trading advice

- When adding trading records, also provide brief unsolicited feedback on trading behavior — e.g. "you are trading too much", "this looks like revenge trading", "consider position sizing", etc. Be direct and honest.
- When giving investment suggestions or answering questions about holdings/trades, read stock memos from `stock-research/` and trading logs from `Logseq-files/` for context on the user's thesis, notes, and prior reasoning before responding.

## Stock trading spreadsheet

Spreadsheet ID: `1oxtcfl2V4ff3eUMW4954IChpx9eFAoB83QMrZERPSgA`. One sheet per year (`txn.YYYY`), rows in reverse chronological order. When adding rows: copy `Name` and `Diversity` from prior rows with the same ticker; keep `Date` as a date type (not string). Use `gws sheets` commands to read/write via the Google Workspace CLI (`~/.local/bin/gws`).

For **real-time stock prices**, read the `Prices` sheet of the same spreadsheet (columns: `Ticker`, `Price`, `Change`, `Marketcap $B`, `Name`, `Premium%`). It has live quotes — use it to value positions / compute unrealized P&L instead of the IBKR MCP or external price APIs.

For **trade confirmations**, search Gmail with the label `ib-trades` (e.g. `label:ib-trades`, or `label:ib-trades newer_than:7d`). IBKR's TradingAssistant sends one email per fill; the full trade is in the subject line, e.g. `BOUGHT 10 RBLX Jan21'28 140 CALL @ 3.81` or `SOLD 200 MINT @ 100.52`. These subjects carry the full option contract details (strike, expiry, call/put) — use them to reconcile or back-fill the transaction sheet.

## Stock screening trend reports

`smart-stock/screening_cube_viz.py` visualizes the "Stock screening" spreadsheet
(ID `1K4m1h_0RqYlouCKhVP4WXpN_UtSJHnfVoLucvYYGhYk`), which is a `company × metric ×
quarter` data cube — each tab stacks quarterly slabs (quarter label in column A,
companies down the rows, metrics across the columns).

It generates one self-contained HTML report per cube-shaped tab (plus an
`index.html`), each with two tabs: **Trends** (small-multiples line charts, one
panel per metric) and **Scatter** (Gapminder-style X/Y/size bubbles with a
quarter slider). Tabs that aren't a quarter×company×metric cube (e.g. Payment,
DD) are skipped.

```bash
# default: render from the committed snapshot, no network → /var/www/smart-stocker/screening/
python3 screening_cube_viz.py
python3 screening_cube_viz.py --fetch          # refresh data from the sheet first
python3 screening_cube_viz.py --sheets SaaS    # limit to specific tabs
python3 screening_cube_viz.py --out /tmp/x     # render elsewhere
```

- **Data backends** (`--backend`, default `auto`): `gspread` (uses the
  service-account key, works in the container) or `gws` (the CLI, host/dev use).
  `auto` picks `gspread` when the key exists.
- **Snapshot**: committed at `smart-stock/data/screening_snapshot.json` so the
  default run needs no network. The daily cron writes its snapshot to `/tmp`
  instead, to avoid dirtying the mounted `/opt/smart-stock` git tree.
- The daily refresh happens automatically via the `screening-cube` cron. To
  refresh on demand:

```bash
docker exec tony-stock bash -c "cd /opt/smart-stock && python3 screening_cube_viz.py --fetch --snapshot /tmp/screening_snapshot.json"
```

## Updating the web server after a smart-stock code change

After merging a PR in smart-stock, run inside the container to refresh immediately:

```bash
docker exec tony-stock bash -c "cd /opt/smart-stock && python3 smart-stocker.py > /tmp/portfolio.html.tmp && mv /tmp/portfolio.html.tmp /var/www/smart-stocker/portfolio.html"
```
