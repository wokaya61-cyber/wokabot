#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

pkill -f amazon_price_bot.py 2>/dev/null || true
git pull origin main

if [ ! -d "venv" ]; then
  python3 -m venv venv
fi

source venv/bin/activate
pip install -r requirements.txt

nohup python amazon_price_bot.py > bot.log 2>&1 &

echo "Bot yeniden baslatildi. Log icin:"
echo "tail -f bot.log"
