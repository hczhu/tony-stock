FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    nginx \
    cron \
    git \
    curl \
    poppler-utils \
    imagemagick \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    jinja2 \
    python-dateutil \
    gspread \
    oauth2client \
    requests

RUN mkdir -p /opt/smart-stock /var/www/smart-stocker

COPY nginx-smart-stocker.conf /etc/nginx/sites-available/smart-stocker
RUN ln -sf /etc/nginx/sites-available/smart-stocker /etc/nginx/sites-enabled/smart-stocker \
    && rm -f /etc/nginx/sites-enabled/default

RUN echo '*/15 * * * * root cd /opt/smart-stock && /usr/local/bin/python3 smart-stocker.py > /tmp/portfolio.html.tmp 2>> /var/log/smart-stocker.log && mv /tmp/portfolio.html.tmp /var/www/smart-stocker/portfolio.html' \
    > /etc/cron.d/smart-stocker \
    && chmod 0644 /etc/cron.d/smart-stocker

# Daily: refresh the stock-screening trend reports (one HTML per sheet) into
# /var/www/smart-stocker/screening/. Uses the gspread backend (service-account
# key already in the image); snapshot goes to /tmp so it never dirties the
# mounted /opt/smart-stock git tree.
RUN echo '0 6 * * * root cd /opt/smart-stock && /usr/local/bin/python3 screening_cube_viz.py --fetch --snapshot /tmp/screening_snapshot.json >> /var/log/screening-cube.log 2>&1' \
    > /etc/cron.d/screening-cube \
    && chmod 0644 /etc/cron.d/screening-cube

COPY .smart-stocker-google-api.json /root/.smart-stocker-google-api.json
COPY .yahoo-finance.api-key.txt /root/.yahoo-finance.api-key.txt

COPY entrypoint-tony-stock.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
