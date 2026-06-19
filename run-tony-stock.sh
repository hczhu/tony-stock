#!/bin/bash
set -e

docker stop tony-stock 2>/dev/null || true
docker rm tony-stock 2>/dev/null || true

mkdir -p /var/www/smart-stocker/charts
mkdir -p /home/hc/tony-stock/smart-stock/screening

docker run -d \
    --name tony-stock \
    --network host \
    --restart unless-stopped \
    -v /var/www/smart-stocker:/var/www/smart-stocker \
    -v /home/hc/tony-stock/smart-stock:/opt/smart-stock \
    -v /home/hc/tony-stock:/opt/tony-stock \
    -v /home/hc/.smart-stocker-google-api.json:/root/.smart-stocker-google-api.json:ro \
    -v /home/hc/.yahoo-finance.api-key.txt:/root/.yahoo-finance.api-key.txt:ro \
    -v /home/hc/.aws:/root/.aws:ro \
    tony-stock
