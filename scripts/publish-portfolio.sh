#!/usr/bin/env bash
# Render the portfolio dashboard and publish it to the nginx web root.
#
# Guards against two failure modes, both observed in production:
#
#  * A degraded render. When the container briefly loses outbound network
#    (seen daily around 14:15 and 23:15 UTC), smart-stocker.py cannot reach
#    Google OAuth, so it has no spreadsheet data -- yet it still emits
#    structurally valid HTML and exits 0. A bare "is the file non-empty" test
#    accepts that page, publishing a dashboard that renders blank until the
#    next good run. We require the render to actually contain data rows.
#
#  * A torn read. /tmp is the container's overlay filesystem while
#    /var/www/smart-stocker is a host bind mount, so mv between them cannot be
#    a rename(2): it truncates the destination and refills it, and nginx can
#    serve the half-written file. Staging inside the destination directory
#    keeps the move on one filesystem, where it is atomic.
set -u

OUT_DIR=/var/www/smart-stocker
OUT="$OUT_DIR/portfolio.html"
TMP="$OUT_DIR/.portfolio.html.tmp"   # same filesystem as $OUT, so mv is atomic
LOG=/var/log/smart-stocker.log
MIN_ROWS=20                          # healthy page has ~135 <tr>; a dataless one has 0

log() { echo "$(date -u '+%Y-%m-%d %H:%M:%S') publish: $*" >> "$LOG"; }

cd /opt/smart-stock || { log "FAIL cannot cd to /opt/smart-stock"; exit 1; }

if ! /usr/local/bin/python3 smart-stocker.py > "$TMP" 2>> "$LOG"; then
    log "FAIL smart-stocker.py exited non-zero; keeping previous page"
    rm -f "$TMP"
    exit 1
fi

rows=$(grep -o '<tr' "$TMP" | wc -l)
if [ "$rows" -lt "$MIN_ROWS" ] || ! grep -q '</html>' "$TMP"; then
    log "FAIL render unusable ($rows data rows, $(stat -c%s "$TMP") bytes); keeping previous page"
    rm -f "$TMP"
    exit 1
fi

mv "$TMP" "$OUT"
exit 0
