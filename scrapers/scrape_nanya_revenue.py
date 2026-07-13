#!/usr/bin/env python3
"""Scrape Nanya Technology monthly consolidated revenue and append to Google Sheets.

Source:  https://www.nanya.com/en/IR/36/Monthly%20Revenue?Year=YYYY  (one page per year)
Target:  "Nanya monthly revenue" sheet in spreadsheet 16_qvEStKUx_nwWoLoTeZRRaSuDlgxBmcPJnDdawsgaY

Sheet columns: Date | Revenue | MoM% | YoY% | Rolling 3M Revenue | 3M Growth %
  Date               — "YYYY-MM" string (e.g. "2026-04")
  Revenue            — integer number (NT$ thousands), displayed with a #,##0 format
  MoM%               — number (e.g. 0.403), displayed with a percent format
  YoY%               — same
  Rolling 3M Revenue — trailing 3-month revenue sum, rev(M)+rev(M-1)+rev(M-2)
  3M Growth %        — growth of the rolling 3M vs the previous, non-overlapping
                       3-month block: (rolling(M)-rolling(M-3))/rolling(M-3)*100

The last two columns (E, F) are derived from Revenue and recomputed on every
run, so they self-heal and stay in sync regardless of row order.

Idempotent: reads existing Date values from the sheet on start-up and only
inserts rows whose Date is not already present. Safe to re-run at any time.
Automatically migrates the old 5-column (Year, Month, …) format to this
4-column format on first run.

    python3 scrape_nanya_revenue.py                  # 2013 to current year
    python3 scrape_nanya_revenue.py --year 2026      # single year
    python3 scrape_nanya_revenue.py --start 2020     # from 2020 onward

If the page requires JavaScript rendering (table not found with plain HTTP),
re-run inside the tony-stock container using the --playwright flag.

Two sources:
  * /36 Monthly Revenue (plain HTTP): authoritative — precise revenue (NT$
    thousands) + MoM% + YoY%. Backfills and refines.
  * /15 Press Releases (non-headless Chromium; the list is JS-rendered and
    headless is blocked): announces each month's revenue promptly, before /36
    updates. Used only to add the latest month(s) early (revenue from the title,
    NT$ millions → thousands; MoM%/YoY% blank). /36 later refines those rows.
    Skip with --no-pr.
"""
import argparse
import os
import re
import shutil
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

SPREADSHEET_ID = "16_qvEStKUx_nwWoLoTeZRRaSuDlgxBmcPJnDdawsgaY"
SHEET_NAME = "Nanya monthly revenue"
BASE_URL = "https://www.nanya.com/en/IR/36/Monthly%20Revenue"
# Press Releases page announces each month's revenue promptly (before the /36
# Monthly Revenue table updates). Used as a supplement to add the latest month
# early; /36 later refines it with precise figures + MoM%/YoY%.
PR_URL = "https://www.nanya.com/en/IR/15/Press%20Releases"
# e.g. "Nanya Technology June 2026 Revenue NT$ 29,388 Million" (NT$ millions).
PR_TITLE_RE = re.compile(
    r"Nanya Technology\s+([A-Za-z]+)\s+(\d{4})\s+Revenue\s+NT\$?\s*([\d,]+)\s*Million",
    re.I,
)
CREDENTIAL_PATH = os.environ.get(
    "SMART_STOCKER_CREDENTIAL",
    os.path.expanduser("~/.smart-stocker-google-api.json"),
)
FIRST_YEAR = 2013
HEADERS = ["Date", "Revenue", "MoM%", "YoY%"]
# Derived columns (E, F) recomputed from Revenue on every run.
DERIVED_HEADERS = ["Rolling 3M Revenue", "3M Growth %"]
OLD_HEADERS_PREFIX = ["Year", "Month"]  # detect pre-migration format

MONTHS = [
    "January", "February", "March", "April",
    "May", "June", "July", "August",
    "September", "October", "November", "December",
]
MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS)}

_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nanya.com/en/IR/",
}


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #

