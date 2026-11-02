"""Telegram-бот магазин с оплатой через Telegram Stars и банковскую карту.

Настройки берутся из переменных окружения (см. .env.example):
  BOT_TOKEN       — токен бота от @BotFather (обязательно)
  PROVIDER_TOKEN  — токен платёжного провайдера для оплаты картой (необязательно)
  ADMIN_ID        — ваш Telegram ID для уведомлений о продажах (необязательно)

Товары описываются в products.json.
"""

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

import telebot
from telebot import types

BASE_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """Простая загрузка .env без внешних библиотек."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
PROVIDER_TOKEN = os.environ.get("PROVIDER_TOKEN", "")
ADMIN_ID = int(os.environ["ADMIN_ID"]) if os.environ.get("ADMIN_ID") else None
CARD_CURRENCY = os.environ.get("CARD_CURRENCY", "RUB")
DB_PATH = BASE_DIR / "shop.db"

if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN. Создайте файл .env по образцу .env.example")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("shop")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")


# ---------- Товары ----------

def load_products() -> dict:
    with open(BASE_DIR / "products.json", encoding="utf-8") as f:
        items = json.load(f)
    return {p["id"]: p for p in items}


PRODUCTS = load_products()


def payment_methods(product: dict) -> list:
    """Какими способами можно оплатить товар.

    По правилам Telegram цифровые товары продаются только за Stars,
    физические товары и услуги — через платёжного провайдера (карта).
    """
    methods = []
    if product["type"] == "digital" and product.get("price_stars"):
        methods.append("stars")
    if product["type"] == "physical" and product.get("price_rub") and PROVIDER_TOKEN:
        methods.append("card")
    return methods


def price_text(product: dict) -> str:
    if product["type"] == "digital":
        return f"{product['price_stars']} ⭐"
    return f"{product['price_rub']} ₽"


# ---------- База данных ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            product_id TEXT NOT NULL,
            method TEXT NOT NULL,
            currency TEXT NOT NULL,
            amount INTEGER NOT NULL,
            telegram_charge_id TEXT UNIQUE,
            provider_charge_id TEXT,
            created_at INTEGER NOT NULL,
            refunded INTEGER NOT NULL DEFAULT 0
        )"""
    )
    return conn


def save_purchase(user, product_id, method, payment) -> None:
    with db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO purchases
               (user_id, username, product_id, method, currency, amount,
                telegram_charge_id, provider_charge_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user.id,
                user.username,
                product_id,
                method,
                payment.currency,
                payment.total_amount,
                payment.telegram_payment_charge_id,
                payment.provider_payment_charge_id,
                int(time.time()),
            ),
        )


# ---------- Клавиатуры ----------

def catalog_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    for p in PRODUCTS.values():
        if payment_methods(p):
            kb.add(types.InlineKeyboardButton(
                f"{p['title']} — {price_text(p)}", callback_data=f"show:{p['id']}"))
    return kb


def product_keyboard(product: dict) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    methods = payment_methods(product)
    if "stars" in methods:
        kb.add(types.InlineKeyboardButton(
            f"⭐ Купить за {product['price_stars']} Stars", callback_data=f"buy:stars:{product['id']}"))
    if "card" in methods:
        kb.add(types.InlineKeyboardButton(
            f"💳 Оплатить картой {product['price_rub']} ₽", callback_data=f"buy:card:{product['id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад в каталог", callback_data="catalog"))
    return kb


# ---------- Команды ----------

@bot.message_handler(commands=["start", "shop"])
def cmd_start(message):
    bot.send_message(
        message.chat.id,
        "Привет! 👋 Это магазин.\nВыберите товар:",
        reply_markup=catalog_keyboard(),
    )


