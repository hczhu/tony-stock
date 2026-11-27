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

Fetches EC2 GPU / Trainium / Inferentia rental prices from AWS's official APIs
(via boto3, installed in the container) and replaces the **"AWS" worksheet** of
the "GPU&ASIC pricing" spreadsheet (`19rqW5crirhEEuz-lfcmoeB7aQPJW_T_7d8sOxTPhhM0`).
It watches mainstream AI chips: NVIDIA GPUs in the P instance family newer
than A100, and AWS Trainium / Inferentia2. Chips are identified by the names
`ec2:DescribeInstanceTypes` reports (not a hardcoded list), so new ones such
as NVIDIA Vera Rubin or Trainium4 appear automatically once AWS lists them.
Only instances with at least 8 accelerators (`--min-accel`). Each chip is
watched in the first region of `REGION_PREFERENCE` (largest first: us-east-1,
us-east-2, us-west-2, ...) that sells it, e.g. Trainium2/3 in us-east-2;
`--regions` overrides (`all` for every region). GB200 isn't covered: it's
only sold as UltraServer Capacity Blocks in the Dallas Local Zone, which the
API won't price for this account. One row per (instance type, region):

| Columns | Source |
|---|---|
| `on_demand_hr`, `ri_1yr_hr`, `ri_3yr_hr` | Price List API (`pricing:GetProducts`); RI = cheapest effective Standard rate (hourly + upfront amortized) |
| `spot_hr`, `spot_az` | `ec2:DescribeSpotPriceHistory`, cheapest AZ right now |
| `cb_hr`, `cb_start`, `cb_az` | `ec2:DescribeCapacityBlockOfferings`, cheapest 24 h offering (`--cb-hours`) |

Every price also has a `*_per_accel_hr` column (divided by the GPU / chip count).

```bash
python3 scrape_aws_accelerator_prices.py                        # → "AWS" sheet
python3 scrape_aws_accelerator_prices.py --dry-run --cheapest --match 'p5|p6|trn|inf'
python3 scrape_aws_accelerator_prices.py -o prices.csv          # CSV file instead
```

- AWS credentials: defaults to `--profile diag` (or `$AWS_PROFILE`), which has
  `pricing:GetProducts`, `pricing:GetAttributeValues`, `ec2:DescribeRegions`, `ec2:DescribeInstanceTypes`,
  `ec2:DescribeSpotPriceHistory` and `ec2:DescribeCapacityBlockOfferings`.
- Linux, shared tenancy only; China (CNY) regions skipped. Spot and Capacity
  Blocks are only queried in regions enabled for the account, not Local Zones.
- Not included: Savings Plans.
