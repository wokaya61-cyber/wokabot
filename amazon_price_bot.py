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
ACTIVE_CHECK_SECONDS = int(os.getenv("ACTIVE_CHECK_SECONDS", "60"))
PENDING_RETRY_SECONDS = int(os.getenv("PENDING_RETRY_SECONDS", "10"))
CAPTCHA_BACKOFF_SECONDS = int(os.getenv("CAPTCHA_BACKOFF_SECONDS", "30"))
MAX_BACKOFF_SECONDS = int(os.getenv("MAX_BACKOFF_SECONDS", "60"))
MAX_CONCURRENT_CHECKS = int(os.getenv("MAX_CONCURRENT_CHECKS", "3"))
MAX_CHECKS_PER_CYCLE = int(os.getenv("MAX_CHECKS_PER_CYCLE", "50"))
FOLLOWUP_DROP_PERCENT = Decimal(os.getenv("FOLLOWUP_DROP_PERCENT", "1"))

AMAZON_HOSTS = {"amazon.com.tr", "www.amazon.com.tr"}
RETRY_HTTP_STATUSES = {500, 502, 504}
BLOCK_HTTP_STATUSES = {429, 503}
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]
MOBILE_USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
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
    max_quantity: int | None


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


def mobile_amazon_url(url: str) -> str:
    match = re.search(r"/dp/([A-Z0-9]{10})", url, re.I)
    if match:
        return f"https://www.amazon.com.tr/gp/aw/d/{match.group(1).upper()}"
    return url.replace("https://www.amazon.com.tr/", "https://www.amazon.com.tr/gp/aw/")


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
    clamp_next_check(product)
    return now_ts() >= int(product.get("next_check_at", 0) or 0)


def clamp_next_check(product: dict[str, Any]) -> None:
    next_check_at = int(product.get("next_check_at", 0) or 0)
    if not next_check_at:
        return

    max_wait = PENDING_RETRY_SECONDS if product.get("pending_initial_price") else MAX_BACKOFF_SECONDS
    latest_allowed = now_ts() + max_wait
    if next_check_at > latest_allowed:
        product["next_check_at"] = latest_allowed