@bot.message_handler(commands=["mypurchases"])
def cmd_my_purchases(message):
    with db() as conn:
        rows = conn.execute(
            "SELECT product_id, amount, currency, created_at, refunded FROM purchases "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT 20",
            (message.from_user.id,),
        ).fetchall()
    if not rows:
        bot.send_message(message.chat.id, "У вас пока нет покупок.")
        return
    lines = ["<b>Ваши покупки:</b>"]
    for product_id, amount, currency, ts, refunded in rows:
        title = PRODUCTS.get(product_id, {}).get("title", product_id)
        shown = f"{amount} ⭐" if currency == "XTR" else f"{amount / 100:.2f} {currency}"
        date = time.strftime("%d.%m.%Y", time.localtime(ts))
        mark = " (возврат)" if refunded else ""
        lines.append(f"• {title} — {shown}, {date}{mark}")
    bot.send_message(message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["paysupport"])
def cmd_pay_support(message):
    # Telegram требует, чтобы у бота с платежами была команда /paysupport.
    bot.send_message(
        message.chat.id,
        "Проблема с оплатой? Опишите её одним сообщением, указав, что и когда вы покупали, — "
        "мы ответим как можно скорее.",
    )
    if ADMIN_ID:
        bot.send_message(ADMIN_ID, f"🆘 /paysupport от {user_link(message.from_user)}")


@bot.message_handler(commands=["terms"])
def cmd_terms(message):
    bot.send_message(
        message.chat.id,
        "<b>Условия покупки</b>\n"
        "• Цифровые товары выдаются сразу после оплаты.\n"
        "• Если товар не получен, напишите /paysupport — мы поможем или вернём деньги.\n"
        "• Физические товары доставляются по указанному адресу.",
    )


@bot.message_handler(commands=["refund"])
def cmd_refund(message):
    """Возврат Stars: /refund <user_id> <telegram_charge_id> (только для админа)."""
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.reply_to(message, "Формат: /refund <user_id> <telegram_charge_id>")
        return
    user_id, charge_id = int(parts[1]), parts[2]
    try:
        bot.refund_star_payment(user_id, charge_id)
    except telebot.apihelper.ApiTelegramException as e:
        bot.reply_to(message, f"Не получилось: {e.description}")
        return
    with db() as conn:
        conn.execute("UPDATE purchases SET refunded = 1 WHERE telegram_charge_id = ?", (charge_id,))
    bot.reply_to(message, "✅ Stars возвращены.")


@bot.message_handler(commands=["sales"])
def cmd_sales(message):
    """Короткая статистика продаж (только для админа)."""
    if message.from_user.id != ADMIN_ID:
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT currency, COUNT(*), SUM(amount) FROM purchases WHERE refunded = 0 GROUP BY currency"
        ).fetchall()
    if not rows:
        bot.reply_to(message, "Продаж пока нет.")
        return
    lines = ["<b>Продажи:</b>"]
    for currency, count, total in rows:
        shown = f"{total} ⭐" if currency == "XTR" else f"{total / 100:.2f} {currency}"
        lines.append(f"• {currency}: {count} шт., {shown}")
    bot.reply_to(message, "\n".join(lines))


# ---------- Кнопки ----------

@bot.callback_query_handler(func=lambda c: c.data == "catalog")
def cb_catalog(call):
    bot.edit_message_text(
        "Выберите товар:", call.message.chat.id, call.message.message_id,
        reply_markup=catalog_keyboard())
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("show:"))
def cb_show(call):
    product = PRODUCTS.get(call.data.split(":", 1)[1])
    if not product:
        bot.answer_callback_query(call.id, "Товар не найден")
        return
    text = f"<b>{product['title']}</b>\n\n{product['description']}\n\nЦена: {price_text(product)}"
    bot.edit_message_text(
        text, call.message.chat.id, call.message.message_id,
        reply_markup=product_keyboard(product))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("buy:"))