def _parse_table(html):
    """Extract rows from the monthly revenue HTML table.

    Returns a list of [year_str, month_num_str, revenue_str, mom_str, yoy_str],
    or None if no table was found (page likely requires JS).
    """
    soup = BeautifulSoup(html, "html.parser")
    table = (
        soup.find("table", class_=re.compile(r"revenue|monthly|table", re.I))
        or soup.find("table")
    )
    if not table:
        return None

    rows = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        month_text = tds[0].get_text(strip=True)
        if month_text not in MONTH_NUM:
            continue

        revenue_raw = tds[1].get_text(strip=True).replace(",", "") if len(tds) > 1 else ""
        if not revenue_raw or not re.search(r"\d", revenue_raw):
            continue  # future month with no data yet

        mom = tds[2].get_text(strip=True).rstrip("%") if len(tds) > 2 else ""
        yoy = tds[3].get_text(strip=True).rstrip("%") if len(tds) > 3 else ""

        rows.append([MONTH_NUM[month_text], revenue_raw, mom, yoy])

    return rows


def fetch_year_requests(year, session):
    """Return parsed rows for *year* using plain HTTP, or None if JS is needed."""
    url = f"{BASE_URL}?Year={year}"
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return _parse_table(resp.text)


def fetch_year_playwright(year):
    """Return parsed rows for *year* using a headless Chromium browser."""
    from playwright.sync_api import sync_playwright

    url = f"{BASE_URL}?Year={year}"
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            channel="chromium",
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            locale="en-US",
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        html = page.content()
        ctx.close()
        browser.close()
    return _parse_table(html)


def _ensure_display():
    """Re-exec under xvfb-run if there is no X display. The Press Releases page
    blocks headless Chromium, so we render it with a *non-headless* browser,
    which needs a display."""
    if os.environ.get("DISPLAY"):
        return
    xvfb = shutil.which("xvfb-run") or "/usr/bin/xvfb-run"
    if os.path.exists(xvfb):
        os.execv(xvfb, [xvfb, "-a", "--server-args=-screen 0 1280x1400x24",
                        sys.executable, *sys.argv])


def fetch_press_release_revenue(year):
    """Render the /15 Press Releases page for *year* (non-headless Chromium; the
    list is JS-rendered and headless is blocked) and return
    {"YYYY-MM": revenue_thousands_int} parsed from revenue-announcement titles
    like "Nanya Technology June 2026 Revenue NT$ 29,388 Million".

    The title reports NT$ millions; we scale to NT$ thousands to match the sheet
    (and the /36 Monthly Revenue figures). The revenue month is taken from the
    title, not the publish date (so a December release published in January is
    keyed correctly)."""
    from playwright.sync_api import sync_playwright

    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False, channel="chromium",
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            locale="en-US", user_agent=_REQUEST_HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 1400},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome={runtime:{}};"
        )
        page = ctx.new_page()
        page.goto(f"{PR_URL}?Year={year}", wait_until="domcontentloaded",
                  timeout=60000)
        for _ in range(20):  # wait out the JS render of the list
            page.wait_for_timeout(1500)
            if re.search(r"Revenue\s+NT", page.content(), re.I):
                break
        for a in page.query_selector_all("a"):
            m = PR_TITLE_RE.search((a.inner_text() or "").replace("\n", " "))
            if not m:
                continue
            month = MONTH_NUM.get(m.group(1).capitalize())
            if not month:
                continue
            out[f"{int(m.group(2))}-{month:02d}"] = int(
                m.group(3).replace(",", "")) * 1000
        ctx.close()
        browser.close()
    return out


# --------------------------------------------------------------------------- #
# Google Sheets
# --------------------------------------------------------------------------- #

def _fmt_revenue(raw):
    """'25,491,201' → 25491201 (a real number, so Sheets stores it numerically)."""
    try:
        return int(str(raw).replace(",", ""))
    except (ValueError, TypeError):
        return str(raw)


def _fmt_pct(raw):
    """'40.3' or '40.3%' → 0.403 (a real number; display via percent format)."""
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


