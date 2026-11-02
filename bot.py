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
# Постоянное хранилище: на Amvera это /data (не стирается при обновлениях бота)
DATA_DIR = Path(os.environ.get("DATA_DIR") or ("/data" if Path("/data").is_dir() else BASE_DIR))
DB_PATH = DATA_DIR / "shop.db"


def find_file(name: str) -> Path:
    """Ищет файл товара/курса рядом с ботом, а если его там нет — в DATA_DIR."""
    local = BASE_DIR / name
    return local if local.exists() else DATA_DIR / name

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


# ---------- Курс внутри бота ----------
# Уроки лежат в files/lessons.json: [{"title": ..., "free": true/false, "text": "<HTML>"}, ...]
# Уроки с "free": true доступны всем, остальные — после покупки товара с "course": true.

LESSONS_PATH = find_file("files/lessons.json")
LESSONS = json.loads(LESSONS_PATH.read_text(encoding="utf-8")) if LESSONS_PATH.exists() else []
COURSE_PRODUCT = next((p for p in PRODUCTS.values() if p.get("course")), None)
TG_LIMIT = 4000  # лимит сообщения Telegram — 4096 символов
if not COURSE_PRODUCT:
    # Нет товара-курса — показываем только бесплатные уроки
    LESSONS = [lesson for lesson in LESSONS if lesson.get("free")]


def has_course(user_id: int) -> bool:
    if not COURSE_PRODUCT:
        return False
    if user_id == ADMIN_ID:
        return True
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM purchases WHERE user_id = ? AND product_id = ? AND refunded = 0 LIMIT 1",
            (user_id, COURSE_PRODUCT["id"]),
        ).fetchone()
    return row is not None


def lesson_open(idx: int, user_id: int) -> bool:
    return LESSONS[idx].get("free") or has_course(user_id)


def split_text(text: str) -> list:
    """Делит длинный урок на части по абзацам, чтобы не превысить лимит Telegram."""
    parts, current = [], ""
    for para in text.split("\n\n"):
        if current and len(current) + len(para) + 2 > TG_LIMIT:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        parts.append(current)
    return parts


def buy_course_button() -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(
        f"🔓 Открыть весь курс за {COURSE_PRODUCT['price_stars']} ⭐",
        callback_data=f"buy:stars:{COURSE_PRODUCT['id']}")


def lesson_keyboard(idx: int, user_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    nav = []
    if idx > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"lesson:{idx - 1}"))
    if idx + 1 < len(LESSONS):
        if lesson_open(idx + 1, user_id):
            nav.append(types.InlineKeyboardButton("Дальше ➡️", callback_data=f"lesson:{idx + 1}"))
    if nav:
        kb.row(*nav)
    if idx + 1 < len(LESSONS) and not lesson_open(idx + 1, user_id):
        kb.add(buy_course_button())
    kb.add(types.InlineKeyboardButton("📚 Все уроки", callback_data="lessons"))
    return kb


def lessons_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    bought = has_course(user_id)
    for i, lesson in enumerate(LESSONS):
        icon = "🎁" if lesson.get("free") else ("📖" if bought else "🔒")
        kb.add(types.InlineKeyboardButton(f"{icon} {lesson['title']}", callback_data=f"lesson:{i}"))
    if not bought:
        kb.add(buy_course_button())
    return kb


def send_lessons_list(chat_id: int, user_id: int) -> None:
    if has_course(user_id):
        text = "📚 <b>Курс открыт полностью.</b> Выберите урок:"
    else:
        text = ("📚 <b>Уроки курса</b>\n\n🎁 — бесплатно, 🔒 — после покупки.\n"
                "Начните с бесплатных уроков!")
    bot.send_message(chat_id, text, reply_markup=lessons_keyboard(user_id))


def send_lesson(chat_id: int, user_id: int, idx: int) -> None:
    lesson = LESSONS[idx]
    if not lesson_open(idx, user_id):
        kb = types.InlineKeyboardMarkup()
        kb.add(buy_course_button())
        kb.add(types.InlineKeyboardButton("🎁 Бесплатный урок", callback_data="lesson:0"))
        bot.send_message(
            chat_id,
            f"🔒 <b>{lesson['title']}</b>\n\nЭтот урок откроется после покупки курса. "
            f"Сразу после оплаты вам станут доступны все уроки и PDF-версия курса.",
            reply_markup=kb)
        return
    parts = split_text(f"<b>{lesson['title']}</b>\n\n{lesson['text']}")
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        bot.send_message(chat_id, part, reply_markup=lesson_keyboard(idx, user_id) if last else None,
                         disable_web_page_preview=True)


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
    if LESSONS:
        kb.add(types.InlineKeyboardButton("🎁 Бесплатный урок", callback_data="lesson:0"))
        kb.add(types.InlineKeyboardButton("📚 Все уроки курса", callback_data="lessons"))
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


@bot.message_handler(commands=["course", "free"])
def cmd_course(message):
    if not LESSONS:
        bot.send_message(message.chat.id, "Курс скоро появится!")
        return
    if message.text.startswith("/free"):
        send_lesson(message.chat.id, message.from_user.id, 0)
    else:
        send_lessons_list(message.chat.id, message.from_user.id)


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


@bot.callback_query_handler(func=lambda c: c.data == "lessons")
def cb_lessons(call):
    bot.answer_callback_query(call.id)
    send_lessons_list(call.message.chat.id, call.from_user.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("lesson:"))
def cb_lesson(call):
    try:
        idx = int(call.data.split(":", 1)[1])
        LESSONS[idx]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Урок не найден")
        return
    bot.answer_callback_query(call.id)
    send_lesson(call.message.chat.id, call.from_user.id, idx)


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
        file_path = find_file(file_name)
        if file_path.exists():
            with open(file_path, "rb") as f:
                bot.send_document(message.chat.id, f)
        else:
            log.error("Файл товара не найден: %s", file_path)
            bot.send_message(message.chat.id, "Файл скоро придёт — мы уже получили уведомление.")
            if ADMIN_ID:
                bot.send_message(ADMIN_ID, f"⚠️ Не найден файл товара <code>{file_name}</code> — "
                                           f"отправьте его покупателю вручную: {user_link(message.from_user)}")

    if product.get("course") and LESSONS:
        kb = types.InlineKeyboardMarkup()
        first_paid = next((i for i, l in enumerate(LESSONS) if not l.get("free")), 0)
        kb.add(types.InlineKeyboardButton("▶️ Продолжить курс", callback_data=f"lesson:{first_paid}"))
        kb.add(types.InlineKeyboardButton("📚 Все уроки", callback_data="lessons"))
        bot.send_message(message.chat.id, "🔓 Все уроки открыты! Проходите прямо здесь, в боте:",
                         reply_markup=kb)

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
        types.BotCommand("course", "Уроки курса"),
        types.BotCommand("free", "Бесплатный урок"),
        types.BotCommand("mypurchases", "Мои покупки"),
        types.BotCommand("paysupport", "Помощь с оплатой"),
        types.BotCommand("terms", "Условия покупки"),
    ])
    log.info("Бот запущен. Оплата картой: %s", "включена" if PROVIDER_TOKEN else "выключена")
    bot.infinity_polling()
