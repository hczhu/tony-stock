#!/usr/bin/env python3
"""Scrape TSMC monthly consolidated revenue and append to Google Sheets.

Source:  https://investor.tsmc.com/english/monthly-revenue/YYYY  (one page per year)
Target:  "TSMC monthly revenue" sheet in spreadsheet 16_qvEStKUx_nwWoLoTeZRRaSuDlgxBmcPJnDdawsgaY

Sheet columns: Date | Revenue | MoM% | YoY% | Rolling 3 Month Revenue | Sequential Growth %
  Date                — "YYYY-MM" string (e.g. "2026-04")
  Revenue             — integer number (NT$ millions), displayed with a #,##0 format
  YoY%                — number (e.g. 0.368), displayed with a percent format
  MoM%                — derived: Rev(M)/Rev(M-1) - 1
  Rolling 3 Month Rev — derived: Rev(M) + Rev(M-1) + Rev(M-2)
  Sequential Growth % — derived: Rolling(M)/Rolling(M-3) - 1

The scraper only pulls Date/Revenue/YoY% from the page (TSMC reports YoY only);
the MoM%, Rolling and Sequential columns are written as live Sheets formulas
(recompute_derived), located by header name, and refreshed on every run.

The TSMC investor site sits behind a Cloudflare "Just a moment…" JavaScript
challenge, so a plain HTTP request is blocked. This scraper drives a real
(non-headless) Chromium via Playwright, which passes the challenge. When there
is no X display it transparently re-execs itself under ``xvfb-run``, so both
cron and interactive runs work with a plain ``python3 scrape_tsmc_revenue.py``.

Idempotent: reads existing Date values from the sheet on start-up and only
inserts rows whose Date is not already present. Safe to re-run at any time.

    python3 scrape_tsmc_revenue.py                  # 1999 to current year
    python3 scrape_tsmc_revenue.py --year 2026      # single year
    python3 scrape_tsmc_revenue.py --start 2020     # from 2020 onward
"""
import argparse
import os
import re
import shutil
import sys
import time
from datetime import datetime

SPREADSHEET_ID = "16_qvEStKUx_nwWoLoTeZRRaSuDlgxBmcPJnDdawsgaY"
SHEET_NAME = "TSMC monthly revenue"
BASE_URL = "https://investor.tsmc.com/english/monthly-revenue"
CREDENTIAL_PATH = os.environ.get(
    "SMART_STOCKER_CREDENTIAL",
    os.path.expanduser("~/.smart-stocker-google-api.json"),
)
FIRST_YEAR = 1999
HEADERS = ["Date", "Revenue", "YoY%"]

# "Jan." … "Dec." (May has no period; match on the first three letters).
MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------- #
# Virtual display: pass Cloudflare with a non-headless browser under Xvfb
# --------------------------------------------------------------------------- #

