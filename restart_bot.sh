#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if pgrep -f amazon_price_bot.py >/dev/null 2>&1; then
  pkill -TERM -f amazon_price_bot.py 2>/dev/null || true

  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if ! pgrep -f amazon_price_bot.py >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  pkill -KILL -f amazon_price_bot.py 2>/dev/null || true
fi

git pull origin main

if [ ! -d "venv" ]; then
  python3 -m venv venv
fi

source venv/bin/activate
pip install -r requirements.txt

nohup python amazon_price_bot.py > bot.log 2>&1 &

echo "Bot yeniden baslatildi. Log icin:"
echo "tail -f bot.log"
