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
- **Commission** and **Currency**: not in the email — fill them from the IBKR
  lookup in step 3. **Exchange**: 1. **Account**: ib-us.

## 3. Look up commissions from IBKR
The confirmation emails carry no commission, so enrich each parsed trade from
the broker before writing it.
- Fetch fills with the IBKR MCP tool
  `mcp__claude_ai_Interactive_Brokers_IBKR__get_account_trades`, params
  `{"period":"DAYS_7"}`. If any parsed trade finds no match below, retry once
  with `{"period":"DAYS_30"}`. Do not request longer periods: they exceed the
  tool-result limit and spill to a file, which is fragile unattended.
- **Match** a parsed trade to IBKR fills on all of: `symbol` (the underlying,
  case-insensitive), `side` (BOUGHT→`BUY`, SOLD→`SELL`), and `price`. Allow the
  calendar date to differ by ±1 day — `trade_time` is UTC while the email `Date`
  is local (e.g. a 9961 sale dated 2026-07-02 in the email is
  `2026-07-03T03:22Z` at IBKR).
- **Sum `commission` over every matching fill.** One logical order is frequently
  several fills, and they collapse into a single sheet row: e.g. 1 contract
  @ 7.25 plus 2 contracts @ 7.30 is one row whose commission is both fills added
  together. Missing this undercounts the commission.
- **Tie-out guard**: the matched fills must account for the whole row —
  `sum(size) × 100` for options, or `sum(size)` for stocks, must equal the
  absolute value of the row's Amount. If it does not reconcile, or nothing
  matched at all, write Commission `0` and flag that row as unmatched in the
  step-7 report rather than guessing.
- Round the summed commission to 2 decimals.
- Set **Currency** from the matched fill's `currency`, lowercased — usually
  `usd`, but non-US trades (e.g. 9961 on SEHK) carry a commission denominated in
  the local currency such as `hkd`. Use `usd` when unmatched.
- A commission of exactly 0 can be legitimate: option exercises and assignments
  carry none. They show up as an `OPT` fill at price 0 next to a stock leg at
  the strike.

## 4. Target sheet
Spreadsheet id `1oxtcfl2V4ff3eUMW4954IChpx9eFAoB83QMrZERPSgA`, tab
`txn.<current calendar year>` (e.g. `txn.2026`). Columns in order:
Date, Ticker, Name, Price, Amount, Commission, Currency, Exchange, Account,
Diversity. Row 1 is the header; data rows are reverse-chronological (newest
first). Read the existing rows (UNFORMATTED_VALUE; dates come back as serials —
convert with epoch 1899-12-30).

## 5. Dedup + enrich
- **Skip** any parsed trade already present, matching on Date + Ticker + Price +
  Amount.
- For genuinely new trades, copy **Name** and **Diversity** from a prior row
  with the same Ticker; if none, use another option row for the same underlying;
  else set Name from the email and leave Diversity blank.

## 6. Write new trades
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

## 7. Report
Print one line: how many added, how many skipped as duplicates, how many of the
added rows got a commission from IBKR vs. were left at 0, and one brief, direct
sentence of trading feedback on the new trades (per the project's convention of
giving candid feedback when logging trades). If any row was left at 0 because it
did not match or tie out, name its ticker and date so it can be fixed by hand.
