#!/usr/bin/env python3
"""Headless-browser URL scraper.

Reads URLs from stdin (one per line; blank lines and lines starting with '#'
are ignored) and saves what each URL yields into an output directory using a
headless Chromium browser (Playwright):

  * a direct file (PDF, xlsx, zip, ...) -> saved as that file
  * a download triggered by the page    -> saved with its suggested filename
  * a plain HTML page                    -> saved as rendered .html

Designed to run inside the tony-stock container, where Playwright + Chromium
are installed (see tony-stock.Dockerfile).

Usage:
    echo "https://example.com/file" | python3 scrape_urls.py -o /out
    python3 scrape_urls.py -o /out < urls.txt
"""
import argparse
import mimetypes
import os
import pathlib
import re
import sys
from urllib.parse import unquote, urlparse

from playwright.sync_api import (
    Error as PWError,
    TimeoutError as PWTimeout,
    sync_playwright,
)

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Record of successful downloads, written into the output dir. One line per
# download: "<saved filename>\t<URL>". Used to skip already-downloaded URLs.
LOG_NAME = "download_log.tsv"


def load_downloaded_urls(log_path):
    """Return the set of URLs already recorded in the download log."""
    seen = set()
    if log_path.exists():
        with log_path.open(encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2 and parts[1]:
                    seen.add(parts[1])
    return seen

EXT_BY_CT = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "application/zip": ".zip",
    "text/csv": ".csv",
    "application/json": ".json",
    "text/plain": ".txt",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}


def safe_name(s):
    """Sanitize a string into a safe filename."""
    s = unquote(s or "").strip().replace("/", "_").replace("\\", "_")
    s = re.sub(r"[^A-Za-z0-9._-]", "_", s)
    s = s.strip("._") or "download"
    return s[:150]


def filename_from_disposition(disposition):
    if not disposition:
        return None
    # RFC 5987 filename*=UTF-8''... takes precedence over plain filename=
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disposition, re.I)
    if not m:
        m = re.search(r'filename="?([^";]+)"?', disposition, re.I)
    return safe_name(m.group(1)) if m else None


def derive_name(url, content_type=None, disposition=None):
    name = filename_from_disposition(disposition)
    if not name:
        parsed = urlparse(url)
        base = os.path.basename(parsed.path.rstrip("/"))
        name = safe_name(base) if base else safe_name(parsed.netloc)
    root, ext = os.path.splitext(name)
    if not ext and content_type:
        ct = content_type.split(";")[0].strip().lower()
        ext = EXT_BY_CT.get(ct) or mimetypes.guess_extension(ct) or ""
        name = name + ext
    return name


def unique_path(out_dir, name):
    dest = out_dir / name
    stem, ext = os.path.splitext(name)
    i = 1
    while dest.exists():
        dest = out_dir / f"{stem}_{i}{ext}"
        i += 1
    return dest


def scrape_one(context, url, out_dir, timeout_ms):
    """Fetch one URL; return (dest_path, size_bytes, kind)."""
    page = context.new_page()
    download_box = {}
    page.on("download", lambda d: download_box.setdefault("d", d))
    resp = None
    nav_err = None
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except PWError as e:
        nav_err = e

    # A navigation that turns into a file download aborts with ERR_ABORTED;
    # wait a bit for the download event to arrive.
    if "d" not in download_box and nav_err is not None and "ERR_ABORTED" in str(nav_err):
        try:
            page.wait_for_event("download", timeout=min(timeout_ms, 15000))
        except PWTimeout:
            pass

    try:
        if "d" in download_box:
            dl = download_box["d"]
            name = safe_name(dl.suggested_filename) or derive_name(url)
            dest = unique_path(out_dir, name)
            dl.save_as(str(dest))
            return dest, dest.stat().st_size, "download"

        if resp is None:
            # Navigation failed at the network layer (e.g. ERR_HTTP2_PROTOCOL_ERROR
            # on some CDNs). Fall back to the browser context's HTTP client, which
            # shares cookies/UA but uses a different network stack.
            api = context.request.get(url, timeout=timeout_ms)
            if not api.ok:
                raise RuntimeError(f"HTTP {api.status} (and {nav_err})")
            ctype = (api.headers.get("content-type") or "").split(";")[0].strip().lower()
            body = api.body()
            name = derive_name(
                url,
                content_type=ctype or "application/octet-stream",
                disposition=api.headers.get("content-disposition"),
            )
            dest = unique_path(out_dir, name)
            dest.write_bytes(body)
            return dest, dest.stat().st_size, "file*"

        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype in ("", "text/html", "application/xhtml+xml"):
            try:
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
            except PWTimeout:
                pass
            name = derive_name(url, content_type="text/html")
            if not name.lower().endswith((".html", ".htm")):
                name += ".html"
            dest = unique_path(out_dir, name)
            dest.write_text(page.content(), encoding="utf-8")
            return dest, dest.stat().st_size, "html"

        body = resp.body()
        name = derive_name(
            url, content_type=ctype, disposition=resp.headers.get("content-disposition")
        )
        dest = unique_path(out_dir, name)
        dest.write_bytes(body)
        return dest, dest.stat().st_size, "file"
    finally:
        page.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", default="scraped", help="output directory (default: ./scraped)")
    ap.add_argument("--timeout", type=float, default=60.0, help="per-URL timeout in seconds (default: 60)")
    ap.add_argument("--user-agent", default=DEFAULT_UA, help="override the browser User-Agent")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    urls = [
        ln.strip()
        for ln in sys.stdin
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not urls:
        print("No URLs on stdin.", file=sys.stderr)
        return 1

    log_path = out_dir / LOG_NAME
    seen = load_downloaded_urls(log_path)

    timeout_ms = int(args.timeout * 1000)
    ok = 0
    skipped = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-http2"],
        )
        context = browser.new_context(user_agent=args.user_agent, accept_downloads=True)
        with log_path.open("a", encoding="utf-8") as log:
            for url in urls:
                if url in seen:
                    print(f"SKIP            {url} (already in {LOG_NAME})")
                    skipped += 1
                    continue
                try:
                    dest, size, kind = scrape_one(context, url, out_dir, timeout_ms)
                    print(f"OK   [{kind:8}] {url} -> {dest} ({size:,} bytes)")
                    log.write(f"{dest.name}\t{url}\n")
                    log.flush()
                    seen.add(url)
                    ok += 1
                except Exception as e:  # noqa: BLE001 - keep going on per-URL failures
                    print(f"FAIL            {url} :: {e}", file=sys.stderr)
        context.close()
        browser.close()

    print(f"\nDone: {ok} saved, {skipped} skipped, {len(urls) - ok - skipped} failed -> {out_dir}")
    return 0 if ok + skipped == len(urls) else 2


if __name__ == "__main__":
    sys.exit(main())
