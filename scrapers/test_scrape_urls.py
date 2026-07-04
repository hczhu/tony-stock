#!/usr/bin/env python3
"""Network integration test for scrape_urls.py.

Drives the scraper against the two known-good PDF URLs (also listed in
scrape_urls.py's docstring) and asserts each saved file is a *real* PDF. This
guards the two regressions fixed in that script:

  * Micron  -- an Akamai edge that 403s the native "HeadlessChrome" UA, so a
               too-plain request saves an "Access Denied" HTML page.
  * arxiv   -- Chromium's built-in PDF viewer hijacks the navigation, so
               resp.body() returns the viewer's HTML wrapper, not the PDF.

Both failure modes produce a tiny (<1 KB) HTML file where a multi-hundred-KB
PDF is expected, so the assertions check the "%PDF" magic bytes and a size
floor.

Requires Playwright + Chromium and network access, so it is meant to run
inside the tony-stock container:

    docker exec -w /opt/tony-stock tony-stock \
        python3 scrapers/test_scrape_urls.py
"""
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).with_name("scrape_urls.py")

# Akamai-fronted PDF: 403s the native headless UA -> needs DEFAULT_UA.
MICRON_PDF = (
    "https://investors.micron.com/static-files/"
    "2354ecda-77a0-4ddd-8462-a631eb491356"
)
# arxiv PDF: Chromium's built-in PDF viewer hijacks the navigation.
ARXIV_PDF = "https://arxiv.org/pdf/2607.01465"

# Real PDFs here are hundreds of KB to several MB; the failure modes save a
# sub-1 KB HTML wrapper/block page. 50 KB cleanly separates the two.
MIN_PDF_BYTES = 50_000


class ScrapeUrlsPdfTest(unittest.TestCase):
    def _assert_downloads_pdf(self, url):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = pathlib.Path(tmp)
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "-o", str(out_dir)],
                input=url + "\n",
                text=True,
                capture_output=True,
                timeout=180,
            )
            report = f"\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            self.assertEqual(proc.returncode, 0, msg=f"scraper exited nonzero{report}")

            pdfs = sorted(p for p in out_dir.iterdir() if p.suffix == ".pdf")
            names = [p.name for p in out_dir.iterdir()]
            self.assertEqual(len(pdfs), 1, msg=f"expected one .pdf, got {names}{report}")

            dest = pdfs[0]
            head = dest.read_bytes()[:5]
            self.assertTrue(
                head.startswith(b"%PDF"),
                msg=f"{dest.name} is not a PDF (starts with {head!r}){report}",
            )
            size = dest.stat().st_size
            self.assertGreater(
                size,
                MIN_PDF_BYTES,
                msg=f"{dest.name} is only {size} bytes -- likely an HTML "
                f"wrapper or block page, not the real PDF{report}",
            )

    def test_micron_akamai_pdf(self):
        self._assert_downloads_pdf(MICRON_PDF)

    def test_arxiv_viewer_pdf(self):
        self._assert_downloads_pdf(ARXIV_PDF)


if __name__ == "__main__":
    unittest.main()
