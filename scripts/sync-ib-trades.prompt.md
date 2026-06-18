You are running as an unattended daily cron job on the user's machine. Sync new
IBKR stock-trade confirmation emails from Gmail into the Transactions Google
spreadsheet. Be precise and idempotent. Do NOT ask for confirmation — run to
completion. Use the `gws` (Google Workspace CLI) for all Gmail and Sheets
access; it is already authenticated.

## 1. Read the trade emails
- `gws gmail users messages list --params '{"userId":"me","q":"label:IB-trades newer_than:7d","maxResults":50}' --format json`
- For each message id, fetch its Subject and Date:
  `gws gmail users messages get --params '{"userId":"me","id":"<ID>","format":"metadata","metadataHeaders":["Subject","Date"]}' --format json`

## 2. Parse each subject into a transaction
Each subject is one fill, e.g.
`BOUGHT 10 RBLX Jan21'28 140 CALL @ 3.81 (UXXX9719)`,
`SOLD 1 META Jan15'27 800 CALL @ 18.12`, or a stock `SOLD 200 MINT @ 100.52`.
- **Ticker**: options → `<underlying-lowercase>-<call|put>@<strike>` (e.g.
  `meta-call@800`, `dram-put@35`); stocks → `<underlying-lowercase>` (e.g. `mint`).
- **Amount** (signed integer quantity): options → contracts × 100; stocks →
  shares. Positive for BOUGHT, negative for SOLD.
- **Price**: the `@` price.
- **Date**: the email Date header's calendar date, formatted `YYYY-MM-DD`.
- **Commission**: 0 (not in the email). **Currency**: usd. **Exchange**: 1.
  **Account**: ib-us.

## 3. Target sheet
Spreadsheet id `1oxtcfl2V4ff3eUMW4954IChpx9eFAoB83QMrZERPSgA`, tab
`txn.<current calendar year>` (e.g. `txn.2026`). Columns in order:
Date, Ticker, Name, Price, Amount, Commission, Currency, Exchange, Account,
Diversity. Row 1 is the header; data rows are reverse-chronological (newest
first). Read the existing rows (UNFORMATTED_VALUE; dates come back as serials —
convert with epoch 1899-12-30).

## 4. Dedup + enrich
- **Skip** any parsed trade already present, matching on Date + Ticker + Price +
  Amount.
- For genuinely new trades, copy **Name** and **Diversity** from a prior row
  with the same Ticker; if none, use another option row for the same underlying;
  else set Name from the email and leave Diversity blank.

## 5. Write new trades
For each new trade, insert a row just below the header and write it so the newest
stays on top:
- Insert: `gws sheets spreadsheets batchUpdate --params '{"spreadsheetId":"..."}'
  --json '{"requests":[{"insertDimension":{"range":{"sheetId":<TAB_SHEET_ID>,
  "dimension":"ROWS","startIndex":1,"endIndex":1+<N_NEW>},"inheritFromBefore":false}}]}'`
  (get `<TAB_SHEET_ID>` from `spreadsheets.get` fields `sheets.properties`).
- Then `gws sheets spreadsheets values update --params '{"spreadsheetId":"...",
  "range":"txn.<year>!A2:J<1+N_NEW>","valueInputOption":"USER_ENTERED"}'
  --json '{"values":[[...],...]}'` so the Date is stored as a real date.

If there are no new trades, change nothing.

## 6. Report
Print one line: how many added, how many skipped as duplicates, and one brief,
direct sentence of trading feedback on the new trades (per the project's
convention of giving candid feedback when logging trades).
