#!/bin/bash
set -e

docker stop tony-stock 2>/dev/null || true
docker rm tony-stock 2>/dev/null || true

mkdir -p /var/www/smart-stocker/charts
mkdir -p /home/hc/tony-stock/smart-stock/screening

# ~/.aws is also mounted at its host path so symlinks inside it with absolute
# targets (e.g. credentials -> /home/hc/.aws/aws_credentials.<profile>) still
# resolve in the container; otherwise the AWS CLI finds no credentials.
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
    -v /home/hc/.aws:/home/hc/.aws:ro \
    tony-stock
