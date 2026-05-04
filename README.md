# Amazon.com.tr Telegram Fiyat Botu

Bu bot, eklediğiniz amazon.com.tr ürün linklerini belirlediğiniz yüzde eşiğine göre takip eder. Satıcısı amazon.com.tr olan ürünlerde fiyat düşüşü veya yeni kupon algıladığında Telegram bildirimi gönderir.

## Kurulum

1. Paketleri kurun:

```bash
pip install -r requirements.txt
```

2. `.env.example` dosyasını `.env` olarak kopyalayın ve `BOT_TOKEN` değerini BotFather'dan aldığınız token ile doldurun.

3. Botu çalıştırın:

```bash
python amazon_price_bot.py
```

## Telegram Komutları

- `/add LINK YUZDE` ürün ekler.
- `/remove LINK` veya `/remove SIRA_NO` ürün siler.
- `/setdrop LINK YUZDE` veya `/setdrop SIRA_NO YUZDE` bildirim yüzdesini değiştirir.
- `/list` takip listesini gösterir.
- `/check` elle kontrol başlatır.

Örnek:

```text
/add https://www.amazon.com.tr/dp/B0BJQP23Y8 15
```

## Notlar

- Varsayılan tarama aralığı 10 saniyedir. Bunu `.env` içindeki `CHECK_INTERVAL` ile değiştirebilirsiniz.
- Amazon zaman zaman bot doğrulaması döndürebilir. Böyle bir durumda ürün atlanır, bot çalışmaya devam eder.
- Captcha veya geçici blok algılanırsa bot aynı ürünü bir süre bekletir. Bekleme süresini `.env` içindeki `CAPTCHA_BACKOFF_SECONDS` ve `MAX_BACKOFF_SECONDS` ile ayarlayabilirsiniz.
- Bot yalnızca satıcısı amazon.com.tr olarak algılanan ürünleri takip listesine ekler ve bildirir.