def _migrate_if_needed(ws):
    """Migrate old 5-column (Year, Month, Revenue, MoM%, YoY%) format in-place."""
    all_rows = ws.get_all_values()
    if not all_rows or all_rows[0][:2] != OLD_HEADERS_PREFIX:
        return  # already new format or empty
    print("  migrating to new format (Year+Month → Date, adding commas/%) ...",
          file=sys.stderr)
    new_rows = []
    for r in all_rows[1:]:
        if not r[0] or not r[1]:
            continue
        date = f"{r[0]}-{int(r[1]):02d}"
        revenue = _fmt_revenue(r[2]) if len(r) > 2 else ""
        mom = _fmt_pct(r[3]) if len(r) > 3 else ""
        yoy = _fmt_pct(r[4]) if len(r) > 4 else ""
        new_rows.append([date, revenue, mom, yoy])
    ws.clear()
    ws.update([HEADERS] + new_rows, value_input_option="RAW")
    print(f"  migrated {len(new_rows)} row(s)", file=sys.stderr)


def get_existing_keys(ws):
    """Return {date_str} for rows already in the sheet (e.g. '2026-04')."""
    all_rows = ws.get_all_values()
    return {r[0] for r in all_rows[1:] if r and r[0]}


def recompute_derived(ws):
    """(Re)write the Rolling-3M-Revenue and 3M-Growth-% columns (E, F) as live
    Google Sheets formulas that reference the Revenue column (B).

    Rows are reverse-chronological (newest at row 2), so for the month on row r,
    the two preceding months are rows r+1 and r+2:
      Rolling 3M Revenue = Revenue(r) + Revenue(r+1) + Revenue(r+2)
      3M Growth %        = (Rolling(r) - Rolling(r+3)) / Rolling(r+3)
                           i.e. vs the previous, non-overlapping 3-month block.

    Revenue cells are real numbers; each cell is still cleaned with
    REGEXREPLACE+VALUE inside the formula as a defensive measure (handles any
    legacy text cells). Cells resolve to "" until their trailing (3mo) / prior
    (6mo) window is fully populated. Formulas are rewritten for the current row
    layout on every run, so they stay correct after new rows are inserted at
    the top. Also (re)applies number formats to the scraped Revenue / MoM% /
    YoY% columns so inserted rows stay consistent.
    """
    all_rows = ws.get_all_values()
    n = len(all_rows)
    if n <= 1:
        return

    def _clean(cell):  # text like "$25,491,201" -> numeric value
        return f'VALUE(REGEXREPLACE(TO_TEXT({cell}),"[^0-9.-]",""))'

    out = [DERIVED_HEADERS]
    for r in range(2, n + 1):  # data rows 2..n
        rolling = (
            f'=IF(OR(B{r}="",B{r+1}="",B{r+2}=""),"",'
            f'{_clean(f"B{r}")}+{_clean(f"B{r+1}")}+{_clean(f"B{r+2}")})'
        )
        growth = (
            f'=IF(OR(E{r}="",E{r+3}="",N(E{r+3})=0),"",'
            f'(E{r}-E{r+3})/E{r+3})'
        )
        out.append([rolling, growth])

    ws.update(out, range_name=f"E1:F{n}", value_input_option="USER_ENTERED")
    # Display formats: thousands for revenue sums, percent for growth columns.
    ws.format(f"B2:B{n}", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}})
    ws.format(f"C2:D{n}", {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}})
    ws.format(f"E2:E{n}", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}})
    ws.format(f"F2:F{n}", {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}})
    print(f"  wrote derived-column formulas for {n - 1} row(s)", file=sys.stderr)


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
    ap.add_argument("--playwright", action="store_true",
                    help="use headless Chromium instead of plain HTTP (for JS-rendered pages)")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="seconds between year requests (default: 1.5)")
    ap.add_argument("--no-pr", action="store_true",
                    help="skip the Press Releases (/15) supplement for the latest month")
    args = ap.parse_args()

    if not args.no_pr:
        _ensure_display()  # may re-exec under xvfb-run and not return

    years = [args.year] if args.year else list(range(args.start, args.end + 1))

    print(f"Opening sheet '{SHEET_NAME}' ...", file=sys.stderr)
    ws = _open_worksheet(args.credential)
    _migrate_if_needed(ws)
    all_rows = ws.get_all_values()
    existing = {r[0] for r in all_rows[1:] if r and r[0]}
    # Rows whose MoM% (col C) is blank are candidates for /36 refinement — i.e.
    # /15-sourced supplement rows waiting for the precise Monthly Revenue figures.
    # Map date -> 1-based sheet row number.
    blank_mom = {r[0]: i + 1 for i, r in enumerate(all_rows)
                 if i >= 1 and r and r[0] and (len(r) < 3 or not str(r[2]).strip())}
    print(f"  {len(existing)} existing row(s)", file=sys.stderr)

    session = None
    if not args.playwright:
        session = requests.Session()
        session.headers.update(_REQUEST_HEADERS)

    js_warning_shown = False
    all_new_rows = []
    authoritative = set()   # months the /36 table has this run
    refines = []            # (sheet_row, [date, rev, mom, yoy]) to overwrite

    for year in years:
        print(f"  {year} ...", file=sys.stderr, end=" ")
        sys.stderr.flush()
        try:
            if args.playwright:
                raw_rows = fetch_year_playwright(year)
            else:
                raw_rows = fetch_year_requests(year, session)
        except Exception as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            continue

        if raw_rows is None:
            if not js_warning_shown:
                print(
                    "\nWARN: no table found in plain HTML — the page likely requires "
                    "JavaScript. Re-run with --playwright (inside the tony-stock "
                    "container where Playwright + Chromium are installed).",
                    file=sys.stderr,
                )
                js_warning_shown = True
            else:
                print("no table (JS?)", file=sys.stderr)
            continue

        # Build formatted rows; the /36 table is authoritative (precise revenue
        # + MoM%/YoY%). Insert genuinely-new months; refine any existing rows
        # that lack MoM% (i.e. earlier /15 supplements) with the precise data.
        new_here = 0
        for m, rev, mom, yoy in raw_rows:
            date = f"{year}-{m:02d}"
            row = [date, _fmt_revenue(rev), _fmt_pct(mom), _fmt_pct(yoy)]
            authoritative.add(date)
            if date not in existing:
                all_new_rows.append(row)
                existing.add(date)
                new_here += 1
            elif date in blank_mom and row[2]:
                refines.append((blank_mom.pop(date), row))
        print(f"{new_here} new row(s)", file=sys.stderr)

        time.sleep(args.delay)

    # Supplement: pull the latest month(s) from the Press Releases page and add
    # any not yet present (and not covered by /36 this run). Revenue only —
    # MoM%/YoY% stay blank until /36 publishes and refines the row.
    if not args.no_pr:
        pr_year = args.year or args.end
        print(f"  press releases {pr_year} ...", file=sys.stderr, end=" ")
        sys.stderr.flush()
        try:
            pr = fetch_press_release_revenue(pr_year)
            added = 0
            for date, rev in sorted(pr.items()):
                if date not in existing and date not in authoritative:
                    all_new_rows.append([date, _fmt_revenue(rev), "", ""])
                    existing.add(date)
                    added += 1
            print(f"{len(pr)} found, {added} supplemented", file=sys.stderr)
        except Exception as exc:
            print(f"FAIL: {exc}", file=sys.stderr)

    # Refine existing rows first (in place), before inserts shift row numbers.
    for sheet_row, row in refines:
        ws.update([row], range_name=f"A{sheet_row}:D{sheet_row}",
                  value_input_option="RAW")
    if refines:
        print(f"  refined {len(refines)} supplement row(s) with /36 data",
              file=sys.stderr)

    if all_new_rows:
        # Sheet is reverse-chronological; insert newest rows at the top (row 2,
        # just below the header) so they appear first.
        all_new_rows.sort(key=lambda r: r[0], reverse=True)
        ws.insert_rows(all_new_rows, row=2, value_input_option="RAW")

    # Always refresh the derived Rolling-3M / 3M-Growth columns (E, F) so they
    # stay in sync with the Revenue column, even on a no-new-rows run.
    recompute_derived(ws)

    print(f"\nDone: {len(all_new_rows)} row(s) added to '{SHEET_NAME}'.", file=sys.stderr)


if __name__ == "__main__":
    main()
