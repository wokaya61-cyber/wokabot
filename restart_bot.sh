#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

LOCK_FILE="${INSTANCE_LOCK_FILE:-/tmp/amazon_price_bot.lock}"

if [ -f "$LOCK_FILE" ]; then
  LOCK_PID="$(tr -cd '0-9' < "$LOCK_FILE")"
  if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
    kill -TERM "$LOCK_PID" 2>/dev/null || true
  fi
fi

pkill -TERM -f '[a]mazon_price_bot.py' 2>/dev/null || true

for _ in $(seq 1 15); do
  if ! pgrep -f '[a]mazon_price_bot.py' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

pkill -KILL -f '[a]mazon_price_bot.py' 2>/dev/null || true
sleep 2

if pgrep -f '[a]mazon_price_bot.py' >/dev/null 2>&1; then
  echo "Eski Amazon bot sureci otomatik yeniden basliyor:"
  pgrep -af '[a]mazon_price_bot.py'
  echo "Bu sureci baslatan systemd/supervisor servisi durdurulmadan yeni bot baslatilamaz."
  exit 1
fi

rm -f "$LOCK_FILE"

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
