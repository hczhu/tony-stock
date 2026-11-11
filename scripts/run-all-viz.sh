#!/usr/bin/env bash
# Run every chart/visualization job on demand -- the same steps the daily crons
# run, in order. Meant to be run inside the tony-stock container:
#
#   docker exec tony-stock bash /opt/tony-stock/scripts/run-all-viz.sh [flags]
#
# Steps:
#   1. (--scrape) the data scrapers that feed the chart sheets: OpenRouter
#      usage, Nanya monthly revenue, TSMC monthly revenue.
#   2. screening_cube_viz.py, which renders every chart page (the screening
#      tabs plus the Memory, AI demand, Meta vs. Google, Datadog, ServiceNow,
#      Cloudflare and SaaS customers pages) and the index into
#      /var/www/smart-stocker/screening/.
#   3. (--s3) upload the pages to s3://tickertick.com/charts.
#
# Flags:
#   --scrape    run the scrapers first
#   --s3        upload to S3 afterwards
#   --no-fetch  render from the saved snapshots instead of refetching the sheets
set -euo pipefail

SCRAPE=0 S3=0 FETCH=--fetch
for arg in "$@"; do
    case "$arg" in
        --scrape)   SCRAPE=1 ;;
        --s3)       S3=1 ;;
        --no-fetch) FETCH= ;;
        -h|--help)  sed -n '2,/^set /p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown flag: $arg (try --help)" >&2; exit 2 ;;
    esac
done

PY=/usr/local/bin/python3
SCRAPERS=/opt/tony-stock/scrapers
export HOME=/root
START=$SECONDS

step() { echo; echo "==> $(date -u '+%H:%M:%S') $*"; }

if (( SCRAPE )); then
    step "OpenRouter usage"
    "$PY" "$SCRAPERS/scrape_openrouter_usage.py"
    step "Nanya monthly revenue"
    "$PY" "$SCRAPERS/scrape_nanya_revenue.py" --year "$(date +%Y)"
    step "TSMC monthly revenue"
    "$PY" "$SCRAPERS/scrape_tsmc_revenue.py" --year "$(date +%Y)"
fi

step "Chart pages + index${FETCH:+ (fetching sheets)}"
# Snapshot to /tmp, as the cron does, so the mounted git tree stays clean.
(cd /opt/smart-stock && "$PY" screening_cube_viz.py $FETCH --snapshot /tmp/screening_snapshot.json)

if (( S3 )); then
    step "Upload to s3://tickertick.com/charts"
    aws s3 sync /var/www/smart-stocker/screening s3://tickertick.com/charts
fi

echo; echo "Done in $((SECONDS - START))s."
