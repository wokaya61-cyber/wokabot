# =========================================
# AMAZON TELEGRAM PRICE BOT
# PROFESSIONAL VERSION
# AUTO CLEAN AMAZON LINKS
# =========================================

import undetected_chromedriver as uc
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

import json
import os
import re
import time
import threading

from bs4 import BeautifulSoup

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)

from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)


# =========================================
# TOKEN
# =========================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

# =========================================
# SETTINGS
# =========================================

CHECK_INTERVAL = 10

DATA_FILE = "products.json"

# =========================================
# LOAD / SAVE
# =========================================

def load_data():

    if not os.path.exists(DATA_FILE):
        return {}

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            indent=4,
            ensure_ascii=False
        )

products = load_data()

# =========================================
# AMAZON LINK CLEANER
# =========================================

def clean_amazon_url(url):

    patterns = [

        r"/dp/([A-Z0-9]{10})",

        r"/gp/product/([A-Z0-9]{10})"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            url
        )

        if match:

            asin = match.group(1)

            return f"https://www.amazon.com.tr/dp/{asin}"

    return url.split("?")[0]

# =========================================
# PRICE PARSER
# =========================================

def parse_price(text):

    text = text.replace(".", "")
    text = text.replace(",", ".")

    nums = re.findall(
        r"\d+\.\d+|\d+",
        text
    )

    if nums:
        return float(nums[0])

    return None

# =========================================
# PRODUCT SCRAPER
# =========================================

def get_product_info(url):

    try:

        options = Options()

        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        options.add_argument(
            "user-agent=Mozilla/5.0"
        )

                driver = uc.Chrome(
        options=options,
        headless=True,
        use_subprocess=True
        )
        

        driver.get(url)

        time.sleep(8)

        html = driver.page_source

        driver.quit()

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        title = ""

        title_el = soup.select_one(
            "#productTitle"
        )

        if title_el:
            title = title_el.text.strip()

        price = None

        selectors = [

            ".a-price .a-offscreen",

            "#priceblock_ourprice",

            "#priceblock_dealprice",

            ".a-price-whole"
        ]

        for selector in selectors:

            el = soup.select_one(selector)

            if el:

                price = parse_price(
                    el.text
                )

                if price:
                    break

        if not title:
            return None

        return {

            "title": title,

            "price": price,

            "seller_ok": True,

            "coupon_exists": False,

            "coupon_text": "",

            "in_stock": True
        }

    except Exception as e:

        print("SCRAPER ERROR:", e)

        return None


# =========================================
# TELEGRAM COMMANDS
# =========================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = """
🤖 AMAZON FİYAT TAKİP BOTU

Komutlar:

/add URL YUZDE
/remove URL
/list
/check

Örnek:

/add https://www.amazon.com.tr/dp/B0BJQP23Y8 15
"""

    await update.message.reply_text(text)

# =========================================

async def add_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = str(
        update.effective_chat.id
    )

    if len(context.args) < 2:

        await update.message.reply_text(
            "Kullanım:\n/add URL YUZDE"
        )

        return

    raw_url = context.args[0]

    url = clean_amazon_url(
        raw_url
    )

    try:

        drop_percent = float(
            context.args[1]
        )

    except:

        await update.message.reply_text(
            "Yüzde hatalı."
        )

        return

    info = get_product_info(url)

    if not info:

        await update.message.reply_text(
            "❌ Ürün okunamadı."
        )

        return

    if not info["price"]:

        await update.message.reply_text(
            "❌ Fiyat okunamadı."
        )

        return

    if chat_id not in products:
        products[chat_id] = []

    for p in products[chat_id]:

        if p["url"] == url:

            await update.message.reply_text(
                "⚠️ Ürün zaten ekli."
            )

            return

    products[chat_id].append({

        "url": url,

        "title": info["title"],

        "base_price": info["price"],

        "last_price": info["price"],

        "drop_percent": drop_percent,

        "coupon_notified": False,

        "stock_notified": False
    })

    save_data(products)

    await update.message.reply_text(
        f"""
✅ Ürün Eklendi

📦 {info['title']}

💰 {info['price']} TL

🎯 %{drop_percent} düşüş bildirimi aktif

