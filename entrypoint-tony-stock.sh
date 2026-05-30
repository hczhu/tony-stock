#!/bin/bash
set -e

mkdir -p /dev/shm/my-trades

cron

nginx -g 'daemon off;'
