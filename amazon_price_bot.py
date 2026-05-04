import asyncio
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask
from requests import HTTPError, RequestException, Timeout
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))
DATA_FILE = Path(os.getenv("DATA_FILE", "products.json"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
MIN_PRODUCT_DELAY = int(os.getenv("MIN_PRODUCT_DELAY", "3"))
CAPTCHA_BACKOFF_SECONDS = int(os.getenv("CAPTCHA_BACKOFF_SECONDS", "300"))
MAX_BACKOFF_SECONDS = int(os.getenv("MAX_BACKOFF_SECONDS", "3600"))

AMAZON_HOSTS = {"amazon.com.tr", "www.amazon.com.tr"}
RETRY_HTTP_STATUSES = {500, 502, 504}
BLOCK_HTTP_STATUSES = {429, 503}
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("amazon-price-bot")


class AmazonBlockedError(RuntimeError):
    pass


class AmazonReadError(RuntimeError):
    pass


http_session = requests.Session()


@dataclass
class ProductInfo:
    title: str
    price: Decimal | None
    seller_ok: bool
    seller_text: str
    coupon_exists: bool
    coupon_text: str
    in_stock: bool


def load_data() -> dict[str, list[dict[str, Any]]]:
    if not DATA_FILE.exists():
        return {}

    try:
        with DATA_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        logger.exception("Could not read %s", DATA_FILE)
        return {}

    if not isinstance(data, dict):
        return {}

    return data


def save_data(data: dict[str, list[dict[str, Any]]]) -> None:
    temp_file = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")
    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)
    temp_file.replace(DATA_FILE)


products = load_data()
data_lock = asyncio.Lock()


def clean_amazon_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url if "://" in url else f"https://{url}")

    if parsed.netloc.lower() not in AMAZON_HOSTS:
        raise ValueError("Sadece amazon.com.tr ürün linkleri desteklenir.")

    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", parsed.path, re.I)
    if not match:
        raise ValueError("Link içinde geçerli bir Amazon ASIN kodu bulunamadı.")

    return f"https://www.amazon.com.tr/dp/{match.group(1).upper()}"


def money_to_decimal(value: str) -> Decimal | None:
    value = value.replace("\xa0", " ").strip()
    value = re.sub(r"[^\d,\.]", "", value)

    if not value:
        return None

    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    else:
        parts = value.split(".")
        if len(parts) > 2:
            value = "".join(parts)

    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def parse_percent(value: str) -> Decimal:
    return Decimal(value.strip().replace("%", "").replace(",", "."))


def now_ts() -> int:
    return int(time.time())


def is_due(product: dict[str, Any]) -> bool:
    return now_ts() >= int(product.get("next_check_at", 0) or 0)


