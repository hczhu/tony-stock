# scrapers

Standalone utility scripts for scraping content. Designed to run **inside the
`tony-stock` container**, which ships Playwright + headless Chromium (installed
via `tony-stock.Dockerfile`).

## scrape_urls.py

Reads URLs from **stdin** (one per line; blank lines and `#` comments ignored)
and saves what each yields into an output directory using headless Chromium:

| URL yields | Saved as |
|---|---|
| a direct file (PDF, xlsx, zip, …) | that file, named from `Content-Disposition` or the URL |
| a download triggered by the page | the download, with its suggested filename |
| a plain HTML page | the **rendered** DOM as `.html` |

If navigation fails at the network layer (e.g. `ERR_HTTP2_PROTOCOL_ERROR` on
some CDNs), it falls back to the browser context's HTTP client (shares
cookies/UA); those rows are tagged `file*`.

### Rendering JS pages & bot walls

To capture what a human actually sees, the scraper runs the **full Chromium in
new-headless mode** (`channel="chromium"` — the default `headless_shell`
advertises `HeadlessChrome` and is trivially flagged), with the automation flag
hidden and a realistic context (locale / timezone / `Accept-Language`). For
HTML pages it waits for `networkidle` and then waits out interstitial
bot-checks (e.g. Cloudflare's "Just a moment…") before saving.

Some sites use a **strict bot wall** (notably Cloudflare's *managed challenge*)
that headless browsers — even patched ones — cannot pass. When the challenge
never clears, the row is tagged **`challenge`** and printed as a `WARN`: the
saved `.html` is the placeholder, *not* the real content, so it is **not**
written to `download_log.tsv` (a later run will retry). These sites simply
can't be scraped with this tool; use a real browser session or a dedicated
anti-bot service.

### Download log (skip already-downloaded URLs)

Each successful download appends a line to `download_log.tsv` in the output
dir: `<saved filename>\t<URL>`. On startup the script reads this log and
**skips** any URL already present, so re-running the same `urls.txt` only
fetches new ones. Delete the log (or the line) to force a re-download.

### Usage

```bash
# inside the container (script is baked in at /opt/scrapers/ after a rebuild)
echo "https://example.com/file.pdf" | python3 /opt/scrapers/scrape_urls.py -o /out
python3 /opt/scrapers/scrape_urls.py -o /out < urls.txt

# from the host, against the running container
docker exec -i tony-stock python3 /opt/scrapers/scrape_urls.py -o /tmp/scraped < urls.txt
docker cp tony-stock:/tmp/scraped ./scraped     # copy results back out
```

Options: `-o/--output DIR` (default `./scraped`), `--timeout SECONDS`
(default 60), `--user-agent STR`.

### Notes

- The Dockerfile installs the browser; the change takes effect after a rebuild:
  `bash deploy-tony-stock.sh` (or `bash build-tony-stock.sh && bash run-tony-stock.sh`).
- The container mounts `/var/www/smart-stocker` and `/opt/smart-stock` from the
  host; write output to a mounted path, or `docker cp` results out as above.

## scrape_aws_accelerator_prices.py

Records EC2 GPU / Trainium / Inferentia rental prices from AWS's official APIs
(via boto3, installed in the container) as a daily history in the **"AWS"
worksheet** of the "GPU&ASIC pricing" spreadsheet
(`19rqW5crirhEEuz-lfcmoeB7aQPJW_T_7d8sOxTPhhM0`):

- Header rows: `Date`, then one column group per accelerator (H100, H200,
  B200, B300, Inferentia2, Trainium, Trainium2, Trainium3, ...) with three
  sub-columns: On-Demand, 1-year RI and Spot **hour rate per accelerator**,
  averaged over that accelerator's instance types.
- Each run writes one row: it overwrites today's row if present, else inserts
  a new row at the top (newest first). Older rows are never rewritten.
- Watched chips come from `ec2:DescribeInstanceTypes`, not a hardcoded list:
  NVIDIA P-family GPUs newer than A100 and AWS Trainium / Inferentia2. New
  chips (Vera Rubin, Trainium4, ...) get a new column group automatically.
- Only instances with at least 8 accelerators (`--min-accel`). Each chip is
  priced in the first `REGION_PREFERENCE` region that sells it (us-east-1,
  then us-east-2, ...); `--regions` overrides.

```bash
python3 scrape_aws_accelerator_prices.py              # upsert today's row
python3 scrape_aws_accelerator_prices.py --dry-run    # print it as CSV instead
```

- AWS credentials: defaults to `--profile diag` (or `$AWS_PROFILE`), which has
  `pricing:GetProducts`, `ec2:DescribeRegions`, `ec2:DescribeInstanceTypes`
  and `ec2:DescribeSpotPriceHistory`.
- GB200 isn't covered: it's only sold as UltraServer Capacity Blocks in the
  Dallas Local Zone, which the API won't price for this account.