def _ensure_display():
    """Re-exec under xvfb-run if there is no X display (needed for non-headless
    Chromium, which is what gets past the Cloudflare challenge)."""
    if os.environ.get("DISPLAY"):
        return
    xvfb = shutil.which("xvfb-run") or "/usr/bin/xvfb-run"
    if os.path.exists(xvfb):
        os.execv(xvfb, [xvfb, "-a", "--server-args=-screen 0 1280x1000x24",
                        sys.executable, *sys.argv])
    print("WARN: no DISPLAY and no xvfb-run; Chromium may fail the Cloudflare "
          "challenge in headless mode.", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #

def _parse_rows(page):
    """Extract [(month_num, revenue_str, yoy_str), …] from the rendered page,
    or None if no table was found (challenge not passed)."""
    table = page.query_selector("table")
    if not table:
        return None
    rows = []
    for tr in table.query_selector_all("tr"):
        cells = [c.inner_text().strip() for c in tr.query_selector_all("th,td")]
        if len(cells) < 2:
            continue
        key = cells[0].rstrip(".").strip().lower()[:3]
        if key not in MONTH_NUM:
            continue  # header row or the trailing "Total" row
        revenue = cells[1].replace(",", "").strip()
        if not revenue or not re.search(r"\d", revenue):
            continue  # month with no data reported yet
        yoy = cells[2] if len(cells) > 2 else ""
        rows.append([MONTH_NUM[key], revenue, yoy])
    return rows


def fetch_year(page, year, timeout_s=45):
    """Navigate to *year* and return parsed rows (waiting out the CF challenge)."""
    page.goto(f"{BASE_URL}/{year}", wait_until="domcontentloaded", timeout=60000)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page.wait_for_timeout(1500)
        if page.query_selector("table"):
            break
    return _parse_rows(page)


# --------------------------------------------------------------------------- #
# Google Sheets
# --------------------------------------------------------------------------- #

def _fmt_revenue(raw):
    """'401,255' → 401255 (a real number, so Sheets stores it numerically)."""
    try:
        return int(str(raw).replace(",", ""))
    except (ValueError, TypeError):
        return str(raw)


def _fmt_pct(raw):
    """'36.8' or '36.8%' → 0.368 (a real number; display via percent format)."""
    s = str(raw).strip().rstrip("%")
    try:
        return float(s) / 100
    except ValueError:
        return ""


def _open_worksheet(credential_path):
    """Return the target gspread Worksheet, creating it (with headers) if absent."""
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(credential_path, scopes=scopes)
    book = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
    try:
        ws = book.worksheet(SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = book.add_worksheet(title=SHEET_NAME, rows=2000, cols=len(HEADERS))
        ws.update([HEADERS], value_input_option="RAW")
        print(f"  created sheet '{SHEET_NAME}' with headers", file=sys.stderr)
    return ws


def locate_columns(ws):
    """Return (width, idx_date, idx_rev, idx_yoy) for the sheet's header.

    The target sheet may carry extra columns the scraper does not populate
    (MoM%, Rolling 3 Month Revenue, Sequential Growth % — mirroring the Nanya
    sheet). We match columns by header name so scraped values land in the right
    place and those extra columns are never clobbered.
    """
    header = ws.row_values(1) or list(HEADERS)

    def find(pred, default):
        for i, h in enumerate(header):
            if pred(str(h).replace("\n", " ").strip().lower()):
                return i
        return default

    idx_date = find(lambda h: h.startswith("date") or h.startswith("month"), 0)
    idx_rev = find(lambda h: h.startswith("revenue"), 1)
    idx_yoy = find(lambda h: h.replace(" ", "").startswith("yoy"), 2)
    return max(len(header), idx_yoy + 1), idx_date, idx_rev, idx_yoy


def get_existing_keys(ws, idx_date):
    """Return {date_str} for rows already in the sheet (e.g. '2026-04')."""
    all_rows = ws.get_all_values()
    return {r[idx_date] for r in all_rows[1:] if len(r) > idx_date and r[idx_date]}


def _col_letter(idx0):
    """0-based column index → A1 letter (0→'A', 2→'C', 4→'E')."""
    s, n = "", idx0
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            return s


def recompute_derived(ws):
    """(Re)write the MoM%, Rolling 3 Month Revenue and Sequential Growth %
    columns as live formulas referencing the Revenue column.

    TSMC's page reports YoY only, so all three of these are derived here. Rows
    are reverse-chronological (newest at row 2), so for the month on row r the
    preceding months are rows r+1, r+2, … :
      MoM%                 = Rev(r) / Rev(r+1) - 1
      Rolling 3 Month Rev  = Rev(r) + Rev(r+1) + Rev(r+2)
      Sequential Growth %  = Rolling(r) / Rolling(r+3) - 1   (vs prior 3-mo block)

    Revenue cells are real numbers; each reference is still cleaned inline with
    REGEXREPLACE+VALUE as a defensive measure (handles any legacy text cells).
    Formulas resolve to "" until their window is populated, and are rewritten
    for the current row layout each run so they survive top-of-sheet inserts.
    Columns are located by header name. Also (re)applies number formats to the
    scraped Revenue and YoY columns so inserted rows stay consistent.
    """
    header = ws.row_values(1)
    n = len(ws.get_all_values())
    if n <= 1 or not header:
        return

    def find(pred):
        for i, h in enumerate(header):
            if pred(str(h).replace("\n", " ").strip().lower()):
                return i
        return None

    i_rev = find(lambda h: h.startswith("revenue"))
    i_yoy = find(lambda h: h.replace(" ", "").startswith("yoy"))
    i_mom = find(lambda h: h.replace(" ", "").startswith("mom"))
    i_roll = find(lambda h: "rolling" in h)
    i_seq = find(lambda h: h.startswith("sequential") or "growth" in h)
    if i_rev is None:
        print("  recompute: no Revenue column found; skipping", file=sys.stderr)
        return

    R = _col_letter(i_rev)
    E = _col_letter(i_roll) if i_roll is not None else None

    def clean(cell):  # defensive: numeric cells pass through, legacy text is cleaned
        return f'VALUE(REGEXREPLACE(TO_TEXT({cell}),"[^0-9.-]",""))'

    updates, fmts = [], []
    # Keep the scraped columns consistently formatted (values are real numbers).
    fmts.append((f"{R}2:{R}{n}", "num"))
    if i_yoy is not None:
        Y = _col_letter(i_yoy)
        fmts.append((f"{Y}2:{Y}{n}", "pct"))
    if i_mom is not None:
        C = _col_letter(i_mom)
        col = [[f'=IF(OR({R}{r}="",{R}{r+1}=""),"",'
                f'{clean(f"{R}{r}")}/{clean(f"{R}{r+1}")}-1)'] for r in range(2, n + 1)]
        updates.append((f"{C}2:{C}{n}", col))
        fmts.append((f"{C}2:{C}{n}", "pct"))
    if i_roll is not None:
        col = [[f'=IF(OR({R}{r}="",{R}{r+1}="",{R}{r+2}=""),"",'
                f'{clean(f"{R}{r}")}+{clean(f"{R}{r+1}")}+{clean(f"{R}{r+2}")})']
               for r in range(2, n + 1)]
        updates.append((f"{E}2:{E}{n}", col))
        fmts.append((f"{E}2:{E}{n}", "num"))
    if i_seq is not None and E is not None:
        F = _col_letter(i_seq)
        col = [[f'=IF(OR({E}{r}="",{E}{r+3}="",N({E}{r+3})=0),"",'
                f'{E}{r}/{E}{r+3}-1)'] for r in range(2, n + 1)]
        updates.append((f"{F}2:{F}{n}", col))
        fmts.append((f"{F}2:{F}{n}", "pct"))

    for rng, vals in updates:
        ws.update(vals, range_name=rng, value_input_option="USER_ENTERED")
    for rng, kind in fmts:
        pat = ({"type": "PERCENT", "pattern": "0.0%"} if kind == "pct"
               else {"type": "NUMBER", "pattern": "#,##0"})
        ws.format(rng, {"numberFormat": pat})
    print(f"  wrote MoM/Rolling/Sequential formulas for {n - 1} row(s)",
          file=sys.stderr)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--year", type=int, help="scrape a single year only")
    ap.add_argument("--start", type=int, default=FIRST_YEAR,
                    help=f"first year to scrape (default: {FIRST_YEAR})")
    ap.add_argument("--end", type=int, default=datetime.now().year,
                    help="last year to scrape (default: current year)")
    ap.add_argument("--credential", default=CREDENTIAL_PATH,
                    help="service-account JSON key (default: %(default)s)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between year requests (default: 1.0)")
    args = ap.parse_args()

    _ensure_display()  # may re-exec under xvfb-run and not return

    years = [args.year] if args.year else list(range(args.start, args.end + 1))

    print(f"Opening sheet '{SHEET_NAME}' ...", file=sys.stderr)
    ws = _open_worksheet(args.credential)
    width, idx_date, idx_rev, idx_yoy = locate_columns(ws)
    existing = get_existing_keys(ws, idx_date)
    print(f"  {len(existing)} existing row(s); columns "
          f"date={idx_date} revenue={idx_rev} yoy={idx_yoy} width={width}",
          file=sys.stderr)

    from playwright.sync_api import sync_playwright

    all_new_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False, channel="chromium",
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            locale="en-US", user_agent=_UA,
            viewport={"width": 1280, "height": 1000},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
            "window.chrome={runtime:{}};"
        )
        page = ctx.new_page()

        for year in years:
            print(f"  {year} ...", file=sys.stderr, end=" ")
            sys.stderr.flush()
            try:
                raw_rows = fetch_year(page, year)
            except Exception as exc:
                print(f"FAIL: {exc}", file=sys.stderr)
                continue

            if raw_rows is None:
                print("no table (Cloudflare not passed?)", file=sys.stderr)
                continue

            new_rows = []
            for m, rev, yoy in raw_rows:
                date = f"{year}-{m:02d}"
                if date in existing:
                    continue
                existing.add(date)
                row = [""] * width
                row[idx_date] = date
                row[idx_rev] = _fmt_revenue(rev)
                row[idx_yoy] = _fmt_pct(yoy)
                new_rows.append(row)
            all_new_rows.extend(new_rows)
            print(f"{len(new_rows)} new row(s)", file=sys.stderr)
            time.sleep(args.delay)

        ctx.close()
        browser.close()

    if all_new_rows:
        # Sheet is reverse-chronological; insert newest rows at the top (row 2,
        # just below the header) so they appear first. 'YYYY-MM' sorts by date.
        all_new_rows.sort(key=lambda r: r[idx_date], reverse=True)
        ws.insert_rows(all_new_rows, row=2, value_input_option="RAW")

    # Always refresh the derived formula columns (MoM% / Rolling / Sequential)
    # so they stay in sync with Revenue, even on a no-new-rows run.
    recompute_derived(ws)

    print(f"\nDone: {len(all_new_rows)} row(s) added to '{SHEET_NAME}'.", file=sys.stderr)


if __name__ == "__main__":
    main()
