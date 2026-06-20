#!/usr/bin/env python3
"""Scrape Nanya Technology monthly consolidated revenue and append to Google Sheets.

Source:  https://www.nanya.com/en/IR/36/Monthly%20Revenue?Year=YYYY  (one page per year)
Target:  "Nanya monthly revenue" sheet in spreadsheet 16_qvEStKUx_nwWoLoTeZRRaSuDlgxBmcPJnDdawsgaY

Sheet columns: Date | Revenue | MoM% | YoY%
  Date    — "YYYY-MM" string (e.g. "2026-04")
  Revenue — comma-formatted integer string (e.g. "25,491,201")
  MoM%    — percentage string with sign and suffix (e.g. "40.3%", "-3.1%")
  YoY%    — same

Idempotent: reads existing Date values from the sheet on start-up and only
inserts rows whose Date is not already present. Safe to re-run at any time.
Automatically migrates the old 5-column (Year, Month, …) format to this
4-column format on first run.

    python3 scrape_nanya_revenue.py                  # 2013 to current year
    python3 scrape_nanya_revenue.py --year 2026      # single year
    python3 scrape_nanya_revenue.py --start 2020     # from 2020 onward

If the page requires JavaScript rendering (table not found with plain HTTP),
re-run inside the tony-stock container using the --playwright flag.
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

SPREADSHEET_ID = "16_qvEStKUx_nwWoLoTeZRRaSuDlgxBmcPJnDdawsgaY"
SHEET_NAME = "Nanya monthly revenue"
BASE_URL = "https://www.nanya.com/en/IR/36/Monthly%20Revenue"
CREDENTIAL_PATH = os.environ.get(
    "SMART_STOCKER_CREDENTIAL",
    os.path.expanduser("~/.smart-stocker-google-api.json"),
)
FIRST_YEAR = 2013
HEADERS = ["Date", "Revenue", "MoM%", "YoY%"]
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


# --------------------------------------------------------------------------- #
# Google Sheets
# --------------------------------------------------------------------------- #

def _fmt_revenue(raw):
    """'25491201' → '25,491,201'"""
    try:
        return f"{int(str(raw).replace(',', '')):,}"
    except (ValueError, TypeError):
        return str(raw)


def _fmt_pct(raw):
    """'40.3' or '40.3%' → '40.3%'; empty → ''"""
    s = str(raw).strip().rstrip("%")
    return f"{s}%" if s else ""


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
    args = ap.parse_args()

    years = [args.year] if args.year else list(range(args.start, args.end + 1))

    print(f"Opening sheet '{SHEET_NAME}' ...", file=sys.stderr)
    ws = _open_worksheet(args.credential)
    _migrate_if_needed(ws)
    existing = get_existing_keys(ws)
    print(f"  {len(existing)} existing row(s)", file=sys.stderr)

    session = None
    if not args.playwright:
        session = requests.Session()
        session.headers.update(_REQUEST_HEADERS)

    js_warning_shown = False
    all_new_rows = []

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

        # Build formatted rows and filter already-present dates.
        full_rows = [
            [f"{year}-{m:02d}", _fmt_revenue(rev), _fmt_pct(mom), _fmt_pct(yoy)]
            for m, rev, mom, yoy in raw_rows
        ]
        new_rows = [r for r in full_rows if r[0] not in existing]
        for r in new_rows:
            existing.add(r[0])
        all_new_rows.extend(new_rows)
        print(f"{len(new_rows)} new row(s)", file=sys.stderr)

        time.sleep(args.delay)

    if all_new_rows:
        # Sheet is reverse-chronological; insert newest rows at the top (row 2,
        # just below the header) so they appear first.
        all_new_rows.sort(key=lambda r: (int(r[0]), int(r[1])), reverse=True)
        ws.insert_rows(all_new_rows, row=2, value_input_option="RAW")

    print(f"\nDone: {len(all_new_rows)} row(s) added to '{SHEET_NAME}'.", file=sys.stderr)


if __name__ == "__main__":
    main()
