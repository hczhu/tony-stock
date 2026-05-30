FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    nginx \
    cron \
    git \
    curl \
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

COPY .smart-stocker-google-api.json /root/.smart-stocker-google-api.json
COPY .yahoo-finance.api-key.txt /root/.yahoo-finance.api-key.txt

COPY entrypoint-tony-stock.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