def set_backoff(product: dict[str, Any], reason: str) -> None:
    failures = int(product.get("failure_count", 0) or 0) + 1
    base_delay = CAPTCHA_BACKOFF_SECONDS if reason == "captcha" else 60
    delay = min(base_delay * (2 ** min(failures - 1, 4)), MAX_BACKOFF_SECONDS)
    delay += random.randint(0, min(60, max(1, delay // 5)))

    product["failure_count"] = failures
    product["next_check_at"] = now_ts() + delay
    product["last_error"] = reason


def clear_backoff(product: dict[str, Any]) -> None:
    product["failure_count"] = 0
    product["next_check_at"] = now_ts() + MIN_PRODUCT_DELAY + random.randint(0, 7)
    product["last_error"] = ""


def first_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            text = " ".join(element.get_text(" ", strip=True).split())
            if text:
                return text
    return ""


def parse_price(soup: BeautifulSoup) -> Decimal | None:
    selectors = [
        "#corePrice_feature_div .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "#priceblock_dealprice",
        "#priceblock_ourprice",
        "#price_inside_buybox",
        ".a-price .a-offscreen",
    ]

    for selector in selectors:
        for element in soup.select(selector):
            price = money_to_decimal(element.get_text(" ", strip=True))
            if price and price > 0:
                return price

    body_text = soup.get_text(" ", strip=True)
    match = re.search(r"(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*TL", body_text)
    return money_to_decimal(match.group(1)) if match else None


def parse_seller(soup: BeautifulSoup) -> tuple[bool, str]:
    seller_link = first_text(soup, ["#sellerProfileTriggerId"])
    if seller_link and "amazon.com.tr" in seller_link.casefold():
        return True, seller_link

    selectors = [
        "#merchant-info",
        "#tabular-buybox-container",
        "#buybox",
        "#shipsFromSoldByMessage_feature_div",
    ]
    seller_text = first_text(soup, selectors)
    compact = seller_text.casefold()

    amazon_markers = [
        "amazon.com.tr tarafından satılır",
        "amazon.com.tr tarafindan satilir",
        "satıcı amazon.com.tr",
        "satici amazon.com.tr",
        "amazon.com.tr satıcısından",
        "amazon.com.tr saticisindan",
    ]
    seller_is_amazon = any(marker in compact for marker in amazon_markers)
    return seller_is_amazon, seller_text or seller_link


def parse_stock(soup: BeautifulSoup) -> bool:
    availability = first_text(soup, ["#availability", "#outOfStock"])
    text = availability.casefold()

    if any(marker in text for marker in ["stokta yok", "mevcut değil", "şu anda mevcut değil"]):
        return False

    return True


def parse_coupon(soup: BeautifulSoup) -> tuple[bool, str]:
    candidates: list[str] = []
    selectors = [
        "#couponText",
        ".couponLabelText",
        ".couponBadge",
        ".promoPriceBlockMessage",
        "#vpcButton",
        "[id*='coupon']",
        "[class*='coupon']",
    ]

    for selector in selectors:
        for element in soup.select(selector):
            text = " ".join(element.get_text(" ", strip=True).split())
            if text and any(word in text.casefold() for word in ["kupon", "indirim", "tasarruf"]):
                candidates.append(text)

    body_text = " ".join(soup.get_text(" ", strip=True).split())
    coupon_patterns = [
        r"(?:% ?\d{1,2}|\d{1,5}(?:[.,]\d{1,2})? ?TL).{0,80}?(?:kupon|indirim|tasarruf)",
        r"(?:kupon|indirim kuponu|tasarruf).{0,80}?(?:% ?\d{1,2}|\d{1,5}(?:[.,]\d{1,2})? ?TL)",
    ]

    for pattern in coupon_patterns:
        match = re.search(pattern, body_text, re.I)
        if match:
            candidates.append(match.group(0))

    if not candidates:
        return False, ""

    coupon_text = min(candidates, key=len)
    return True, coupon_text[:240]


def fetch_product_info(url: str) -> ProductInfo | None:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.7,en;q=0.6",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.amazon.com.tr/",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": random.choice(USER_AGENTS),
    }

    response = None
    last_error: Exception | None = None

    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            response = http_session.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            if response.status_code in RETRY_HTTP_STATUSES and attempt < HTTP_RETRIES:
                logger.warning(
                    "Amazon returned HTTP %s for %s. Attempt %s/%s",
                    response.status_code,
                    url,
                    attempt,
                    HTTP_RETRIES,
                )
                time.sleep(2 + attempt + random.uniform(0, 2))
                continue
            break
        except Timeout as exc:
            last_error = exc
            logger.warning(
                "Amazon read timed out for %s. Attempt %s/%s",
                url,
                attempt,
                HTTP_RETRIES,
            )
        except RequestException as exc:
            last_error = exc
            logger.warning(
                "Amazon request failed for %s. Attempt %s/%s: %s",
                url,
                attempt,
                HTTP_RETRIES,
                exc,
            )

        if attempt < HTTP_RETRIES:
            time.sleep(2 + attempt + random.uniform(0, 2))

    if response is None:
        raise AmazonReadError(f"Amazon sayfası okunamadı: {last_error}")

    if response.status_code in BLOCK_HTTP_STATUSES:
        raise AmazonBlockedError(f"Amazon geçici blok döndürdü: HTTP {response.status_code}")

    if response.status_code in RETRY_HTTP_STATUSES:
        raise AmazonReadError(f"Amazon geçici sunucu hatası döndürdü: HTTP {response.status_code}")

    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    title = first_text(soup, ["#productTitle", "span#title", "h1"])
    page_text = soup.get_text(" ", strip=True).casefold()

    if any(marker in page_text for marker in ["captcha", "robot check", "automated access"]):
        raise AmazonBlockedError("Amazon bot doğrulama sayfası döndürdü.")

    if not title:
        return None

    seller_ok, seller_text = parse_seller(soup)
    coupon_exists, coupon_text = parse_coupon(soup)

    return ProductInfo(
        title=title,
        price=parse_price(soup),
        seller_ok=seller_ok,
        seller_text=seller_text,
        coupon_exists=coupon_exists,
        coupon_text=coupon_text,
        in_stock=parse_stock(soup),
    )


def format_money(value: Decimal | str | float | int | None) -> str:
    if value is None:
        return "?"
    decimal = Decimal(str(value))
    return f"{decimal:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def calculate_drop(old_price: Decimal, new_price: Decimal) -> Decimal:
    if old_price <= 0:
        return Decimal("0")
    return ((old_price - new_price) / old_price) * Decimal("100")


def product_label(product: dict[str, Any]) -> str:
    title = product.get("title") or product.get("url", "Ürün")
    return title if len(title) <= 90 else f"{title[:87]}..."


def product_status_line(product: dict[str, Any]) -> str:
    next_check_at = int(product.get("next_check_at", 0) or 0)
    if next_check_at > now_ts():
        wait_seconds = next_check_at - now_ts()
        return f"⏳ Sonraki kontrol: yaklaşık {max(1, wait_seconds // 60)} dk sonra"

    if product.get("last_error"):
        return f"⚠️ Son durum: {product['last_error']}"

    return "✅ Kontrol aktif"


async def scrape(url: str) -> ProductInfo | None:
    return await asyncio.to_thread(fetch_product_info, url)


async def notify(app: Application, chat_id: str, text: str, url: str) -> None:
    keyboard = [[InlineKeyboardButton("Amazon'da aç", url=url)]]
    await app.bot.send_message(
        chat_id=int(chat_id),
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Amazon.com.tr fiyat takip botu hazır.\n\n"
        "Komutlar:\n"
        "/add LINK YUZDE - Ürün ekler\n"
        "/remove LINK veya SIRA_NO - Ürün siler\n"
        "/setdrop LINK veya SIRA_NO YUZDE - Bildirim yüzdesini değiştirir\n"
        "/list - Takip listesini gösterir\n"
        "/check - Elle kontrol başlatır\n\n"
        "Örnek:\n"
        "/add https://www.amazon.com.tr/dp/B0BJQP23Y8 15"
    )


async def add_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)

    if len(context.args) < 2:
        await update.message.reply_text("Kullanım: /add LINK YUZDE")
        return

    try:
        url = clean_amazon_url(context.args[0])
        drop_percent = parse_percent(context.args[1])
    except (ValueError, InvalidOperation) as exc:
        await update.message.reply_text(str(exc) if str(exc) else "Yüzde değeri hatalı.")
        return

    if drop_percent <= 0:
        await update.message.reply_text("Yüzde değeri 0'dan büyük olmalı.")
        return

    await update.message.reply_text("Ürünü okuyorum, birkaç saniye sürebilir...")

    try:
        info = await scrape(url)
    except AmazonReadError as exc:
        logger.warning("Scrape read failed for %s: %s", url, exc)
        await update.message.reply_text(
            "Ürün şu an Amazon geçici hata verdiği veya yavaş cevapladığı için okunamadı. "
            "Biraz sonra tekrar /add komutunu deneyebilirsin."
        )
        return
    except AmazonBlockedError as exc:
        logger.warning("Scrape blocked for %s: %s", url, exc)
        await update.message.reply_text(
            "Amazon şu an bot doğrulaması veya geçici blok döndürdü. "
            "Biraz bekleyip tekrar denemek daha sağlıklı olur."
        )
        return
    except Exception as exc:
        logger.exception("Scrape failed for %s", url)
        await update.message.reply_text(f"Ürün okunamadı: {exc}")
        return

    if not info or not info.price:
        await update.message.reply_text("Ürün veya fiyat okunamadı.")
        return

    if not info.seller_ok:
        seller = f"\nSatıcı bilgisi: {info.seller_text}" if info.seller_text else ""
        await update.message.reply_text(
            "Bu ürünün satıcısı amazon.com.tr görünmüyor, takip listesine eklemedim."
            f"{seller}"
        )
        return

    async with data_lock:
        chat_products = products.setdefault(chat_id, [])
        if any(item.get("url") == url for item in chat_products):
            await update.message.reply_text("Bu ürün zaten takip listende.")
            return

        chat_products.append(
            {
                "url": url,
                "title": info.title,
                "base_price": str(info.price),
                "last_price": str(info.price),
                "drop_percent": str(drop_percent),
                "coupon_notified": info.coupon_exists,
                "last_coupon_text": info.coupon_text,
                "failure_count": 0,
                "last_error": "",
                "next_check_at": now_ts() + MIN_PRODUCT_DELAY,
                "created_at": int(time.time()),
            }
        )
        save_data(products)

    coupon_line = f"🎟️ Kupon: {info.coupon_text}" if info.coupon_exists else "❌ Üründe kupon yok"
    await update.message.reply_text(
        "✅ Ürün eklendi\n\n"
        f"📦 {info.title}\n"
        f"💰 Fiyat: {format_money(info.price)} TL\n"
        f"{coupon_line}\n"
        f"🎯 Bildirim eşiği: %{drop_percent}\n"
        f"🔗 {url}"
    )


def find_product(chat_products: list[dict[str, Any]], key: str) -> tuple[int, dict[str, Any]] | None:
    if key.isdigit():
        index = int(key) - 1
        if 0 <= index < len(chat_products):
            return index, chat_products[index]
        return None

    try:
        url = clean_amazon_url(key)
    except ValueError:
        return None

    for index, product in enumerate(chat_products):
        if product.get("url") == url:
            return index, product
    return None


async def remove_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)

    if not context.args:
        await update.message.reply_text("Kullanım: /remove LINK veya SIRA_NO")
        return

    async with data_lock:
        chat_products = products.get(chat_id, [])
        found = find_product(chat_products, context.args[0])
        if not found:
            await update.message.reply_text("Ürün bulunamadı.")
            return

        index, removed = found
        del chat_products[index]
        save_data(products)

    await update.message.reply_text(f"Ürün silindi: {product_label(removed)}")