🔗 {url}
"""
    )

# =========================================

async def remove_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = str(
        update.effective_chat.id
    )

    if len(context.args) < 1:

        await update.message.reply_text(
            "Kullanım:\n/remove URL"
        )

        return

    url = clean_amazon_url(
        context.args[0]
    )

    if chat_id not in products:

        await update.message.reply_text(
            "Liste boş."
        )

        return

    before = len(
        products[chat_id]
    )

    products[chat_id] = [

        p for p in products[chat_id]

        if p["url"] != url
    ]

    after = len(
        products[chat_id]
    )

    save_data(products)

    if before == after:

        await update.message.reply_text(
            "Ürün bulunamadı."
        )

    else:

        await update.message.reply_text(
            "✅ Ürün silindi."
        )

# =========================================

async def list_products(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = str(
        update.effective_chat.id
    )

    if (
        chat_id not in products
        or
        not products[chat_id]
    ):

        await update.message.reply_text(
            "Liste boş."
        )

        return

    msg = "📋 TAKİP EDİLEN ÜRÜNLER\n\n"

    for i, p in enumerate(
        products[chat_id],
        start=1
    ):

        msg += (
            f"{i}. {p['title']}\n"
            f"💰 {p['base_price']} TL\n"
            f"🎯 %{p['drop_percent']}\n"
            f"{p['url']}\n\n"
        )

    await update.message.reply_text(msg)

# =========================================

async def check_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "✅ Bot çalışıyor."
    )

# =========================================
# PRICE DROP
# =========================================

def calculate_drop(
    old_price,
    new_price
):

    return (
        (
            old_price - new_price
        ) / old_price
    ) * 100

# =========================================
# TELEGRAM SEND
# =========================================

async def notify(
    app,
    chat_id,
    text,
    url
):

    keyboard = [

        [
            InlineKeyboardButton(
                "🛒 Amazon'da Aç",
                url=url
            )
        ]
    ]

    reply_markup = InlineKeyboardMarkup(
        keyboard
    )

    await app.bot.send_message(
        chat_id=int(chat_id),
        text=text,
        reply_markup=reply_markup
    )

# =========================================
# BACKGROUND CHECKER
# =========================================

def background_checker(app):

    import asyncio

    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)

    while True:

        try:

            for chat_id in list(products.keys()):

                for product in products[chat_id]:

                    info = get_product_info(
                        product["url"]
                    )

                    if not info:
                        continue

                    if not info["seller_ok"]:
                        continue

                    current_price = info["price"]

                    if not current_price:
                        continue

                    drop = calculate_drop(
                        product["base_price"],
                        current_price
                    )

                    # PRICE DROP

                    if (
                        drop >=
                        product["drop_percent"]
                    ):

                        text = f"""
🔥 FİYAT DÜŞTÜ

📦 {info['title']}

💰 Eski:
{product['base_price']} TL

💸 Yeni:
{current_price} TL

📉 %{drop:.2f} düşüş
"""

                        loop.run_until_complete(
                            notify(
                                app,
                                chat_id,
                                text,
                                product["url"]
                            )
                        )

                        product["base_price"] = current_price

                    # COUPON

                    if (
                        info["coupon_exists"]
                        and
                        not product["coupon_notified"]
                    ):

                        text = f"""
🎟 KUPON BULUNDU

📦 {info['title']}

🧾 {info['coupon_text']}
"""

                        loop.run_until_complete(
                            notify(
                                app,
                                chat_id,
                                text,
                                product["url"]
                            )
                        )

                        product["coupon_notified"] = True

                    if not info["coupon_exists"]:

                        product["coupon_notified"] = False

                    # STOCK

                    if (
                        info["in_stock"]
                        and
                        not product["stock_notified"]
                    ):

                        text = f"""
📦 ÜRÜN STOKTA

📦 {info['title']}
"""

                        loop.run_until_complete(
                            notify(
                                app,
                                chat_id,
                                text,
                                product["url"]
                            )
                        )

                        product["stock_notified"] = True

                    if not info["in_stock"]:

                        product["stock_notified"] = False

                    product["last_price"] = current_price

                    save_data(products)

        except Exception as e:

            print("CHECK ERROR:", e)

        time.sleep(CHECK_INTERVAL)

# =========================================
# MAIN
# =========================================

def main():

    app = Application.builder().token(
        BOT_TOKEN
    ).build()

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "add",
            add_product
        )
    )

    app.add_handler(
        CommandHandler(
            "remove",
            remove_product
        )
    )

    app.add_handler(
        CommandHandler(
            "list",
            list_products
        )
    )

    app.add_handler(
        CommandHandler(
            "check",
            check_command
        )
    )

    checker_thread = threading.Thread(
        target=background_checker,
        args=(app,),
        daemon=True
    )

    checker_thread.start()

    print("🤖 BOT STARTED")

    app.run_polling()

# =========================================

if __name__ == "__main__":
    main()