def set_backoff(product: dict[str, Any], reason: str) -> None:
    failures = int(product.get("failure_count", 0) or 0) + 1
    if product.get("pending_initial_price"):
        base_delay = PENDING_RETRY_SECONDS
        delay = base_delay
    else:
        base_delay = CAPTCHA_BACKOFF_SECONDS if reason == "captcha" else 15
        delay = min(base_delay * min(failures, 2), MAX_BACKOFF_SECONDS)

    delay += random.randint(0, min(10, max(1, delay // 5)))

    product["failure_count"] = failures
    product["next_check_at"] = now_ts() + delay
    product["last_error"] = reason


def clear_backoff(product: dict[str, Any]) -> None:
    product["failure_count"] = 0
    product["next_check_at"] = now_ts() + max(MIN_PRODUCT_DELAY, ACTIVE_CHECK_SECONDS) + random.randint(0, 10)
    product["last_error"] = ""


def first_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            text = " ".join(element.get_text(" ", strip=True).split())
            if text:
                return text
    return ""


def first_attr(soup: BeautifulSoup, selectors: list[str], attr: str) -> str:
    for selector in selectors:
        element = soup.select_one(selector)
        if element and element.get(attr):
            text = " ".join(str(element[attr]).split())
            if text:
                return text
    return ""


def parse_title(soup: BeautifulSoup) -> str:
    title = first_text(soup, ["#productTitle", "span#title", "h1"])
    if title:
        return title

    title = first_attr(
        soup,
        [
            "meta[property='og:title']",
            "meta[name='twitter:title']",
            "meta[name='title']",
        ],
        "content",
    )
    if title:
        return re.sub(r"\s*:\s*Amazon\.com\.tr\s*$", "", title).strip()

    return first_text(soup, ["title"])


def find_price_in_json(data: Any, currency_seen: bool = False) -> Decimal | None:
    if isinstance(data, dict):
        local_currency = currency_seen or any(
            str(value).upper() in {"TRY", "TL"}
            for key, value in data.items()
            if "currency" in str(key).casefold()
        )

        if local_currency:
            for key in ["price", "priceAmount", "amount", "value", "lowPrice"]:
                if key in data:
                    price = money_to_decimal(str(data[key]))
                    if price and price > 0:
                        return price

        for value in data.values():
            price = find_price_in_json(value, local_currency)
            if price:
                return price

    if isinstance(data, list):
        for item in data:
            price = find_price_in_json(item, currency_seen)
            if price:
                return price

    if isinstance(data, str) and "TL" in data:
        price = money_to_decimal(data)
        if price and price > 0:
            return price

    return None


def parse_price_from_scripts(soup: BeautifulSoup, html: str) -> Decimal | None:
    for script in soup.select("script[type='application/ld+json']"):
        text = script.string or script.get_text(strip=True)
        if not text:
            continue

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue

        price = find_price_in_json(data)
        if price:
            return price

    patterns = [
        r'"displayPrice"\s*:\s*"([^"]*?TL)"',
        r'"priceString"\s*:\s*"([^"]*?TL)"',
        r'"priceToPay"\s*:\s*\{[^{}]*?"displayString"\s*:\s*"([^"]*?TL)"',
        r'"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
    ]

    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            price = money_to_decimal(match.group(1))
            if price and price > 0:
                return price

    return None


def price_from_container(container: Any) -> Decimal | None:
    old_price_markers = [
        "a-text-price",
        "basisPrice",
        "listPrice",
        "wasPrice",
        "savingsPercentage",
        "previous",
        "strike",
    ]

    def is_old_price_element(element: Any) -> bool:
        for parent in [element, *element.parents]:
            parent_id = str(parent.get("id", ""))
            parent_class = " ".join(parent.get("class", []))
            marker_text = f"{parent_id} {parent_class}"
            if any(marker.casefold() in marker_text.casefold() for marker in old_price_markers):
                return True
        return False

    for offscreen in container.select(".a-price .a-offscreen, .a-offscreen"):
        if is_old_price_element(offscreen):
            continue

        price = money_to_decimal(offscreen.get_text(" ", strip=True))
        if price and price > 0:
            return price

    whole = container.select_one(".a-price-whole")
    fraction = container.select_one(".a-price-fraction")
    if whole and not is_old_price_element(whole):
        whole_text = whole.get_text("", strip=True)
        fraction_text = fraction.get_text("", strip=True) if fraction else "00"
        price = money_to_decimal(f"{whole_text},{fraction_text}")
        if price and price > 0:
            return price

    return None


def parse_price(soup: BeautifulSoup, html: str = "") -> Decimal | None:
    price_containers = [
        "#corePriceDisplay_desktop_feature_div .priceToPay",
        "#corePriceDisplay_mobile_feature_div .priceToPay",
        "#corePrice_feature_div .priceToPay",
        "#apex_desktop .priceToPay",
        "#tp_price_block_total_price_ww",
        "#newBuyBoxPrice",
        "#price_inside_buybox",
        "#corePriceDisplay_desktop_feature_div",
        "#corePriceDisplay_mobile_feature_div",
        "#corePrice_feature_div",
    ]

    for selector in price_containers:
        for container in soup.select(selector):
            price = price_from_container(container)
            if price and price > 0:
                return price

    selectors = [
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
        "#corePriceDisplay_mobile_feature_div .priceToPay .a-offscreen",
        "#corePrice_feature_div .priceToPay .a-offscreen",
        "#apex_desktop .priceToPay .a-offscreen",
        ".priceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div span[data-a-color='price'] .a-offscreen",
        "#corePriceDisplay_mobile_feature_div span[data-a-color='price'] .a-offscreen",
        "#priceblock_dealprice",
        "#priceblock_ourprice",
        "#price_inside_buybox",
    ]

    for selector in selectors:
        for element in soup.select(selector):
            price = money_to_decimal(element.get_text(" ", strip=True))
            if price and price > 0:
                return price

    for selector in [
        "meta[property='product:price:amount']",
        "meta[itemprop='price']",
        "[itemprop='price']",
    ]:
        for element in soup.select(selector):
            raw_price = element.get("content") or element.get_text(" ", strip=True)
            price = money_to_decimal(str(raw_price))
            if price and price > 0:
                return price

    script_price = parse_price_from_scripts(soup, html)
    if script_price:
        return script_price

    return None


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
        "satıcı amazon.com.tr iadeler",
        "satici amazon.com.tr iadeler",
        "gönderici amazon.com.tr",
        "gonderici amazon.com.tr",
    ]
    seller_is_amazon = any(marker in compact for marker in amazon_markers)
    seller_is_amazon = seller_is_amazon or (
        "amazon.com.tr" in compact
        and any(word in compact for word in ["satıcı", "satici", "gönderici", "gonderici"])
    )
    return seller_is_amazon, seller_text or seller_link


def parse_stock(soup: BeautifulSoup) -> bool:
    availability = first_text(soup, ["#availability", "#outOfStock"])
    text = availability.casefold()

    if any(marker in text for marker in ["stokta yok", "mevcut değil", "şu anda mevcut değil"]):
        return False

    return True


def parse_max_quantity(soup: BeautifulSoup, html: str = "") -> int | None:
    quantities: list[int] = []

    for selector in [
        "#quantity",
        "#quantity-native",
        "#mobileQuantityDropDown",
        "select[name='quantity']",
        "select[name='quantityBox']",
    ]:
        for option in soup.select(f"{selector} option"):
            value = option.get("value") or option.get_text(" ", strip=True)
            match = re.search(r"\d+", str(value))
            if match:
                quantities.append(int(match.group(0)))

    for select in soup.select("select"):
        attrs = " ".join(str(value) for value in select.attrs.values()).casefold()
        label = " ".join(select.get_text(" ", strip=True).split()).casefold()
        if not any(marker in attrs or marker in label for marker in ["quantity", "qty", "adet", "miktar"]):
            continue

        for option in select.select("option"):
            value = option.get("value") or option.get_text(" ", strip=True)
            match = re.search(r"\d{1,3}", str(value))
            if match:
                quantities.append(int(match.group(0)))

    for select in soup.select("select"):
        option_numbers: list[int] = []
        option_texts: list[str] = []

        for option in select.select("option"):
            value = str(option.get("value") or option.get_text(" ", strip=True)).strip()
            text = option.get_text(" ", strip=True).strip()
            candidate = value or text
            if not candidate:
                continue

            option_texts.append(candidate)
            if re.fullmatch(r"\d{1,3}", candidate):
                option_numbers.append(int(candidate))

        if len(option_numbers) >= 2 and len(option_numbers) == len(option_texts):
            quantities.append(max(option_numbers))

    for element in soup.select(".a-dropdown-item, li[role='option'], [data-value]"):
        text = element.get_text(" ", strip=True).strip()
        data_value = str(element.get("data-value", "")).strip()
        candidates = [text, data_value]

        for candidate in candidates:
            match = re.fullmatch(r"\d{1,3}", candidate)
            if match:
                quantities.append(int(match.group(0)))

    for element in soup.select("[data-quantity], [data-a-selector='quantity']"):
        for value in element.attrs.values():
            match = re.search(r"\d+", str(value))
            if match:
                quantities.append(int(match.group(0)))

    if quantities:
        return max(quantities)

    json_patterns = [
        r'"maxQuantity"\s*:\s*(\d{1,3})',
        r'"maxOrderQuantity"\s*:\s*(\d{1,3})',
        r'"quantityLimit"\s*:\s*(\d{1,3})',
        r'"quantityOptions"\s*:\s*\[([^\]]+)\]',
        r'"quantityDropDownOptions"\s*:\s*\[([^\]]+)\]',
        r'<select[^>]*(?:quantity|qty|adet|miktar)[^>]*>(.*?)</select>',
    ]

    for pattern in json_patterns:
        for match in re.finditer(pattern, html, re.I | re.S):
            if match.lastindex and match.lastindex >= 1:
                numbers = [int(value) for value in re.findall(r"\d{1,3}", match.group(1))]
                quantities.extend(numbers)

    if quantities:
        return max(quantities)

    page_text = " ".join(soup.get_text(" ", strip=True).split())
    patterns = [
        r"(?:en fazla|maksimum|max)\s*(\d{1,3})\s*(?:adet|tane)",
        r"(?:adet|miktar).{0,40}?(?:en fazla|maksimum|max)\s*(\d{1,3})",
        r"(\d{1,3})\s*(?:adet|tane).{0,40}?(?:satın alabilirsiniz|alinabilir|alabilirsiniz)",
        r"adet\s*:\s*((?:\d{1,3}\s*){2,30})",
    ]

    for pattern in patterns:
        match = re.search(pattern, page_text, re.I)
        if match:
            numbers = [int(value) for value in re.findall(r"\d{1,3}", match.group(1))]
            if numbers:
                return max(numbers)

    buyable_selectors = [
        "#add-to-cart-button",
        "input[name='submit.add-to-cart']",
        "button[name='submit.add-to-cart']",
        "#buy-now-button",
        "input[name='submit.buy-now']",
    ]
    if any(soup.select_one(selector) for selector in buyable_selectors) and parse_stock(soup):
        return 1

    return None


def form_payload(form: Any) -> dict[str, str]:
    payload: dict[str, str] = {}
    for field in form.select("input, select, textarea"):
        name = field.get("name")
        if not name:
            continue

        if field.name == "select":
            selected = field.select_one("option[selected]")
            option = selected or field.select_one("option")
            payload[name] = option.get("value", "") if option else ""
            continue

        field_type = str(field.get("type", "")).casefold()
        if field_type in {"checkbox", "radio"} and not field.get("checked"):
            continue

        payload[name] = str(field.get("value", ""))

    return payload


def normalize_promo_text(text: str) -> str:
    replacements = str.maketrans(
        {
            "ı": "i",
            "İ": "i",
            "ğ": "g",
            "Ğ": "g",
            "ü": "u",
            "Ü": "u",
            "ş": "s",
            "Ş": "s",
            "ö": "o",
            "Ö": "o",
            "ç": "c",
            "Ç": "c",
        }
    )
    return " ".join(text.translate(replacements).casefold().split())


def promotion_match(text: str) -> str:
    normalized = normalize_promo_text(text)
    promo_patterns = [
        r"(?:kupon|kuponu).{0,80}?(?:uygula|tl|%)",
        r"(?:\d{1,5}(?:[.,]\d{1,2})?\s*tl|%\s*\d{1,2}).{0,80}?(?:kupon|kuponu).{0,80}?(?:uygula)?",
        r"odeme.{0,60}?(?:esnasinda|sirasinda).{0,80}?(?:\d{1,5}(?:[.,]\d{1,2})?\s*tl|%\s*\d{1,2}).{0,80}?(?:tasarruf|indirim)",
        r"(?:\d+\s*(?:veya)?\s*daha\s*fazla\s*al|daha\s*fazla\s*al).{0,80}?%\s*\d{1,2}.{0,80}?indirim\s*kazan",
        r"cok\s*al.{0,40}?(?:az\s*ode|indirim).{0,80}?(?:%\s*\d{1,2}|\d{1,5}(?:[.,]\d{1,2})?\s*tl)",
        r"(?:prime\s*uyelerine\s*ozel|uyelere\s*ozel).{0,80}?(?:\d{1,5}(?:[.,]\d{1,2})?\s*tl|%\s*\d{1,2}).{0,80}?(?:promosyon|indirim|kod)",
        r"(?:promosyon\s*kodu|promosyon\s*kod).{0,100}?(?:[a-z0-9]{3,}|indirim|tl|%)",
        r"(?:\d{1,5}(?:[.,]\d{1,2})?\s*tl|%\s*\d{1,2}).{0,80}?(?:kazandiran|kazandıran).{0,80}?promosyon\s*kodu",
        r"bir\s*sonraki.{0,80}?(?:siparisiniz|siparis).{0,80}?(?:\d{1,5}(?:[.,]\d{1,2})?\s*tl|%\s*\d{1,2}).{0,80}?(?:kupon|indirim).{0,40}?kazan",
        r"amazon\s*tarafindan\s*yapilan\s*indirim",
    ]

    for pattern in promo_patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            return text[:240]

    return ""


def parse_coupon(soup: BeautifulSoup) -> tuple[bool, str]:
    candidates: list[str] = []
    selectors = [
        "#couponText",
        ".couponLabelText",
        ".couponBadge",
        "#vpcButton",
        "#couponFeature",
        "#coupon_feature_div",
        "#promoPriceBlockMessage_feature_div [id*='coupon']",
        "[data-csa-c-slot-id*='coupon']",
        "#promoPriceBlockMessage_feature_div",
        "#reinvent_price_desktop_pickupOfferDisplay_feature_div",
        "#corePriceDisplay_desktop_feature_div",
        "#corePriceDisplay_mobile_feature_div",
        "#apex_desktop",
        "#desktop_buybox",
        "#buybox",
        "#qualifiedBuyBox",
        "#tmmSwatches",
        "#ppd #centerCol",
    ]

    for selector in selectors:
        for element in soup.select(selector):
            text_candidates: list[str] = []
            direct_text = " ".join(element.get_text(" ", strip=True).split())
            if direct_text and len(direct_text) <= 300:
                text_candidates.append(direct_text)

            for child in element.select("span, label, div, a, td"):
                child_text = " ".join(child.get_text(" ", strip=True).split())
                if child_text and len(child_text) <= 300:
                    text_candidates.append(child_text)

            for text in dict.fromkeys(text_candidates):
                normalized = normalize_promo_text(text)
                if any(skip in normalized for skip in ["sponsorlu", "whatsapp", "arama yapin"]):
                    continue

                matched_text = promotion_match(text)
                if matched_text:
                    candidates.append(matched_text)

    if not candidates:
        return False, ""

    coupon_text = min(candidates, key=len)
    return True, coupon_text[:240]


def fetch_html(url: str, headers: dict[str, str], session: requests.Session | None = None) -> str:
    active_session = session or http_session
    response = None
    last_error: Exception | None = None

    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            response = active_session.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            if response.status_code in RETRY_HTTP_STATUSES and attempt < HTTP_RETRIES:
                logger.warning(
                    "Amazon returned HTTP %s for %s. Attempt %s/%s",
                    response.status_code,
                    url,
                    attempt,
                    HTTP_RETRIES,
                )
                time.sleep(0.7 + random.uniform(0, 0.8))
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
            time.sleep(0.7 + random.uniform(0, 0.8))

    if response is None:
        raise AmazonReadError(f"Amazon sayfası okunamadı: {last_error}")

    if response.status_code in BLOCK_HTTP_STATUSES:
        raise AmazonBlockedError(f"Amazon geçici blok döndürdü: HTTP {response.status_code}")

    if response.status_code in RETRY_HTTP_STATUSES:
        raise AmazonReadError(f"Amazon geçici sunucu hatası döndürdü: HTTP {response.status_code}")

    response.raise_for_status()
    return response.text


def parse_product_html(html: str) -> ProductInfo | None:
    soup = BeautifulSoup(html, "html.parser")
    title = parse_title(soup)
    page_text = soup.get_text(" ", strip=True).casefold()

    if any(marker in page_text for marker in ["captcha", "robot check", "automated access"]):
        raise AmazonBlockedError("Amazon bot doğrulama sayfası döndürdü.")

    if not title:
        return None

    seller_ok, seller_text = parse_seller(soup)
    coupon_exists, coupon_text = parse_coupon(soup)

    return ProductInfo(
        title=title,
        price=parse_price(soup, html),
        seller_ok=seller_ok,
        seller_text=seller_text,
        coupon_exists=coupon_exists,
        coupon_text=coupon_text,
        in_stock=parse_stock(soup),
        max_quantity=parse_max_quantity(soup, html),
    )


def build_headers(user_agent: str) -> dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.7,en;q=0.6",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.amazon.com.tr/",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": user_agent,
    }


def fetch_product_info(url: str) -> ProductInfo | None:
    errors: list[str] = []
    blocked_errors: list[str] = []
    candidates = [
        (url, random.choice(USER_AGENTS)),
        (mobile_amazon_url(url), random.choice(MOBILE_USER_AGENTS)),
    ]

    for candidate_url, user_agent in candidates:
        try:
            headers = build_headers(user_agent)
            html = fetch_html(candidate_url, headers)
            info = parse_product_html(html)
            if info and info.price:
                return info

            if info:
                errors.append(f"{candidate_url}: fiyat yok")
        except AmazonBlockedError as exc:
            blocked_errors.append(f"{candidate_url}: {exc}")
            continue
        except Exception as exc:
            errors.append(f"{candidate_url}: {exc}")

    if errors:
        raise AmazonReadError(" / ".join(errors[-2:]))

    if blocked_errors:
        raise AmazonBlockedError(" / ".join(blocked_errors[-2:]))

    return None


def format_money(value: Decimal | str | float | int | None) -> str:
    if value is None or value == "":
        return "?"
    try:
        decimal = Decimal(str(value))
    except InvalidOperation:
        return "?"
    return f"{decimal:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def max_quantity_line(info: ProductInfo, cart_max_quantity: int | None = None) -> str:
    if cart_max_quantity:
        if info.max_quantity and cart_max_quantity > info.max_quantity:
            return f"🛒 Sepet maksimum adedi: {cart_max_quantity} (sayfada {info.max_quantity})"
        return f"🛒 Sepet maksimum adedi: {cart_max_quantity}"

    if info.max_quantity:
        return f"🛒 Sayfa maksimum adedi: {info.max_quantity}"

    return "🛒 Maksimum adet: okunamadı"


async def get_cart_max_quantity(url: str, info: ProductInfo) -> int | None:
    return None


async def get_cached_or_probe_cart_quantity(
    product: dict[str, Any],
    url: str,
    info: ProductInfo,
) -> int | None:
    return None


def calculate_drop(old_price: Decimal, new_price: Decimal) -> Decimal:
    if old_price <= 0:
        return Decimal("0")
    return ((old_price - new_price) / old_price) * Decimal("100")


def product_label(product: dict[str, Any]) -> str:
    title = product.get("title") or product.get("url", "Ürün")
    return title if len(title) <= 90 else f"{title[:87]}..."


def product_status_line(product: dict[str, Any]) -> str:
    clamp_next_check(product)

    if product.get("pending_initial_price"):
        if product.get("last_error") == "Amazon saticisi bekleniyor":
            return "⏳ Amazon.com.tr saticisi bekleniyor"
        next_check_at = int(product.get("next_check_at", 0) or 0)
        wait_seconds = max(0, next_check_at - now_ts())
        if wait_seconds:
            return f"⏳ İlk başarılı fiyat okuması bekleniyor, yaklaşık {wait_seconds} sn sonra tekrar denenecek"
        return "⏳ İlk başarılı fiyat okuması bekleniyor, sıradaki kontrolde denenecek"

    if product.get("disabled"):
        return f"⛔ Pasif: {product.get('last_error', 'takip durduruldu')}"

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


async def add_pending_product(chat_id: str, url: str, drop_percent: Decimal, reason: str, title: str = "") -> bool:
    product = {
        "url": url,
        "title": title or "Ürün okunmayı bekliyor",
        "base_price": "",
        "last_price": "",
        "drop_percent": str(drop_percent),
        "first_drop_notified": False,
        "coupon_notified": False,
        "last_coupon_text": "",
        "pending_initial_price": True,
        "failure_count": 0,
        "last_error": reason,
        "next_check_at": now_ts() + PENDING_RETRY_SECONDS,
        "created_at": int(time.time()),
    }

    async with data_lock:
        chat_products = products.setdefault(chat_id, [])
        if any(item.get("url") == url for item in chat_products):
            return False

        chat_products.append(product)
        save_data(products)

    return True


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

    async with data_lock:
        if any(item.get("url") == url for item in products.get(chat_id, [])):
            await update.message.reply_text("Bu ürün zaten takip listende.")
            return

    await update.message.reply_text("Ürünü okuyorum, birkaç saniye sürebilir...")

    try:
        info = await scrape(url)
    except AmazonReadError as exc:
        logger.warning("Scrape read failed for %s: %s", url, exc)
        await add_pending_product(chat_id, url, drop_percent, "timeout")
        await update.message.reply_text(
            "⏳ Ürün şu an Amazon geçici hata verdiği veya yavaş cevapladığı için okunamadı.\n\n"
            "Link takip listesine beklemede olarak eklendi. Bot arkada denemeye devam edecek; "
            "ilk başarılı okumada fiyatı başlangıç fiyatı olarak kaydedecek."
        )
        return
    except AmazonBlockedError as exc:
        logger.warning("Scrape blocked for %s: %s", url, exc)
        await add_pending_product(chat_id, url, drop_percent, "captcha")
        await update.message.reply_text(
            "⏳ Amazon şu an bot doğrulaması veya geçici blok döndürdü.\n\n"
            "Link takip listesine beklemede olarak eklendi. Bot biraz bekleyip arkada tekrar deneyecek."
        )
        return
    except Exception as exc:
        logger.exception("Scrape failed for %s", url)
        await update.message.reply_text(f"Ürün okunamadı: {exc}")
        return

    if not info:
        await add_pending_product(chat_id, url, drop_percent, "empty_product")
        await update.message.reply_text(
            "⏳ Ürün detayları şu an tam okunamadı.\n\n"
            "Link takip listesine beklemede olarak eklendi. Bot arkada tekrar deneyecek."
        )
        return

    if not info.seller_ok:
        seller = f"\nSatici bilgisi: {info.seller_text}" if info.seller_text else ""
        await add_pending_product(chat_id, url, drop_percent, "amazon_seller_wait", info.title)
        await update.message.reply_text(
            "⏳ Urun takip listesine eklendi.\n\n"
            f"📦 {info.title}\n"
            "Satici su an amazon.com.tr gorunmedigi icin fiyat/kupon takibi baslatilmadi. "
            "Bot kontrollerde once saticiya bakacak; Amazon satmaya baslayinca baslangic fiyatini kaydedip takibi aktif edecek."
            f"{seller}"
        )
        return

    if not info.price:
        await add_pending_product(chat_id, url, drop_percent, "price_missing", info.title)
        await update.message.reply_text(
            "⏳ Ürün bulundu ama fiyat şu an okunamadı.\n\n"
            f"📦 {info.title}\n"
            "Link takip listesine beklemede olarak eklendi. Bot arkada tekrar deneyecek."
        )
        return

    cart_max_quantity = await get_cart_max_quantity(url, info)

    async with data_lock:
        chat_products = products.setdefault(chat_id, [])
        chat_products.append(
            {
                "url": url,
                "title": info.title,
                "base_price": str(info.price),
                "last_price": str(info.price),
                "drop_percent": str(drop_percent),
                "first_drop_notified": False,
                "coupon_notified": info.coupon_exists,
                "last_coupon_text": info.coupon_text,
                "page_max_quantity": info.max_quantity,
                "cart_max_quantity": cart_max_quantity,
                "cart_max_checked_at": now_ts() if cart_max_quantity else 0,
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
        f"📦 {info.title}\n\n"
        f"💰 Fiyat: {format_money(info.price)} TL\n\n"
        f"{coupon_line}\n\n"
        f"{max_quantity_line(info, cart_max_quantity)}\n\n"
        f"🎯 Ilk bildirim esigi: %{drop_percent}\n"
        f"Sonraki fiyat dusus esigi: %{FOLLOWUP_DROP_PERCENT}\n\n"
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

    messages: list[str] = []
    current_message = "📋 Takip edilen ürünler:\n"

    for index, product in enumerate(chat_products, start=1):
        product_block = "\n".join(
            (
                f"{index}. 📦 {product_label(product)}",
                f"💰 Fiyat: {format_money(product.get('last_price'))} TL",
                f"🎯 Eşik: %{product.get('drop_percent')}",
                product_status_line(product),
                f"🔗 {product.get('url', '')}",
                "",
            )
        )

        if len(current_message) + len(product_block) > 3500:
            messages.append(current_message)
            current_message = "📋 Takip edilen ürünler devamı:\n"

        current_message += f"\n{product_block}"

    messages.append(current_message)

    for message in messages:
        await update.message.reply_text(message, disable_web_page_preview=True)


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    context.application.create_task(check_all_products(context.application, manual_chat_id=chat_id))
    await update.message.reply_text("✅ Elle kontrol arka planda başlatıldı.")


async def check_product(app: Application, chat_id: str, product: dict[str, Any]) -> None:
    url = product.get("url", "")

    if product.get("disabled"):
        return

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
        product["last_error"] = "Amazon saticisi bekleniyor"
        product["pending_initial_price"] = True
        return

    product["seller_ok"] = True

    if not info.price:
        set_backoff(product, "price_missing")
        return

    current_price = info.price
    initial_drop_percent = Decimal(str(product.get("drop_percent", "0")))
    drop_percent = FOLLOWUP_DROP_PERCENT if product.get("first_drop_notified") else initial_drop_percent
    product["page_max_quantity"] = info.max_quantity

    if product.get("pending_initial_price") or not product.get("base_price"):
        cart_max_quantity = await get_cached_or_probe_cart_quantity(product, url, info)
        product["base_price"] = str(current_price)
        product["first_drop_notified"] = False
        product["last_price"] = str(current_price)
        product["pending_initial_price"] = False
        product["coupon_notified"] = info.coupon_exists
        product["last_coupon_text"] = info.coupon_text
        product["in_stock"] = info.in_stock

        coupon_line = f"🎟️ Kupon: {info.coupon_text}" if info.coupon_exists else "❌ Üründe kupon yok"
        await notify(
            app,
            chat_id,
            "✅ Amazon.com.tr satmaya basladi, urun takibe alindi\n\n"
            f"📦 {info.title}\n\n"
            f"💰 Başlangıç fiyatı: {format_money(current_price)} TL\n\n"
            f"{coupon_line}\n\n"
            f"{max_quantity_line(info, cart_max_quantity)}\n\n"
            f"🎯 Ilk bildirim esigi: %{initial_drop_percent}\n"
            f"Sonraki fiyat dusus esigi: %{FOLLOWUP_DROP_PERCENT}",
            url,
        )
        return

    old_price = Decimal(str(product.get("base_price") or product.get("last_price") or info.price))
    drop = calculate_drop(old_price, current_price)

    if drop >= drop_percent:
        cart_max_quantity = await get_cached_or_probe_cart_quantity(product, url, info)
        await notify(
            app,
            chat_id,
            "🔥 Fiyat düştü\n\n"
            f"📦 {info.title}\n"
            f"💸 Eski fiyat: {format_money(old_price)} TL\n"
            f"💰 Yeni fiyat: {format_money(current_price)} TL\n"
            f"📉 Düşüş: %{drop:.2f}\n"
            f"Sonraki fiyat dusus esigi: %{FOLLOWUP_DROP_PERCENT}\n"
            f"{max_quantity_line(info, cart_max_quantity)}",
            url,
        )
        product["base_price"] = str(current_price)
        product["first_drop_notified"] = True

    last_coupon_text = product.get("last_coupon_text", "")
    if info.coupon_exists and info.coupon_text != last_coupon_text:
        cart_max_quantity = await get_cached_or_probe_cart_quantity(product, url, info)
        await notify(
            app,
            chat_id,
            "🎟️ Kupon bulundu\n\n"
            f"📦 {info.title}\n"
            f"🧾 {info.coupon_text}\n"
            f"{max_quantity_line(info, cart_max_quantity)}",
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
        all_targets = []
        for chat_id in chat_ids:
            for product in products.get(chat_id, []):
                if product.get("disabled"):
                    continue

                clamp_next_check(product)
                next_check_at = int(product.get("next_check_at", 0) or 0)
                if manual_chat_id or next_check_at <= now_ts():
                    all_targets.append((next_check_at, chat_id, product))

        all_targets.sort(key=lambda item: item[0])
        targets = [(chat_id, product) for _, chat_id, product in all_targets[:MAX_CHECKS_PER_CYCLE]]

    if not targets:
        return

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    async def run_one(chat_id: str, product: dict[str, Any]) -> None:
        async with semaphore:
            await check_product(app, chat_id, product)

    await asyncio.gather(
        *(run_one(chat_id, product) for chat_id, product in targets),
        return_exceptions=True,
    )

    async with data_lock:
        save_data(products)


async def background_checker(app: Application) -> None:
    while True:
        try:
            await check_all_products(app)
        except Exception:
            logger.exception("Background checker crashed during a cycle")

        await asyncio.sleep(CHECK_INTERVAL)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Telegram handler error", exc_info=context.error)


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
    app.add_error_handler(error_handler)

    logger.info("Telegram bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
