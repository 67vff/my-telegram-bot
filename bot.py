import os
import uuid
import hmac
import time
import sqlite3
import hashlib
import threading
import base64
import urllib.parse
import telebot
import requests
import urllib3
from hashlib import sha256
from flask import Flask, request, jsonify

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BOT_TOKEN = os.getenv('BOT_TOKEN')
YOOMONEY_RECEIVER = os.getenv('YOOMONEY_RECEIVER')
YOOMONEY_SECRET = os.getenv('YOOMONEY_SECRET')
GIGACHAT_AUTH_KEY = os.getenv('GIGACHAT_AUTH_KEY')

ADMIN_ID = 8000630493

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

DB_PATH = "/app/data/users.db"
TRIAL_PRICE = 10
TRIAL_TOKENS = 2
TRIAL_WINDOW = 3600

def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# === БАЗА ДАННЫХ ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        tokens INTEGER DEFAULT 0,
        state TEXT DEFAULT 'idle',
        mode TEXT DEFAULT 'regular',
        trial_started INTEGER DEFAULT 0,
        trial_used INTEGER DEFAULT 0,
        username TEXT DEFAULT '',
        theme TEXT DEFAULT 'bright',
        night_mode INTEGER DEFAULT 1,
        notifications INTEGER DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        tokens INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        role TEXT,
        content TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        tokens_spent INTEGER,
        timestamp INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        username TEXT,
        message TEXT,
        photo_id TEXT,
        status TEXT DEFAULT 'open',
        answer TEXT,
        created INTEGER,
        answered INTEGER
    )''')
    conn.commit()
    for col, definition in [
        ("mode", "TEXT DEFAULT 'regular'"),
        ("trial_started", "INTEGER DEFAULT 0"),
        ("trial_used", "INTEGER DEFAULT 0"),
        ("username", "TEXT DEFAULT ''"),
        ("theme", "TEXT DEFAULT 'bright'"),
        ("night_mode", "INTEGER DEFAULT 1"),
        ("notifications", "INTEGER DEFAULT 1"),
    ]:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    conn.close()

init_db()

def get_user(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("SELECT tokens, state, mode, trial_started, trial_used, username, theme, night_mode, notifications FROM users WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
    except sqlite3.OperationalError:
        conn.close()
        init_db()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT tokens, state, mode, trial_started, trial_used, username, theme, night_mode, notifications FROM users WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
    if not row:
        c.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        c.execute("SELECT tokens, state, mode, trial_started, trial_used, username, theme, night_mode, notifications FROM users WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        if not row:
            row = (0, 'idle', 'regular', 0, 0, '', 'bright', 1, 1)
    conn.close()
    return row

def update_user(chat_id, field, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"UPDATE users SET {field}=? WHERE chat_id=?", (value, chat_id))
    conn.commit()
    conn.close()

def add_tokens(chat_id, count):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET tokens = tokens + ? WHERE chat_id=?", (count, chat_id))
    conn.commit()
    conn.close()

def log_stat(chat_id, tokens_spent):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO stats (chat_id, tokens_spent, timestamp) VALUES (?, ?, ?)", (chat_id, tokens_spent, int(time.time())))
    conn.commit()
    conn.close()

def get_stats(chat_id):
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(tokens_spent),0) FROM stats WHERE chat_id=? AND timestamp>?", (chat_id, now - 86400))
    day = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(tokens_spent),0) FROM stats WHERE chat_id=? AND timestamp>?", (chat_id, now - 604800))
    week = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(tokens_spent),0) FROM stats WHERE chat_id=? AND timestamp>?", (chat_id, now - 2592000))
    month = c.fetchone()[0]
    conn.close()
    return day, week, month

def save_order(order_id, chat_id, tokens):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO orders VALUES (?, ?, ?)", (order_id, chat_id, tokens))
    conn.commit()
    conn.close()

def get_order(order_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id, tokens FROM orders WHERE order_id=?", (order_id,))
    row = c.fetchone()
    conn.close()
    return row

def add_to_history(chat_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO history (chat_id, role, content) VALUES (?, ?, ?)", (chat_id, role, content))
    conn.commit()
    conn.close()

def get_history(chat_id, limit=50):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit))
    rows = c.fetchall()
    conn.close()
    return list(reversed(rows))

def clear_history(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM history WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

# === ТИКЕТЫ ===
def create_ticket(chat_id, username, message, photo_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO tickets (chat_id, username, message, photo_id, created) VALUES (?, ?, ?, ?, ?)",
              (chat_id, username, message, photo_id, int(time.time())))
    conn.commit()
    ticket_id = c.lastrowid
    conn.close()
    return ticket_id

def get_open_tickets():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, chat_id, username, message, photo_id, created FROM tickets WHERE status='open' ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    return rows

def get_ticket(ticket_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, chat_id, username, message, photo_id, status FROM tickets WHERE id=?", (ticket_id,))
    row = c.fetchone()
    conn.close()
    return row

def answer_ticket(ticket_id, answer):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE tickets SET status='closed', answer=?, answered=? WHERE id=?", (answer, int(time.time()), ticket_id))
    conn.commit()
    conn.close()

def get_user_tickets(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, message, answer, status FROM tickets WHERE chat_id=? ORDER BY id DESC LIMIT 10", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows

# === УДАЛЕНИЕ СТАРЫХ СООБЩЕНИЙ ===
last_messages = {}

def clear_old_messages(chat_id):
    for msg_id in last_messages.get(chat_id, []):
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    last_messages[chat_id] = []

def remember(chat_id, msg_id):
    last_messages.setdefault(chat_id, []).append(msg_id)

# === МЕНЮ ===
def main_menu(chat_id=None):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
    markup.add(telebot.types.InlineKeyboardButton("🤖 Чат с ИИ", callback_data="menu_chat"))
    markup.add(telebot.types.InlineKeyboardButton("📜 История чата", callback_data="menu_history"))
    markup.add(telebot.types.InlineKeyboardButton("💰 Мой баланс", callback_data="menu_balance"))
    markup.add(telebot.types.InlineKeyboardButton("🆘 Поддержка", callback_data="menu_support"))
    if chat_id == ADMIN_ID:
        markup.add(telebot.types.InlineKeyboardButton("👑 Админ-меню", callback_data="menu_admin"))
    return markup

def admin_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📋 Тикеты", callback_data="admin_tickets"))
    markup.add(telebot.types.InlineKeyboardButton("💰 Начислить токены", callback_data="admin_give"))
    markup.add(telebot.types.InlineKeyboardButton("👥 Список пользователей", callback_data="admin_users"))
    markup.add(telebot.types.InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

def buy_menu(chat_id):
    markup = telebot.types.InlineKeyboardMarkup()
    _, _, _, trial_started, trial_used, _, _, _, _ = get_user(chat_id)
    now = int(time.time())
    if not trial_used and trial_started > 0 and (now - trial_started) < TRIAL_WINDOW:
        left = TRIAL_WINDOW - (now - trial_started)
        minutes = left // 60
        markup.add(telebot.types.InlineKeyboardButton(
            f"🎁 Подарок: 2 токена за 10 ₽ (осталось {minutes} мин)",
            callback_data="pack_trial"
        ))
    markup.add(telebot.types.InlineKeyboardButton("100 ₽ — 20 токенов", callback_data="pack_100_20"))
    markup.add(telebot.types.InlineKeyboardButton("250 ₽ — 50 токенов", callback_data="pack_250_50"))
    markup.add(telebot.types.InlineKeyboardButton("500 ₽ — 100 токенов", callback_data="pack_500_100"))
    markup.add(telebot.types.InlineKeyboardButton("✏️ Своя сумма", callback_data="custom_amount"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

def gift_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Приобрести", callback_data="pack_trial"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Не надо", callback_data="decline_gift"))
    return markup

def mode_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🤖 Обычный ИИ", callback_data="mode_regular"))
    markup.add(telebot.types.InlineKeyboardButton("💻 Кодер", callback_data="mode_coder"))
    markup.add(telebot.types.InlineKeyboardButton("📖 Объяснятор", callback_data="mode_explainer"))
    markup.add(telebot.types.InlineKeyboardButton("🌍 Переводчик", callback_data="mode_translator"))
    markup.add(telebot.types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings_from_chat"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

def back_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

def send_main_menu(chat_id):
    tokens = get_user(chat_id)[0]
    update_user(chat_id, 'state', 'idle')
    text = f"👋 <b>Главное меню</b>\n💰 Токенов: <b>{tokens}</b>\n────────────────\nВыбери действие:"
    sent = bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=main_menu(chat_id))
    remember(chat_id, sent.message_id)

# === /start ===
@bot.message_handler(commands=['start'])
def start(message):
    if message.from_user.username:
        update_user(message.chat.id, 'username', f"@{message.from_user.username}")
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    clear_old_messages(message.chat.id)
    send_main_menu(message.chat.id)

# === НАЗАД ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_main")
def back_to_main(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    send_main_menu(call.message.chat.id)
    bot.answer_callback_query(call.id)

# === КУПИТЬ ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_buy")
def buy_tokens(call):
    _, _, _, trial_started, trial_used, _, _, _, _ = get_user(call.message.chat.id)
    now = int(time.time())
    show_gift = False
    if not trial_used:
        if trial_started == 0:
            update_user(call.message.chat.id, 'trial_started', int(time.time()))
            trial_started = now
        if now - trial_started < TRIAL_WINDOW:
            show_gift = True
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    if show_gift:
        left = TRIAL_WINDOW - (now - trial_started)
        minutes = left // 60
        text = (
            "🎁 <b>ПОДАРОК НА ПЕРВЫЙ РАЗ!</b>\n"
            "────────────────\n"
            f"🎫 <b>2 токена</b> всего за <b>10 ₽</b>\n"
            "🤖 Попробуй чат с ИИ!\n\n"
            f"⏳ Осталось: <b>{minutes} мин</b>\n"
            "────────────────"
        )
        sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=gift_menu())
    else:
        sent = bot.send_message(call.message.chat.id, "💳 <b>Покупка токенов</b>\n────────────────\nВыбери пакет:", parse_mode='HTML', reply_markup=buy_menu(call.message.chat.id))
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === ОТКАЗ ОТ ПОДАРКА ===
@bot.callback_query_handler(func=lambda call: call.data == "decline_gift")
def decline_gift(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    sent = bot.send_message(
        call.message.chat.id,
        "💳 <b>Покупка токенов</b>\n────────────────\nВыбери пакет:",
        parse_mode='HTML',
        reply_markup=buy_menu(call.message.chat.id)
    )
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === ПОДАРОК ===
@bot.callback_query_handler(func=lambda call: call.data == "pack_trial")
def pack_trial(call):
    _, _, _, trial_started, trial_used, _, _, _, _ = get_user(call.message.chat.id)
    if trial_used:
        bot.answer_callback_query(call.id, "❌ Подарок уже использован.")
        return
    if int(time.time()) - trial_started > TRIAL_WINDOW:
        bot.answer_callback_query(call.id, "⏳ Время подарка истекло.")
        return
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    update_user(call.message.chat.id, 'trial_used', 1)
    create_invoice(call.message.chat.id, TRIAL_PRICE, TRIAL_TOKENS)
    bot.answer_callback_query(call.id)

# === ПАКЕТЫ ===
@bot.callback_query_handler(func=lambda call: call.data.startswith("pack_"))
def pack_selected(call):
    parts = call.data.split("_")
    amount = int(parts[1])
    tokens = int(parts[2])
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    create_invoice(call.message.chat.id, amount, tokens)
    bot.answer_callback_query(call.id)

# === СВОЯ СУММА ===
@bot.callback_query_handler(func=lambda call: call.data == "custom_amount")
def custom_amount(call):
    bot.answer_callback_query(call.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    sent = bot.send_message(call.message.chat.id, "✏️ Введи количество токенов (минимум 20):", reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.register_next_step_handler(sent, custom_tokens)

def custom_tokens(message):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    clear_old_messages(message.chat.id)
    try:
        tokens = int(message.text)
        if tokens < 20:
            sent = bot.send_message(message.chat.id, "❌ Минимум 20 токенов.", reply_markup=back_menu())
            remember(message.chat.id, sent.message_id)
            return
        amount = tokens * 5
        create_invoice(message.chat.id, amount, tokens)
    except ValueError:
        sent = bot.send_message(message.chat.id, "❌ Введи число.", reply_markup=back_menu())
        remember(message.chat.id, sent.message_id)

# === СЧЁТ ===
def create_invoice(chat_id, amount, tokens):
    order_id = f"ORD-{chat_id}-{tokens}-{amount}"
    save_order(order_id, chat_id, tokens)
    pay_url = f"https://bot-1790959533-7739-maks746395.bothost.tech/pay/{amount}/{order_id}"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(text=f"💳 Оплатить {amount} ₽", url=pay_url))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    sent = bot.send_message(
        chat_id,
        f"🧾 <b>Счёт:</b> <code>{order_id}</code>\n🎫 <b>Токенов:</b> {tokens}\n💰 <b>Сумма:</b> {amount} ₽\n\nНажми кнопку ниже 👇",
        parse_mode='HTML',
        reply_markup=markup
    )
    remember(chat_id, sent.message_id)

# === БАЛАНС ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_balance")
def show_balance(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    tokens = get_user(call.message.chat.id)[0]
    sent = bot.send_message(call.message.chat.id, f"💰 <b>Ваш баланс:</b> {tokens} токенов", parse_mode='HTML', reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === ИСТОРИЯ ЧАТА ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_history")
def show_history(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    history = get_history(call.message.chat.id, limit=50)
    if not history:
        sent = bot.send_message(call.message.chat.id, "📜 История чата пуста.", reply_markup=back_menu())
        remember(call.message.chat.id, sent.message_id)
        bot.answer_callback_query(call.id)
        return
    text = "📜 <b>Ваша история чата:</b>\n\n"
    for role, content in history:
        prefix = "👤 Вы" if role == "user" else "🤖 Боб"
        short = content[:100] + "..." if len(content) > 100 else content
        safe_short = escape_html(short)
        text += f"{prefix}: {safe_short}\n\n"
    if len(text) > 4000:
        text = text[:4000] + "\n\n...и другие сообщения"
    sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === ПОДДЕРЖКА ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_support")
def support(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📝 Написать тикет", callback_data="ticket_new"))
    markup.add(telebot.types.InlineKeyboardButton("📋 Мои тикеты", callback_data="ticket_my"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    sent = bot.send_message(call.message.chat.id, "🆘 <b>Поддержка</b>\n────────────────\nЧто вы хотите сделать?", parse_mode='HTML', reply_markup=markup)
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "ticket_new")
def ticket_new(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    sent = bot.send_message(call.message.chat.id, "📝 Напишите ваш вопрос (можно прикрепить фото):", reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.register_next_step_handler(sent, ticket_save)

def ticket_save(message):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    user = get_user(message.chat.id)
    username = user[5] if user[5] else f"ID:{message.chat.id}"
    text = message.text if message.text else (message.caption if message.caption else "[без текста]")
    photo_id = message.photo[-1].file_id if message.photo else None
    ticket_id = create_ticket(message.chat.id, username, text, photo_id)
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    sent = bot.send_message(
        message.chat.id,
        f"✅ <b>Тикет #{ticket_id} создан.</b>\nАдминистратор ответит в ближайшее время.",
        parse_mode='HTML',
        reply_markup=back_menu()
    )
    remember(message.chat.id, sent.message_id)
    try:
        if photo_id:
            bot.send_photo(ADMIN_ID, photo_id, caption=f"🔔 Новый тикет #{ticket_id} от {username}\n\n{text}")
        else:
            bot.send_message(ADMIN_ID, f"🔔 Новый тикет #{ticket_id} от {username}\n\n{text}")
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "ticket_my")
def ticket_my(call):
    tickets = get_user_tickets(call.message.chat.id)
    if not tickets:
        bot.answer_callback_query(call.id, "У вас нет тикетов.")
        return
    text = "📋 <b>Ваши тикеты:</b>\n\n"
    for tid, msg, ans, status in tickets:
        st = "✅ отвечен" if status == "closed" else "⏳ ожидает"
        text += f"<b>#{tid}</b> ({st})\n{escape_html(msg[:80])}\n"
        if ans:
            text += f"💬 Ответ: {escape_html(ans[:80])}\n"
        text += "\n"
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=back_menu())
    except Exception:
        sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=back_menu())
        remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === АДМИН-МЕНЮ ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_admin")
def admin_panel(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Доступ запрещён.")
        return
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    sent = bot.send_message(call.message.chat.id, "👑 <b>Админ-меню</b>", parse_mode='HTML', reply_markup=admin_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_tickets")
def admin_tickets(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Доступ запрещён.")
        return
    tickets = get_open_tickets()
    if not tickets:
        try:
            bot.edit_message_text("📋 Нет открытых тикетов.", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=admin_menu())
        except Exception:
            sent = bot.send_message(call.message.chat.id, "📋 Нет открытых тикетов.", reply_markup=admin_menu())
            remember(call.message.chat.id, sent.message_id)
        bot.answer_callback_query(call.id)
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for tid, uid, uname, msg, photo, created in tickets:
        label = f"#{tid} — {uname[:20]} — {msg[:30]}"
        markup.add(telebot.types.InlineKeyboardButton(label, callback_data=f"admin_view_{tid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_admin"))
    try:
        bot.edit_message_text("📋 <b>Открытые тикеты:</b>", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        sent = bot.send_message(call.message.chat.id, "📋 <b>Открытые тикеты:</b>", parse_mode='HTML', reply_markup=markup)
        remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_view_"))
def admin_view_ticket(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Доступ запрещён.")
        return
    ticket_id = int(call.data.replace("admin_view_", ""))
    ticket = get_ticket(ticket_id)
    if not ticket:
        bot.answer_callback_query(call.id, "Тикет не найден.")
        return
    tid, uid, uname, msg, photo, status = ticket
    text = f"📋 <b>Тикет #{tid}</b>\n👤 {uname} (ID: {uid})\n\n💬 {escape_html(msg)}"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✍️ Ответить", callback_data=f"admin_reply_{tid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_tickets"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)
        remember(call.message.chat.id, sent.message_id)
    if photo:
        try:
            bot.send_photo(call.message.chat.id, photo, caption=f"Фото к тикету #{tid}")
        except Exception:
            pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_reply_"))
def admin_reply(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Доступ запрещён.")
        return
    ticket_id = int(call.data.replace("admin_reply_", ""))
    sent = bot.send_message(call.message.chat.id, f"✍️ Напишите ответ на тикет #{ticket_id} (можно фото):", reply_markup=back_menu())
    bot.register_next_step_handler(sent, admin_send_reply, ticket_id)
    bot.answer_callback_query(call.id)

def admin_send_reply(message, ticket_id):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    ticket = get_ticket(ticket_id)
    if not ticket:
        bot.send_message(message.chat.id, "Тикет не найден.")
        return
    _, uid, uname, _, _, _ = ticket
    text = message.text if message.text else (message.caption if message.caption else "")
    photo_id = message.photo[-1].file_id if message.photo else None
    answer_ticket(ticket_id, text)
    try:
        if photo_id:
            bot.send_photo(uid, photo_id, caption=f"💬 <b>Ответ от поддержки</b> (тикет #{ticket_id}):\n\n{text}", parse_mode='HTML')
        else:
            bot.send_message(uid, f"💬 <b>Ответ от поддержки</b> (тикет #{ticket_id}):\n\n{text}", parse_mode='HTML')
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Не удалось отправить: {e}")
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    sent = bot.send_message(message.chat.id, f"✅ Ответ отправлен по тикету #{ticket_id}.", reply_markup=admin_menu())
    remember(message.chat.id, sent.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_give")
def admin_give(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Доступ запрещён.")
        return
    sent = bot.send_message(call.message.chat.id, "💰 Введите ID пользователя и количество токенов через пробел:\nНапример: `8000630493 50`", parse_mode='Markdown', reply_markup=back_menu())
    bot.register_next_step_handler(sent, admin_give_process)
    bot.answer_callback_query(call.id)

def admin_give_process(message):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    try:
        parts = message.text.split()
        uid = int(parts[0])
        cnt = int(parts[1])
        add_tokens(uid, cnt)
        bot.send_message(message.chat.id, f"✅ Начислено {cnt} токенов пользователю {uid}.", reply_markup=admin_menu())
        try:
            bot.send_message(uid, f"🎁 Вам начислено <b>{cnt}</b> токенов!", parse_mode='HTML')
        except Exception:
            pass
    except Exception:
        bot.send_message(message.chat.id, "❌ Неверный формат. Введите: `ID количество`", parse_mode='Markdown', reply_markup=admin_menu())

@bot.callback_query_handler(func=lambda call: call.data == "admin_users")
def admin_users(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Доступ запрещён.")
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id, username, tokens FROM users ORDER BY tokens DESC LIMIT 50")
    rows = c.fetchall()
    conn.close()
    if not rows:
        bot.answer_callback_query(call.id, "Пользователей нет.")
        return
    text = "👥 <b>Пользователи (топ-50):</b>\n\n"
    for uid, uname, tokens in rows:
        text += f"• <code>{uid}</code> — {uname or '—'} — <b>{tokens}</b> токенов\n"
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=admin_menu())
    except Exception:
        sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=admin_menu())
        remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast")
def admin_broadcast(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Доступ запрещён.")
        return
    sent = bot.send_message(call.message.chat.id, "📢 Введите текст для рассылки:", reply_markup=back_menu())
    bot.register_next_step_handler(sent, admin_broadcast_send)
    bot.answer_callback_query(call.id)

def admin_broadcast_send(message):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM users")
    rows = c.fetchall()
    conn.close()
    sent_count = 0
    for (uid,) in rows:
        try:
            bot.send_message(uid, f"📢 <b>Сообщение от администрации:</b>\n\n{escape_html(message.text)}", parse_mode='HTML')
            sent_count += 1
        except Exception:
            pass
    bot.send_message(message.chat.id, f"✅ Рассылка завершена. Отправлено: {sent_count}.", reply_markup=admin_menu())

# === ЧАТ С ИИ ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_chat")
def enter_chat(call):
    tokens, _, mode, _, _, _, _, _, _ = get_user(call.message.chat.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    if tokens < 1:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
        markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
        sent = bot.send_message(call.message.chat.id, "❌ У вас нет токенов.", reply_markup=markup)
        remember(call.message.chat.id, sent.message_id)
        bot.answer_callback_query(call.id)
        return
    update_user(call.message.chat.id, 'state', 'chat')
    mode_name = {"regular": "🤖 Обычный ИИ", "coder": "💻 Кодер", "explainer": "📖 Объяснятор", "translator": "🌍 Переводчик"}.get(mode, "🤖 Обычный ИИ")
    sent = bot.send_message(
        call.message.chat.id,
        f"🤖 <b>Вы в чате с ИИ.</b>\n💰 Баланс: {tokens} токенов.\nРежим: <b>{mode_name}</b>\n\n"
        f"Задайте вопрос или отправьте фото — 1 запрос = 1 токен.\n"
        f"💡 Если хотите сменить режим — напишите «смени режим на кодера».",
        parse_mode='HTML',
        reply_markup=mode_menu()
    )
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("mode_"))
def set_mode(call):
    mode = call.data.replace("mode_", "")
    update_user(call.message.chat.id, 'mode', mode)
    mode_name = {"regular": "🤖 Обычный ИИ", "coder": "💻 Кодер", "explainer": "📖 Объяснятор", "translator": "🌍 Переводчик"}.get(mode, "🤖 Обычный ИИ")
    bot.answer_callback_query(call.id, f"Режим: {mode_name}")
    try:
        bot.edit_message_text(
            f"🤖 <b>Вы в чате с ИИ.</b>\nРежим: <b>{mode_name}</b>\n\nЗадайте вопрос.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML',
            reply_markup=mode_menu()
        )
    except Exception:
        pass

# === GIGACHAT ===
def get_gigachat_token():
    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4()),
        "Authorization": f"Basic {GIGACHAT_AUTH_KEY}"
    }
    data = {"scope": "GIGACHAT_API_PERS"}
    try:
        response = requests.post(url, headers=headers, data=data, verify=False, timeout=30)
        return response.json().get("access_token")
    except Exception as e:
        print(f"Ошибка GigaChat: {e}")
        return None

BASE_PROMPT = (
    "Тебя зовут Боб. Ты — очень умный и полезный ИИ-помощник. "
    "Ты можешь делать ВСЁ, что попросит пользователь: писать код, объяснять, переводить, "
    "сочинять, анализировать, придумывать идеи. "
    "Если тебя спросят 'какая ты модель', 'кто ты' — отвечай: 'Я Боб, твой ИИ-помощник.' "
    "Никогда не упоминай GigaChat, Сбер, OpenAI и другие компании. Ты просто Боб. "
    "Отвечай на том языке, на котором написал пользователь. "
)

SYSTEM_PROMPTS = {
    "regular": BASE_PROMPT + "РЕЖИМ: ОБЫЧНЫЙ ИИ. Отвечай на любые вопросы.",
    "coder": BASE_PROMPT + "РЕЖИМ: КОДЕР. Пиши ТОЛЬКО код. Формат: ```python ... ``` + одно короткое пояснение.",
    "explainer": BASE_PROMPT + "РЕЖИМ: ОБЪЯСНЯТОР. Объясняй сложные вещи простыми словами.",
    "translator": BASE_PROMPT + "РЕЖИМ: ПЕРЕВОДЧИК. Переводи тексты. Русский ↔ английский."
}

def detect_mode_request(text):
    text_lower = text.lower()
    mode_words = {
        "coder": ["кодер", "программист", "code", "coder"],
        "explainer": ["объясн", "учитель", "explain"],
        "translator": ["перевод", "translate", "translator"],
        "regular": ["обычный", "regular"],
    }
    trigger_words = ["переключ", "стань", "режим", "смени", "включи", "сменить"]
    for key, words in mode_words.items():
        if any(w in text_lower for w in words):
            if any(w in text_lower for w in trigger_words):
                return key
    return None

def ask_gigachat(chat_id, question, mode, image_base64=None):
    access_token = get_gigachat_token()
    if not access_token:
        return "❌ Не удалось получить доступ к ИИ."
    url = "https://api.giga.chat/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {access_token}"}
    system_prompt = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["regular"])
    messages = [{"role": "system", "content": system_prompt}]
    history = get_history(chat_id, limit=20)
    for role, content in history:
        messages.append({"role": role, "content": content})
    if image_base64:
        messages.append({"role": "user", "content": [
            {"type": "text", "text": question if question else "Что на этой картинке?"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
        ]})
    else:
        messages.append({"role": "user", "content": question})
    data = {"model": "GigaChat-3-Ultra", "messages": messages, "temperature": 0.7, "max_tokens": 2000}
    try:
        r = requests.post(url, headers=headers, json=data, verify=False, timeout=90)
        result = r.json()
        if "choices" in result:
            answer = result["choices"][0]["message"]["content"]
            add_to_history(chat_id, "user", question if question else "фото")
            add_to_history(chat_id, "assistant", answer)
            return answer
        return f"❌ Ошибка ИИ: {result}"
    except Exception as e:
        return f"❌ Ошибка: {e}"

@bot.message_handler(content_types=['text', 'photo'])
def handle_message(message):
    tokens, state, mode, _, _, _, _, _, _ = get_user(message.chat.id)
    if state != 'chat':
        return
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    if tokens < 1:
        update_user(message.chat.id, 'state', 'idle')
        clear_old_messages(message.chat.id)
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
        markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
        sent = bot.send_message(message.chat.id, "❌ Токены закончились.", reply_markup=markup)
        remember(message.chat.id, sent.message_id)
        return

    prefix = ""
    if message.text:
        new_mode = detect_mode_request(message.text)
        if new_mode:
            update_user(message.chat.id, 'mode', new_mode)
            mode_name = {"regular": "🤖 Обычный ИИ", "coder": "💻 Кодер", "explainer": "📖 Объяснятор", "translator": "🌍 Переводчик"}.get(new_mode, "🤖 Обычный ИИ")
            prefix = f"✅ <b>Сменил режим на {mode_name}.</b>\n\n"
            mode = new_mode
            text = message.text
            for phrase in ["смени режим на ", "переключись на ", "стань ", "сменить режим на ", "включи режим "]:
                if phrase in text.lower():
                    idx = text.lower().find(phrase)
                    rest = text[idx + len(phrase):]
                    parts = rest.split(" ", 1)
                    text = parts[1] if len(parts) > 1 else ""
                    break
            message.text = text if text else "Привет"

    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    add_tokens(message.chat.id, -1)
    log_stat(message.chat.id, 1)
    bot.send_chat_action(message.chat.id, 'typing')

    if message.photo:
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)
        image_base64 = base64.b64encode(downloaded).decode('utf-8')
        caption = message.caption if message.caption else "Что на этой картинке?"
        answer = ask_gigachat(message.chat.id, caption, mode, image_base64=image_base64)
    else:
        answer = ask_gigachat(message.chat.id, message.text, mode)

    tokens_left = get_user(message.chat.id)[0]
    full_answer = prefix + answer

    if "```" in full_answer or "def " in full_answer or "import " in full_answer or "class " in full_answer:
        clean_code = full_answer.replace("```python", "").replace("```", "").strip()
        safe_code = escape_html(clean_code)
        try:
            copy_btn = telebot.types.InlineKeyboardButton(text="📋 Скопировать код", copy_text=clean_code)
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(copy_btn)
            markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
            sent = bot.send_message(message.chat.id, f"<pre><code>{safe_code}</code></pre>\n\n──────────\n💰 Осталось: {tokens_left}", parse_mode='HTML', reply_markup=markup)
        except Exception:
            sent = bot.send_message(message.chat.id, f"{full_answer}\n\n──────────\n💰 Осталось: {tokens_left}", reply_markup=mode_menu())
    else:
        safe_answer = escape_html(full_answer)
        try:
            sent = bot.send_message(message.chat.id, f"{safe_answer}\n\n──────────\n💰 Осталось: {tokens_left}", parse_mode='HTML', reply_markup=mode_menu())
        except Exception:
            sent = bot.send_message(message.chat.id, f"{full_answer}\n\n──────────\n💰 Осталось: {tokens_left}", reply_markup=mode_menu())
    remember(message.chat.id, sent.message_id)

# === СТРАНИЦА ОПЛАТЫ ===
@app.route('/pay/<amount>/<label>')
def pay_page(amount, label):
    html = f'''
    <html>
    <head><meta charset="utf-8"><title>Оплата...</title></head>
    <body onload="document.forms[0].submit()">
        <form method="POST" action="https://yoomoney.ru/quickpay/confirm">
            <input type="hidden" name="receiver" value="{YOOMONEY_RECEIVER}"/>
            <input type="hidden" name="quickpay-form" value="button"/>
            <input type="hidden" name="sum" value="{amount}"/>
            <input type="hidden" name="label" value="{label}"/>
            <input type="hidden" name="paymentType" value="AC"/>
        </form>
    </body>
    </html>
    '''
    return html

# === ВЕБХУК ===
@app.route('/webhook', methods=['POST'])
def yoomoney_webhook():
    data = request.form.to_dict()
    received_sign = data.pop('sign', '')
    if received_sign:
        sorted_items = sorted(data.items())
        check_string = '&'.join(f"{k}={urllib.parse.quote_plus(str(v))}" for k, v in sorted_items)
        calculated_sign = hmac.new(YOOMONEY_SECRET.encode('utf-8'), check_string.encode('utf-8'), sha256).hexdigest()
        if not hmac.compare_digest(calculated_sign, received_sign):
            return jsonify({"status": "error", "message": "Invalid sign"}), 403
    else:
        received_hash = data.pop('sha1_hash', '')
        if received_hash:
            check_string = '&'.join([f"{k}={v}" for k, v in sorted(data.items())]) + YOOMONEY_SECRET
            if hashlib.sha1(check_string.encode('utf-8')).hexdigest() != received_hash:
                return jsonify({"status": "error", "message": "Invalid sha1"}), 403
    label = data.get('label', '')
    amount = data.get('amount', '')
    order = get_order(label)
    if order:
        chat_id, tokens = order
        add_tokens(chat_id, tokens)
        new_balance = get_user(chat_id)[0]
        clear_old_messages(chat_id)
        parts = label.split("-")
        real_amount = parts[-1] if len(parts) >= 4 else amount
        sent = bot.send_message(
            chat_id,
            f"✅ <b>Оплата прошла!</b>\n\n🧾 Счёт: <code>{label}</code>\n💰 Сумма: {real_amount} ₽\n🎫 Токенов: <b>{tokens}</b>\n💎 Баланс: <b>{new_balance}</b>",
            parse_mode='HTML',
            reply_markup=main_menu(chat_id)
        )
        remember(chat_id, sent.message_id)
    return jsonify({"status": "ok"}), 200

# === ЗАПУСК ===
def run_flask():
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 3000)))

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    bot.polling(none_stop=True)
