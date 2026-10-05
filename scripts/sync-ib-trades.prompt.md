You are running as an unattended daily cron job on the user's machine. Sync
IBKR trades from the **Daily Trade Report** emails into the Transactions Google
spreadsheet. Be precise and idempotent. Do NOT ask for confirmation — run to
completion. Use the `gws` (Google Workspace CLI) for all Gmail and Sheets
access; it is already authenticated.

Why this source: IBKR stopped sending one-email-per-fill confirmations in early
September 2026, and the job silently found nothing for a month. The daily PDF
report also carries commissions directly and lists option expirations and
assignments, which per-fill emails never announced.

## 1. Find and download the reports
- `gws gmail users messages list --params '{"userId":"me","q":"subject:\"Daily Trade Report\" from:interactivebrokers newer_than:7d","maxResults":20}' --format json`
  (strip any leading "Using keyring backend" line before parsing JSON).
- For each message: `gws gmail users messages get --params '{"userId":"me","id":"<ID>","format":"full"}' --format json`,
  walk `payload.parts` for the `DailyTradeReport.YYYYMMDD.pdf` attachment, fetch it with
  `gws gmail users messages attachments get --params '{"userId":"me","messageId":"<ID>","id":"<ATTACHMENT_ID>"}' --format json`,
  and base64url-decode `data` into a fresh temp directory (`mktemp -d`).
- Reading a full week every run is intentional: dedupe (step 5) makes reruns
  safe, and it recovers days a failed run missed.

## 2. Extract text (via the container — the host has no PDF tools)
```
docker exec tony-stock mkdir -p /tmp/dtr-sync
docker cp <TMPDIR>/. tony-stock:/tmp/dtr-sync/
docker exec tony-stock bash -c 'cd /tmp/dtr-sync && for f in *.pdf; do pdftotext -layout "$f" "${f%.pdf}.txt"; done'
docker cp tony-stock:/tmp/dtr-sync/. <TMPDIR>/
docker exec tony-stock rm -rf /tmp/dtr-sync
```

## 3. Parse the Trades section
Each report has a `Trades` section that runs until `Financial Instrument
Information`. Inside it, asset-class headers (`Stocks`, `Equity and Index
Options`, `Forex`, …) are followed by a currency header (`USD`, `HKD`, …), then
per-fill rows and `Total` lines. Columns: Acct ID, Symbol, Trade Date/Time,
Settle Date, Exchange, Type, Quantity, Price, Proceeds, Comm In Base, Fee, Order
Type, Code.
- **Skip the `Forex` section entirely** — it is the daily USD.TWD / USD.HKD
  auto-conversion, which the sheet does not track.
- **Use the `Total <SYMBOL> (Bought)` / `Total <SYMBOL> (Sold)` lines, not the
  individual fills.** IBKR has already summed quantity and commission per
  contract per direction, which matches the sheet's one-row-per-order
  convention (e.g. a combo order filled across four exchanges is one Total per
  leg). Take the trade date from that symbol's fill row(s).
  If a symbol has only a single fill and no Total line, use the fill row.
- **Ticker**: options like `DRAM 18SEP26 40 P` → `dram-put@40` (`C` → `call`);
  stocks → the lowercase symbol (e.g. `mint`, `9961`).
- **Amount** (signed integer): options → contracts × 100; stocks → shares.
  Positive for bought, negative for sold — the report's sign already says so.
- **Price**: the report's price. **Expirations** (Code contains `Ep`, price 0)
  are written at price **0.01**, matching existing expiry rows. Assignments and
  exercises (`A`, `Ex`) are recorded as they appear — the option leg and the
  resulting stock leg are separate trade lines.
- **Commission**: absolute value of `Comm In Base` (2 decimals).
- **Currency**: the section's currency header, lowercased (`usd`, `hkd`).
- **Exchange**: copy from the most recent existing row with the same currency
  (`usd` is always 1).
- **Account**: `ib-us`. **Date**: `YYYY-MM-DD` from the trade date.

## 4. Target sheet
Spreadsheet id `1oxtcfl2V4ff3eUMW4954IChpx9eFAoB83QMrZERPSgA`, tab
`txn.<current calendar year>` (e.g. `txn.2026`). Columns in order:
Date, Ticker, Name, Price, Amount, Commission, Currency, Exchange, Account,
Diversity. Row 1 is the header; data rows are reverse-chronological (newest
first). Read the existing rows with `valueRenderOption: UNFORMATTED_VALUE`
(dates come back as serials — convert with epoch 1899-12-30; prices keep full
precision, so 100.315 is not mistaken for a displayed 100.32).

## 5. Dedup + enrich
- **Skip** any parsed trade already present, matching on Date + Ticker + Price
  (to 4 decimals) + Amount. Rows a human entered by hand count as present.
- For genuinely new trades, copy **Name** and **Diversity** from a prior row
  with the same Ticker; if none, use another row for the same underlying (e.g.
  `dram` for `dram-put@40`); else use the report's symbol as Name and leave
  Diversity blank.

## 6. Write new trades, keeping date order
New trades can be older than the newest row (a week is re-read), so do not
blindly insert at the top. For each new trade, find the first existing data row
whose Date is older than it and insert directly above that row:
- Insert with `gws sheets spreadsheets batchUpdate --params '{"spreadsheetId":"..."}'
  --json '{"requests":[{"insertDimension":{"range":{"sheetId":<TAB_SHEET_ID>,
  "dimension":"ROWS","startIndex":<ROW_INDEX_0_BASED>,"endIndex":<ROW_INDEX_0_BASED>+1},"inheritFromBefore":false}}]}'`
  (get `<TAB_SHEET_ID>` from `spreadsheets.get` fields `sheets.properties`).
  Process from the oldest new trade to the newest, re-reading row positions
  after each insert, or compute all indices up front and insert bottom-up.
- Write each row with `gws sheets spreadsheets values update ...
  "valueInputOption":"USER_ENTERED"` so the Date is stored as a real date.

If there are no new trades, change nothing.

## 7. Report
Print one line: reports read (with their date range), trades added, trades
skipped as duplicates. Then one brief, direct sentence of trading feedback on
the new trades (per the project's convention). If a new trade reverses a
recent trade in the same ticker (buying back what was recently sold, or
selling what was recently bought), say so explicitly with both prices.

**Staleness check:** if the newest Daily Trade Report found is more than 3
business days old, print a line starting `WARNING: no Daily Trade Report since`
with its date — that means IBKR's emails have stopped again and trades are not
being synced.
