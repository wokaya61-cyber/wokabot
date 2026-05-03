# =========================================
# AMAZON TELEGRAM PRICE BOT
# HATASIZ STABİL SÜRÜM
# =========================================

# KURULUM:
#
# pip install python-telegram-bot==20.3
# pip install selenium==4.21.0
# pip install beautifulsoup4==4.12.3
#
# =========================================

import json
import os
import re
import time
import threading
import random

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

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# =========================================
# TELEGRAM TOKEN
# =========================================

BOT_TOKEN = "8795704026:AAEjTlhdbWwPmdYWobzCmruRBBToe0gWOuQ"

# =========================================
# AYARLAR
# =========================================

CHECK_INTERVAL = 10

DATA_FILE = "products.json"

# =========================================
# DATA LOAD
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
# CHROME DRIVER
# =========================================

def create_driver():

    options = Options()

    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.binary_location = "/usr/bin/google-chrome"

    user_agents = [

        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",

        "Mozilla/5.0 (X11; Linux x86_64)",

        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    ]

    ua = random.choice(user_agents)

    options.add_argument(
        f"user-agent={ua}"
    )

    
	from selenium.webdriver.chrome.service import Service
	from webdriver_manager.chrome import ChromeDriverManager

	driver = webdriver.Chrome(
    	service=Service(
        ChromeDriverManager().install()
    	),
    	options=options
	)

    )

    return driver


driver = create_driver()

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
# AMAZON SCRAPER
# =========================================

def get_product_info(url):

    try:

        driver.get(url)

        time.sleep(4)

        soup = BeautifulSoup(
            driver.page_source,
            "html.parser"
        )

        # TITLE

        title = ""

        title_el = soup.select_one(
            "#productTitle"
        )

        if title_el:
            title = title_el.text.strip()

        # SELLER

        seller_ok = False

        page_text = soup.get_text(
            " ",
            strip=True
        ).lower()

        seller_patterns = [

            "amazon.com.tr tarafından satılmaktadır",

            "satıcı amazon.com.tr",

            "gönderen amazon.com.tr"
        ]

        for p in seller_patterns:

            if p in page_text:
                seller_ok = True
                break

        # PRICE

        price = None

        whole = soup.select_one(
            ".a-price-whole"
        )

        fraction = soup.select_one(
            ".a-price-fraction"
        )

        if whole:

            price_text = whole.text

            if fraction:
                price_text += "." + fraction.text

            price = parse_price(
                price_text
            )

        # COUPON

        coupon_exists = False

        coupon_text = ""

        coupon_patterns = [

            r"kupon",

            r"indirim",

            r"sepette",

            r"%\d+ indirim",

            r"₺\d+ kupon"
        ]

        all_texts = soup.find_all(
            string=True
        )

        for t in all_texts:

            txt = t.strip()

            for pattern in coupon_patterns:

                if re.search(
                    pattern,
                    txt,
                    re.IGNORECASE
                ):

                    coupon_exists = True
                    coupon_text = txt
                    break

            if coupon_exists:
                break

        # STOCK

        in_stock = True

        stock_patterns = [

            "stokta yok",

            "currently unavailable"
        ]

        for s in stock_patterns:

            if s in page_text:
                in_stock = False
                break

        return {

            "title": title,

            "price": price,

            "seller_ok": seller_ok,

            "coupon_exists": coupon_exists,

            "coupon_text": coupon_text,

            "in_stock": in_stock
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

/add https://amazon.com.tr/... 15
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

    url = context.args[0]

    url = url.split("?")[0]

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
            "Ürün okunamadı."
        )

        return

    if not info["seller_ok"]:

        await update.message.reply_text(
            "❌ Satıcı amazon.com.tr değil."
        )

        return

    if not info["price"]:

        await update.message.reply_text(
            "❌ Fiyat okunamadı."
        )

        return

    if chat_id not in products:
        products[chat_id] = []

    # DUPLICATE

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

    url = context.args[0]

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
# PRICE CALCULATOR
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
# TELEGRAM NOTIFY
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
# PRICE CHECKER
# =========================================

def background_checker(app):

    import asyncio

    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)

    global driver

    request_count = 0

    while True:

        try:

            for chat_id in list(products.keys()):

                for product in products[chat_id]:

                    request_count += 1

                    # DRIVER RESET

                    if request_count >= 50:

                        driver.quit()

                        driver = create_driver()

                        request_count = 0

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
