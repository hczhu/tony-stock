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
    tony-stock