def cb_buy(call):
    _, method, product_id = call.data.split(":", 2)
    product = PRODUCTS.get(product_id)
    if not product or method not in payment_methods(product):
        bot.answer_callback_query(call.id, "Этот способ оплаты недоступен")
        return

    payload = f"{product_id}:{method}"
    if method == "stars":
        bot.send_invoice(
            call.message.chat.id,
            title=product["title"],
            description=product["description"],
            invoice_payload=payload,
            provider_token="",  # для Stars токен провайдера не нужен
            currency="XTR",
            prices=[types.LabeledPrice(product["title"], product["price_stars"])],
        )
    else:
        bot.send_invoice(
            call.message.chat.id,
            title=product["title"],
            description=product["description"],
            invoice_payload=payload,
            provider_token=PROVIDER_TOKEN,
            currency=CARD_CURRENCY,
            # сумма в копейках
            prices=[types.LabeledPrice(product["title"], product["price_rub"] * 100)],
            need_name=True,
            need_phone_number=True,
            # для услуг адрес не нужен: "need_address": false в products.json
            need_shipping_address=product.get("need_address", True),
        )
    bot.answer_callback_query(call.id)


# ---------- Оплата ----------

def expected_amount(product: dict, method: str):
    if method == "stars":
        return "XTR", product["price_stars"]
    return CARD_CURRENCY, product["price_rub"] * 100


@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(query):
    """Последняя проверка перед списанием денег: товар существует и цена совпадает."""
    try:
        product_id, method = query.invoice_payload.split(":", 1)
        product = PRODUCTS[product_id]
        ok = (query.currency, query.total_amount) == expected_amount(product, method)
    except (ValueError, KeyError):
        ok = False
    if ok:
        bot.answer_pre_checkout_query(query.id, ok=True)
    else:
        bot.answer_pre_checkout_query(
            query.id, ok=False, error_message="Товар недоступен или изменилась цена. Откройте /shop заново.")


@bot.message_handler(content_types=["successful_payment"])
def on_paid(message):
    payment = message.successful_payment
    product_id, method = payment.invoice_payload.split(":", 1)
    product = PRODUCTS.get(product_id, {"title": product_id, "delivery": "Спасибо за покупку!"})

    save_purchase(message.from_user, product_id, method, payment)
    log.info("Оплата: user=%s product=%s %s %s",
             message.from_user.id, product_id, payment.total_amount, payment.currency)

    # Выдаём товар
    bot.send_message(message.chat.id, f"✅ Оплата прошла!\n\n{product['delivery']}")
    file_name = product.get("file")
    if file_name:
        with open(BASE_DIR / file_name, "rb") as f:
            bot.send_document(message.chat.id, f)

    # Уведомляем владельца
    if ADMIN_ID:
        shown = (f"{payment.total_amount} ⭐" if payment.currency == "XTR"
                 else f"{payment.total_amount / 100:.2f} {payment.currency}")
        text = (f"💰 Новая продажа: <b>{product['title']}</b> за {shown}\n"
                f"Покупатель: {user_link(message.from_user)}\n"
                f"charge_id: <code>{payment.telegram_payment_charge_id}</code>")
        info = payment.order_info
        if info:
            if info.name:
                text += f"\nИмя: {info.name}"
            if info.phone_number:
                text += f"\nТелефон: {info.phone_number}"
            if info.shipping_address:
                a = info.shipping_address
                text += f"\nАдрес: {a.country_code}, {a.state}, {a.city}, {a.street_line1} {a.street_line2}, {a.post_code}"
        bot.send_message(ADMIN_ID, text)


def user_link(user) -> str:
    name = telebot.formatting.escape_html(user.full_name)
    return f'<a href="tg://user?id={user.id}">{name}</a> (id {user.id})'


if __name__ == "__main__":
    db().close()
    bot.set_my_commands([
        types.BotCommand("shop", "Каталог товаров"),
        types.BotCommand("mypurchases", "Мои покупки"),
        types.BotCommand("paysupport", "Помощь с оплатой"),
        types.BotCommand("terms", "Условия покупки"),
    ])
    log.info("Бот запущен. Оплата картой: %s", "включена" if PROVIDER_TOKEN else "выключена")
    bot.infinity_polling()
