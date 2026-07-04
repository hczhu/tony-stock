FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    nginx \
    cron \
    logrotate \
    git \
    curl \
    unzip \
    poppler-utils \
    imagemagick \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI v2 (official installer).
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/awscliv2.zip /tmp/aws

RUN pip install --no-cache-dir \
    jinja2 \
    python-dateutil \
    gspread \
    oauth2client \
    requests \
    beautifulsoup4

# Headless-browser scraper (scrapers/scrape_urls.py). Installs Playwright and
# Chromium with its system dependencies.
RUN pip install --no-cache-dir playwright \
    && playwright install --with-deps chromium

RUN mkdir -p /opt/smart-stock /var/www/smart-stocker

# Standalone utility scripts (e.g. the headless-browser scraper).
COPY scrapers/ /opt/scrapers/

COPY nginx-smart-stocker.conf /etc/nginx/sites-available/smart-stocker
RUN ln -sf /etc/nginx/sites-available/smart-stocker /etc/nginx/sites-enabled/smart-stocker \
    && rm -f /etc/nginx/sites-enabled/default

RUN echo '*/15 * * * * root cd /opt/smart-stock && /usr/local/bin/python3 smart-stocker.py > /tmp/portfolio.html.tmp 2>> /var/log/smart-stocker.log && [ -s /tmp/portfolio.html.tmp ] && mv /tmp/portfolio.html.tmp /var/www/smart-stocker/portfolio.html' \
    > /etc/cron.d/smart-stocker \
    && chmod 0644 /etc/cron.d/smart-stocker

# Daily: refresh the stock-screening trend reports (one HTML per sheet) into
# /var/www/smart-stocker/screening/. Uses the gspread backend (service-account
# key already in the image); snapshot goes to /tmp so it never dirties the
# mounted /opt/smart-stock git tree.
RUN echo '0 6 * * * root cd /opt/smart-stock && /usr/local/bin/python3 screening_cube_viz.py --fetch --snapshot /tmp/screening_snapshot.json >> /var/log/screening-cube.log 2>&1' \
    > /etc/cron.d/screening-cube \
    && chmod 0644 /etc/cron.d/screening-cube

# Daily (06:15 UTC, just after screening-cube): publish the screening trend
# reports to the tickertick.com S3 bucket. AWS creds come from the runtime-
# mounted /root/.aws (HOME=/root so the CLI finds them under cron); requires the
# tickertick_server IAM user to allow s3:PutObject/ListBucket on charts/.
RUN echo '15 6 * * * root HOME=/root /usr/local/bin/aws s3 sync /var/www/smart-stocker/screening s3://tickertick.com/charts >> /var/log/charts-s3-sync.log 2>&1' \
    > /etc/cron.d/charts-s3-sync \
    && chmod 0644 /etc/cron.d/charts-s3-sync

# Credentials are NOT baked into the image; run-tony-stock.sh bind-mounts them
# read-only at runtime (~/.smart-stocker-google-api.json, ~/.yahoo-finance.api-key.txt,
# ~/.aws) so secrets never end up in an image layer.

# Daily (16:00 UTC = midnight Taiwan time, UTC+8): append new Nanya monthly
# revenue rows to the "Nanya monthly revenue" sheet. Scrapes only the current
# year so each run is fast; idempotent (skips already-present rows).
# Scraper crons run from the live-mounted repo (/opt/tony-stock, bind-mounted at
# runtime) rather than the baked /opt/scrapers/ copy, so edits to a scraper take
# effect on the next run without an image rebuild.
RUN echo '0 16 * * * root /usr/local/bin/python3 /opt/tony-stock/scrapers/scrape_nanya_revenue.py --year $(date +\%Y) >> /var/log/nanya-revenue.log 2>&1' \
    > /etc/cron.d/nanya-revenue \
    && chmod 0644 /etc/cron.d/nanya-revenue

# Daily (05:00 UTC): refresh the OpenRouter top-model weekly-usage sheet. Keeps
# the current week's pace projection fresh and finalizes completed weeks. The
# script fetches everything it needs (no args) and is idempotent.
RUN echo '0 5 * * * root HOME=/root /usr/local/bin/python3 /opt/tony-stock/scrapers/scrape_openrouter_usage.py >> /var/log/openrouter-usage.log 2>&1' \
    > /etc/cron.d/openrouter-usage \
    && chmod 0644 /etc/cron.d/openrouter-usage

# Daily (16:30 UTC = 00:30 Taiwan time): append new TSMC monthly revenue rows to
# the "TSMC monthly revenue" sheet. The TSMC investor site is behind a Cloudflare
# JS challenge, so the scraper drives a real (non-headless) Chromium; it
# self-re-execs under xvfb-run when there is no X display. Scrapes only the
# current year; idempotent (skips already-present rows).
RUN echo '30 16 * * * root HOME=/root /usr/local/bin/python3 /opt/tony-stock/scrapers/scrape_tsmc_revenue.py --year $(date +\%Y) >> /var/log/tsmc-revenue.log 2>&1' \
    > /etc/cron.d/tsmc-revenue \
    && chmod 0644 /etc/cron.d/tsmc-revenue

# Daily (07:30 UTC): rotate the cron logs so they don't grow unbounded
# (smart-stocker.log in particular grows every 15 min). copytruncate keeps the
# same inode so the append-mode cron jobs keep writing without a restart.
COPY --chmod=0644 tony-stock-logrotate.conf /etc/logrotate.d/tony-stock-logs
RUN echo '30 7 * * * root /usr/sbin/logrotate --state /var/lib/logrotate/tony-stock.status /etc/logrotate.d/tony-stock-logs >> /var/log/logrotate.log 2>&1' \
    > /etc/cron.d/logrotate \
    && chmod 0644 /etc/cron.d/logrotate

COPY entrypoint-tony-stock.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
