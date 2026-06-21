#!/usr/bin/env python3
"""Scrape OpenRouter top-model weekly usage and upsert into Google Sheets.

Source:  https://openrouter.ai/rankings  (the "Top Models" weekly-usage chart)
  Backed by the public frontend API:
    https://openrouter.ai/api/frontend/v1/rankings/model-rankings-chart
    https://openrouter.ai/api/frontend/v1/catalog/models   (permaslug -> short_name)
Target:  "Openrouter top model weekly usage" sheet in spreadsheet
         1D4T49GIdN8Ksme1NjxGVddUcH08YgqXmigzE5_ARfX0

Sheet layout (one row per week, reverse-chronological — newest at the top):
    Date | Total | <model columns ...>
  Date    — "YYYY-MM-DD" (the week-start Monday, e.g. "2026-06-15")
  Total   — total usage that week, in trillions of tokens
  <model> — that model's usage that week, in trillions of tokens (blank if the
            model was not in the week's top list)

Model columns use OpenRouter's friendly short_name (e.g. "DeepSeek V4 Flash").
Free / non-standard variants keep their suffix, e.g. "DeepSeek V3 0324 (free)",
so they stay distinct columns matching the chart. The catch-all bucket is
"Others".

Current (incomplete) week: the chart's last week is always in progress. The
site shows a "Weekly Pace" projection on top of the partial bar; we replicate it
as a linear time-extrapolation and scale every value (and the Total) by
    factor = oneWeekMS / (cachedAt - weekStartUTC)   (clamped to >= 1)
so the row represents the projected full week. On a later run, once the week has
completed, the API returns the final actuals and the projection is overwritten.

Idempotent: re-reads the existing sheet and merges. Weeks returned by the API
(the trailing ~52) overwrite the matching rows; older weeks already in the sheet
are preserved (the API window slides forward over time). Safe to re-run anytime.

    python3 scrape_openrouter_usage.py            # fetch + upsert
    python3 scrape_openrouter_usage.py --dry-run  # show what would change
    python3 scrape_openrouter_usage.py --json resp.json   # use a saved response
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

import requests

SPREADSHEET_ID = "1D4T49GIdN8Ksme1NjxGVddUcH08YgqXmigzE5_ARfX0"
SHEET_NAME = "Openrouter top model weekly usage"
CHART_URL = "https://openrouter.ai/api/frontend/v1/rankings/model-rankings-chart"
CATALOG_URL = "https://openrouter.ai/api/frontend/v1/catalog/models"
CREDENTIAL_PATH = os.environ.get(
    "SMART_STOCKER_CREDENTIAL",
    os.path.expanduser("~/.smart-stocker-google-api.json"),
)

OTHERS = "Others"
TRILLION = 1e12
ONE_WEEK_MS = 7 * 24 * 3600 * 1000

_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://openrouter.ai/rankings",
}


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch_chart(session):
    """Return (weeks, cached_at_ms) from the model-rankings-chart endpoint.

    weeks is a list of {"x": "YYYY-MM-DD", "ys": {permaslug: tokens, ...}}.
    """
    resp = session.get(CHART_URL, timeout=30)
    resp.raise_for_status()
    inner = resp.json()["data"]
    return inner["data"], inner.get("cachedAt")


def fetch_name_map(session):
    """Return {permaslug: short_name} from the catalog endpoint."""
    resp = session.get(CATALOG_URL, timeout=60)
    resp.raise_for_status()
    out = {}
    for item in resp.json().get("data", []):
        slug = item.get("permaslug")
        name = item.get("short_name")
        if slug and name:
            out[slug] = name
    return out


# --------------------------------------------------------------------------- #
# Transforms
# --------------------------------------------------------------------------- #

def display_name(key, name_map):
    """Map a chart key (permaslug, maybe with a ':variant' suffix) to a column.

    "deepseek/deepseek-v4-flash-20260423"      -> "DeepSeek V4 Flash"
    "deepseek/deepseek-chat-v3-0324:free"      -> "DeepSeek V3 0324 (free)"
    "Others"                                   -> "Others"
    unknown permaslug                          -> the permaslug itself
    """
    if key == OTHERS:
        return OTHERS
    base, _, variant = key.partition(":")
    name = name_map.get(base, base)
    if variant and variant != "standard":
        name = f"{name} ({variant})"
    return name


def _sig(x, n=3):
    """Round x to n significant figures (returns a float; 0 stays 0.0)."""
    if x == 0 or x is None:
        return 0.0
    return round(x, -int(math.floor(math.log10(abs(x)))) + (n - 1))


def week_factor(week_x, cached_at_ms):
    """Pace-projection multiplier for the current (incomplete) week.

    Linear time-extrapolation: oneWeek / elapsed-since-week-start. Clamped to
    >= 1 so a just-completed week is never scaled down.
    """
    if not cached_at_ms:
        return 1.0
    start_ms = datetime.fromisoformat(week_x + "T00:00:00+00:00").timestamp() * 1000
    elapsed = cached_at_ms - start_ms
    if elapsed <= 0:
        return 1.0
    return max(1.0, ONE_WEEK_MS / elapsed)


def build_week_rows(weeks, cached_at_ms, name_map):
    """Return {date: {column: tokens}} with the last week pace-projected.

    Values are raw token counts (floats). Keys that collapse to the same column
    within a week are summed.
    """
    out = {}
    last_idx = len(weeks) - 1
    for i, w in enumerate(weeks):
        factor = week_factor(w["x"], cached_at_ms) if i == last_idx else 1.0
        cols = {}
        for key, tokens in w["ys"].items():
            col = display_name(key, name_map)
            cols[col] = cols.get(col, 0.0) + tokens * factor
        out[w["x"]] = cols
    return out


# --------------------------------------------------------------------------- #
# Google Sheets
# --------------------------------------------------------------------------- #

def _open_worksheet(credential_path):
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
        ws = book.add_worksheet(title=SHEET_NAME, rows=400, cols=80)
        print(f"  created sheet '{SHEET_NAME}'", file=sys.stderr)
    return ws


def read_existing(ws):
    """Return (header_models, {date: {column: float_trillions}}) from the sheet.

    header_models is the existing model-column order (excludes Date, Total).
    """
    rows = ws.get_all_values()
    if not rows:
        return [], {}
    header = rows[0]
    # columns after Date, Total are model columns
    model_cols = header[2:] if len(header) > 2 else []
    data = {}
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        date = r[0]
        cols = {}
        for j, col in enumerate(model_cols):
            idx = j + 2
            if idx < len(r) and r[idx] not in ("", None):
                try:
                    cols[col] = float(str(r[idx]).replace(",", ""))
                except ValueError:
                    pass
        data[date] = cols
    return model_cols, data


def merge(existing_data, new_rows_tokens):
    """Merge API weeks (token counts) over existing sheet weeks (trillions).

    Returns {date: {column: float_trillions}}. API weeks overwrite matching
    dates; existing-only weeks (older than the API window) are preserved.
    """
    merged = dict(existing_data)  # already in trillions
    for date, cols in new_rows_tokens.items():
        merged[date] = {c: v / TRILLION for c, v in cols.items()}
    return merged


def order_columns(prior_order, merged):
    """Order model columns by usage in the most recent week (descending).

    Missing values count as 0; ties (incl. models absent from the latest week)
    break alphabetically. prior_order is unused — column order is recomputed
    each run so it always reflects current popularity.
    """
    if not merged:
        return []
    latest = max(merged)
    cols = {c for week in merged.values() for c in week}
    latest_week = merged[latest]
    return sorted(cols, key=lambda c: (-latest_week.get(c, 0.0), c))


def build_sheet_matrix(merged, model_order):
    """Return [header, *rows] with rows reverse-chronological, values formatted."""
    header = ["Date", "Total"] + model_order
    out = [header]
    for date in sorted(merged, reverse=True):  # newest first
        cols = merged[date]
        total = sum(cols.values())
        row = [date, _sig(total)]
        for col in model_order:
            v = cols.get(col)
            row.append(_sig(v) if v not in (None, 0, 0.0) else "")
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--credential", default=CREDENTIAL_PATH,
                    help="service-account JSON key (default: %(default)s)")
    ap.add_argument("--json", help="use a saved chart-API response instead of fetching")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resulting table instead of writing to the sheet")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(_REQUEST_HEADERS)

    if args.json:
        with open(args.json) as f:
            inner = json.load(f)["data"]
        weeks, cached_at = inner["data"], inner.get("cachedAt")
    else:
        print("Fetching chart ...", file=sys.stderr)
        weeks, cached_at = fetch_chart(session)
    print(f"  {len(weeks)} week(s); cachedAt={cached_at}", file=sys.stderr)

    print("Fetching catalog (name map) ...", file=sys.stderr)
    name_map = fetch_name_map(session)
    print(f"  {len(name_map)} model name(s)", file=sys.stderr)

    new_rows = build_week_rows(weeks, cached_at, name_map)
    last_x = weeks[-1]["x"]
    factor = week_factor(last_x, cached_at)
    print(f"  current week {last_x} pace factor = {factor:.4f}", file=sys.stderr)

    if args.dry_run and not os.path.exists(args.credential):
        prior_order, existing = [], {}
    else:
        ws = _open_worksheet(args.credential)
        prior_order, existing = read_existing(ws)
    print(f"  {len(existing)} existing row(s), {len(prior_order)} existing column(s)",
          file=sys.stderr)

    merged = merge(existing, new_rows)
    model_order = order_columns(prior_order, merged)
    matrix = build_sheet_matrix(merged, model_order)

    print(f"  {len(matrix) - 1} total row(s), {len(model_order)} model column(s)",
          file=sys.stderr)

    if args.dry_run:
        for row in matrix[:6]:
            print(" | ".join(str(c) for c in row[:8]))
        print(f"... ({len(matrix) - 1} data rows x {len(matrix[0])} cols)")
        return

    ws.clear()
    ws.update(matrix, value_input_option="RAW")
    print(f"\nDone: wrote {len(matrix) - 1} row(s) to '{SHEET_NAME}'.", file=sys.stderr)


if __name__ == "__main__":
    main()
