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

rm -f /tmp/amazon_price_bot.lock

git pull origin main

if [ ! -d "venv" ]; then
  python3 -m venv venv
fi

source venv/bin/activate
pip install -r requirements.txt

nohup python amazon_price_bot.py > bot.log 2>&1 &
BOT_PID=$!
sleep 3

if ! kill -0 "$BOT_PID" 2>/dev/null; then
  echo "Bot baslatilamadi. Son loglar:"
  tail -n 40 bot.log
  exit 1
fi

echo "Bot yeniden baslatildi. Log icin:"
echo "tail -f bot.log"
echo "Calisan Amazon bot surecleri:"
pgrep -af amazon_price_bot.py