async def set_drop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)

    if len(context.args) < 2:
        await update.message.reply_text("Kullanım: /setdrop LINK veya SIRA_NO YUZDE")
        return

    try:
        drop_percent = parse_percent(context.args[1])
    except InvalidOperation:
        await update.message.reply_text("Yüzde değeri hatalı.")
        return

    if drop_percent <= 0:
        await update.message.reply_text("Yüzde değeri 0'dan büyük olmalı.")
        return

    async with data_lock:
        chat_products = products.get(chat_id, [])
        found = find_product(chat_products, context.args[0])
        if not found:
            await update.message.reply_text("Ürün bulunamadı.")
            return

        _, product = found
        product["drop_percent"] = str(drop_percent)
        save_data(products)

    await update.message.reply_text(f"🎯 Bildirim eşiği güncellendi: %{drop_percent}")


async def list_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    chat_products = products.get(chat_id, [])

    if not chat_products:
        await update.message.reply_text("Takip listen boş.")
        return

    lines = ["📋 Takip edilen ürünler:\n"]
    for index, product in enumerate(chat_products, start=1):
        lines.extend(
            [
                f"{index}. 📦 {product_label(product)}",
                f"💰 Fiyat: {format_money(product.get('last_price'))} TL",
                f"🎯 Eşik: %{product.get('drop_percent')}",
                product_status_line(product),
                f"🔗 {product.get('url', '')}",
                "",
            ]
        )

    await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Elle kontrol başlatıldı.")
    await check_all_products(context.application, manual_chat_id=str(update.effective_chat.id))
    await update.message.reply_text("Kontrol tamamlandı.")


async def check_product(app: Application, chat_id: str, product: dict[str, Any]) -> None:
    url = product.get("url", "")

    if not is_due(product):
        return

    try:
        info = await scrape(url)
    except AmazonBlockedError as exc:
        set_backoff(product, "captcha")
        logger.warning(
            "Amazon blocked check for %s. Backoff until %s: %s",
            url,
            product.get("next_check_at"),
            exc,
        )
        return
    except HTTPError as exc:
        set_backoff(product, "http_error")
        product["last_error"] = str(exc)
        logger.warning("HTTP error for %s: %s", url, exc)
        return
    except AmazonReadError as exc:
        set_backoff(product, "timeout")
        product["last_error"] = str(exc)
        logger.warning("Read timeout for %s: %s", url, exc)
        return
    except Exception as exc:
        set_backoff(product, "read_error")
        product["last_error"] = str(exc)
        logger.warning("Check failed for %s: %s", url, exc)
        return

    if not info:
        set_backoff(product, "empty_product")
        return

    product["title"] = info.title
    clear_backoff(product)

    if not info.seller_ok:
        product["seller_ok"] = False
        return

    product["seller_ok"] = True

    if not info.price:
        return

    old_price = Decimal(str(product.get("base_price") or product.get("last_price") or info.price))
    current_price = info.price
    drop_percent = Decimal(str(product.get("drop_percent", "0")))
    drop = calculate_drop(old_price, current_price)

    if drop >= drop_percent:
        await notify(
            app,
            chat_id,
            "🔥 Fiyat düştü\n\n"
            f"📦 {info.title}\n"
            f"💸 Eski fiyat: {format_money(old_price)} TL\n"
            f"💰 Yeni fiyat: {format_money(current_price)} TL\n"
            f"📉 Düşüş: %{drop:.2f}",
            url,
        )
        product["base_price"] = str(current_price)

    last_coupon_text = product.get("last_coupon_text", "")
    if info.coupon_exists and info.coupon_text != last_coupon_text:
        await notify(
            app,
            chat_id,
            "🎟️ Kupon bulundu\n\n"
            f"📦 {info.title}\n"
            f"🧾 {info.coupon_text}",
            url,
        )
        product["coupon_notified"] = True
        product["last_coupon_text"] = info.coupon_text

    if not info.coupon_exists:
        product["coupon_notified"] = False
        product["last_coupon_text"] = ""

    product["last_price"] = str(current_price)
    product["in_stock"] = info.in_stock


async def check_all_products(app: Application, manual_chat_id: str | None = None) -> None:
    async with data_lock:
        chat_ids = [manual_chat_id] if manual_chat_id else list(products.keys())
        targets = [
            (chat_id, product)
            for chat_id in chat_ids
            for product in products.get(chat_id, [])
        ]

    changed = False
    for chat_id, product in targets:
        await check_product(app, chat_id, product)
        changed = True

    if changed:
        async with data_lock:
            save_data(products)


async def background_checker(app: Application) -> None:
    while True:
        await check_all_products(app)
        await asyncio.sleep(CHECK_INTERVAL)


async def post_init(app: Application) -> None:
    app.create_task(background_checker(app))
    logger.info("Background checker started. Interval: %s seconds", CHECK_INTERVAL)


app_web = Flask(__name__)


@app_web.route("/")
def home() -> str:
    return "Amazon fiyat takip botu çalışıyor"


def run_web() -> None:
    port = int(os.getenv("PORT", "8000"))
    app_web.run(host="0.0.0.0", port=port)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN ortam değişkeni tanımlı değil.")

    from threading import Thread

    Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("add", add_product))
    app.add_handler(CommandHandler("remove", remove_product))
    app.add_handler(CommandHandler("setdrop", set_drop))
    app.add_handler(CommandHandler("list", list_products))
    app.add_handler(CommandHandler("check", check_command))

    logger.info("Telegram bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
