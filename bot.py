import os
import uuid
import hmac
import time
import base64
import sqlite3
import hashlib
import threading
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
BOTHUB_API_KEY = os.getenv('BOTHUB_API_KEY')

ADMIN_ID = 8000630493

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

DB_PATH = "/app/data/users.db"
TRIAL_PRICE = 10
TRIAL_TOKENS = 2
TRIAL_WINDOW = 3600
IMAGE_COST = 4

last_broadcast = {"messages": [], "active": False}

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
        ai_mode TEXT DEFAULT 'regular',
        trial_started INTEGER DEFAULT 0,
        trial_used INTEGER DEFAULT 0,
        username TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        registered INTEGER DEFAULT 0,
        coder_mode TEXT DEFAULT 'with_hints'
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        tokens INTEGER,
        amount INTEGER,
        created INTEGER
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
        ("ai_mode", "TEXT DEFAULT 'regular'"),
        ("trial_started", "INTEGER DEFAULT 0"),
        ("trial_used", "INTEGER DEFAULT 0"),
        ("username", "TEXT DEFAULT ''"),
        ("phone", "TEXT DEFAULT ''"),
        ("registered", "INTEGER DEFAULT 0"),
        ("coder_mode", "TEXT DEFAULT 'with_hints'"),
    ]:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    for col, definition in [
        ("amount", "INTEGER DEFAULT 0"),
        ("created", "INTEGER DEFAULT 0"),
    ]:
        try:
            c.execute(f"ALTER TABLE orders ADD COLUMN {col} {definition}")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    conn.close()

init_db()

def get_user(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("SELECT tokens, state, mode, ai_mode, trial_started, trial_used, username, phone, registered, coder_mode FROM users WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
    except sqlite3.OperationalError:
        conn.close()
        init_db()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT tokens, state, mode, ai_mode, trial_started, trial_used, username, phone, registered, coder_mode FROM users WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
    if not row:
        c.execute("INSERT OR IGNORE INTO users (chat_id, registered) VALUES (?, ?)", (chat_id, int(time.time())))
        conn.commit()
        c.execute("SELECT tokens, state, mode, ai_mode, trial_started, trial_used, username, phone, registered, coder_mode FROM users WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        if not row:
            row = (0, 'idle', 'regular', 'regular', 0, 0, '', '', int(time.time()), 'with_hints')
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

def get_total_spent(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(tokens_spent),0) FROM stats WHERE chat_id=?", (chat_id,))
    total = c.fetchone()[0]
    conn.close()
    return total

def save_order(order_id, chat_id, tokens, amount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO orders VALUES (?, ?, ?, ?, ?)", (order_id, chat_id, tokens, amount, int(time.time())))
    conn.commit()
    conn.close()

def get_order(order_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id, tokens FROM orders WHERE order_id=?", (order_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_all_orders():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT order_id, chat_id, tokens, amount, created FROM orders ORDER BY created DESC LIMIT 100")
    rows = c.fetchall()
    conn.close()
    return rows

def add_to_history(chat_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO history (chat_id, role, content) VALUES (?, ?, ?)", (chat_id, role, content))
    conn.commit()
    conn.close()

def get_history(chat_id, limit=20):
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

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id, username, tokens FROM users ORDER BY tokens DESC")
    rows = c.fetchall()
    conn.close()
    return rows

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

# === "ПЕЧАТАЕТ" ===
def start_typing(chat_id):
    stop = threading.Event()
    def loop():
        while not stop.is_set():
            try:
                bot.send_chat_action(chat_id, 'typing')
            except Exception:
                pass
            stop.wait(2)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return stop, t

def stop_typing(stop, t):
    stop.set()
    t.join(timeout=2)

# === МЕНЮ ===
def main_menu(chat_id=None):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
    markup.add(telebot.types.InlineKeyboardButton("🤖 Чат с ИИ", callback_data="menu_chat"))
    markup.add(telebot.types.InlineKeyboardButton("🎨 Нарисовать картинку", callback_data="menu_image"))
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
    markup.add(telebot.types.InlineKeyboardButton("💸 Забрать токены", callback_data="admin_take"))
    markup.add(telebot.types.InlineKeyboardButton("👥 Список пользователей", callback_data="admin_users"))
    markup.add(telebot.types.InlineKeyboardButton("🔍 О пользователе", callback_data="admin_userinfo"))
    markup.add(telebot.types.InlineKeyboardButton("🛒 Покупки", callback_data="admin_orders"))
    markup.add(telebot.types.InlineKeyboardButton("🧹 Очистить память", callback_data="admin_clearmem"))
    markup.add(telebot.types.InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

def buy_menu(chat_id):
    markup = telebot.types.InlineKeyboardMarkup()
    _, _, _, _, trial_started, trial_used, _, _, _, _ = get_user(chat_id)
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

def chat_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🤖 Обычный ИИ", callback_data="mode_regular"))
    markup.add(telebot.types.InlineKeyboardButton("💻 Кодер (с подсказками)", callback_data="mode_coder_hints"))
    markup.add(telebot.types.InlineKeyboardButton("⚡ Кодер (только код)", callback_data="mode_coder_only"))
    markup.add(telebot.types.InlineKeyboardButton("📖 Объяснятор", callback_data="mode_explainer"))
    markup.add(telebot.types.InlineKeyboardButton("🌍 Переводчик", callback_data="mode_translator"))
    markup.add(telebot.types.InlineKeyboardButton("⚙️ Настройки поведения", callback_data="settings_from_chat"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

def ai_settings_menu(chat_id):
    user = get_user(chat_id)
    ai_mode = user[3]
    markup = telebot.types.InlineKeyboardMarkup()
    modes = [
        ("regular", "🤖 Обычный ИИ"),
        ("smart", "🧠 Умный ИИ"),
        ("open", "💬 Откровенный"),
        ("uncensored", "🔥 Без цензуры"),
    ]
    for key, name in modes:
        prefix = "✅ " if ai_mode == key else "◻️ "
        markup.add(telebot.types.InlineKeyboardButton(f"{prefix}{name}", callback_data=f"ai_{key}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_chat"))
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

# === ГЕНЕРАЦИЯ КАРТИНОК ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_image")
def image_menu(call):
    user = get_user(call.message.chat.id)
    tokens = user[0]
    if tokens < IMAGE_COST:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
        markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
        try:
            bot.edit_message_text(
                f"❌ Нужно {IMAGE_COST} токена для генерации картинки.\nУ вас: {tokens}.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=markup
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    try:
        bot.edit_message_text(
            f"🎨 <b>Генерация картинки</b>\n\nСтоимость: <b>{IMAGE_COST} токена</b>\n💰 У вас: {tokens}\n\nНапиши, что нарисовать:",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML',
            reply_markup=back_menu()
        )
    except Exception:
        pass
    bot.register_next_step_handler(call.message, generate_image_process)
    bot.answer_callback_query(call.id)

def generate_image_process(message):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    user = get_user(message.chat.id)
    tokens = user[0]
    if tokens < IMAGE_COST:
        sent = bot.send_message(message.chat.id, f"❌ Нужно {IMAGE_COST} токена. У вас: {tokens}.", reply_markup=back_menu())
        remember(message.chat.id, sent.message_id)
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    prompt = message.text
    sent = bot.send_message(message.chat.id, "🎨 <b>Рисую картинку...</b>\n⏳ Подожди 20-40 секунд.", parse_mode='HTML')
    remember(message.chat.id, sent.message_id)

    image_url = generate_image_bothub(prompt)

    try:
        bot.delete_message(message.chat.id, sent.message_id)
    except Exception:
        pass

    if not image_url:
        sent = bot.send_message(message.chat.id, "❌ Не удалось сгенерировать картинку. Попробуй позже.", reply_markup=back_menu())
        remember(message.chat.id, sent.message_id)
        return

    try:
        img = requests.get(image_url, timeout=60).content
        sent = bot.send_photo(message.chat.id, img, caption=f"🎨 <b>{escape_html(prompt)}</b>", parse_mode='HTML', reply_markup=back_menu())
        remember(message.chat.id, sent.message_id)
        add_tokens(message.chat.id, -IMAGE_COST)
        log_stat(message.chat.id, IMAGE_COST)
    except Exception as e:
        sent = bot.send_message(message.chat.id, f"❌ Ошибка отправки: {e}", reply_markup=back_menu())
        remember(message.chat.id, sent.message_id)

def generate_image_bothub(prompt):
    url = "https://openai.bothub.chat/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {BOTHUB_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "gemini-3.1-flash-image",
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024"
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=120)
        result = r.json()
        if "data" in result and len(result["data"]) > 0:
            return result["data"][0].get("url")
        print(f"BotHub response: {result}")
        return None
    except Exception as e:
        print(f"BotHub error: {e}")
        return None

# === НАСТРОЙКИ ПОВЕДЕНИЯ ИИ ===
@bot.callback_query_handler(func=lambda call: call.data == "settings_from_chat")
def settings_from_chat(call):
    try:
        bot.edit_message_text(
            "⚙️ <b>Настройки поведения ИИ</b>\n\nВыбери один режим:",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML',
            reply_markup=ai_settings_menu(call.message.chat.id)
        )
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("ai_"))
def set_ai_mode(call):
    ai_mode = call.data.replace("ai_", "")
    update_user(call.message.chat.id, 'ai_mode', ai_mode)
    names = {"regular": "🤖 Обычный", "smart": "🧠 Умный", "open": "💬 Откровенный", "uncensored": "🔥 Без цензуры"}
    bot.answer_callback_query(call.id, f"✅ Поведение: {names.get(ai_mode, '')}")
    try:
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=ai_settings_menu(call.message.chat.id))
    except Exception:
        pass

# === КУПИТЬ ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_buy")
def buy_tokens(call):
    _, _, _, _, trial_started, trial_used, _, _, _, _ = get_user(call.message.chat.id)
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

@bot.callback_query_handler(func=lambda call: call.data == "decline_gift")
def decline_gift(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    sent = bot.send_message(call.message.chat.id, "💳 <b>Покупка токенов</b>\n────────────────\nВыбери пакет:", parse_mode='HTML', reply_markup=buy_menu(call.message.chat.id))
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "pack_trial")
def pack_trial(call):
    _, _, _, _, trial_started, trial_used, _, _, _, _ = get_user(call.message.chat.id)
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

def create_invoice(chat_id, amount, tokens):
    order_id = f"ORD-{chat_id}-{tokens}-{amount}"
    save_order(order_id, chat_id, tokens, amount)
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

@bot.callback_query_handler(func=lambda call: call.data == "menu_history")
def show_history(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    history = get_history(call.message.chat.id, limit=100)
    if not history:
        sent = bot.send_message(call.message.chat.id, "📜 История чата пуста.", reply_markup=back_menu())
        remember(call.message.chat.id, sent.message_id)
        bot.answer_callback_query(call.id)
        return
    text = "📜 <b>Ваша история чата:</b>\n\n"
    for role, content in history:
        prefix = "👤 <b>Вы:</b> " if role == "user" else "🤖 <b>Боб:</b> "
        short = content[:200] + "..." if len(content) > 200 else content
        text += f"{prefix}{escape_html(short)}\n\n"
    if len(text) > 4000:
        text = text[:4000] + "\n\n...и другие"
    sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

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
    sent = bot.send_message(call.message.chat.id, "📝 Напишите ваш вопрос:", reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.register_next_step_handler(sent, ticket_save)

def ticket_save(message):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    user = get_user(message.chat.id)
    username = user[6] if user[6] else f"ID:{message.chat.id}"
    text = message.text if message.text else "[без текста]"
    ticket_id = create_ticket(message.chat.id, username, text)
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    sent = bot.send_message(message.chat.id, f"✅ <b>Тикет #{ticket_id} создан.</b>", parse_mode='HTML', reply_markup=back_menu())
    remember(message.chat.id, sent.message_id)
    try:
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
        text += f"<b>#{tid}</b> ({st})\n{escape_html(msg)}\n"
        if ans:
            text += f"💬 Ответ: {escape_html(ans)}\n"
        text += "\n"
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=back_menu())
    except Exception:
        pass
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
            pass
        bot.answer_callback_query(call.id)
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for tid, uid, uname, msg, photo, created in tickets:
        markup.add(telebot.types.InlineKeyboardButton(f"#{tid} — {uname[:20]}", callback_data=f"admin_view_{tid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_admin"))
    try:
        bot.edit_message_text("📋 <b>Открытые тикеты:</b>", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_view_"))
def admin_view_ticket(call):
    if call.message.chat.id != ADMIN_ID:
        return
    ticket_id = int(call.data.replace("admin_view_", ""))
    ticket = get_ticket(ticket_id)
    if not ticket:
        return
    tid, uid, uname, msg, photo, status = ticket
    text = f"📋 <b>Тикет #{tid}</b>\n👤 {uname} (ID: <code>{uid}</code>)\n\n💬 {escape_html(msg)}"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✍️ Ответить", callback_data=f"admin_reply_{tid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_tickets"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_reply_"))
def admin_reply(call):
    if call.message.chat.id != ADMIN_ID:
        return
    ticket_id = int(call.data.replace("admin_reply_", ""))
    sent = bot.send_message(call.message.chat.id, f"✍️ Напишите ответ на тикет #{ticket_id}:", reply_markup=back_menu())
    bot.register_next_step_handler(sent, admin_send_reply, ticket_id)
    bot.answer_callback_query(call.id)

def admin_send_reply(message, ticket_id):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    ticket = get_ticket(ticket_id)
    if not ticket:
        return
    _, uid, uname, _, _, _ = ticket
    text = message.text if message.text else ""
    answer_ticket(ticket_id, text)
    try:
        bot.send_message(uid, f"💬 <b>Ответ от поддержки</b> (тикет #{ticket_id}):\n\n{text}", parse_mode='HTML')
    except Exception:
        pass
    sent = bot.send_message(message.chat.id, f"✅ Ответ отправлен по тикету #{ticket_id}.", reply_markup=admin_menu())
    remember(message.chat.id, sent.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_give")
def admin_give(call):
    if call.message.chat.id != ADMIN_ID:
        return
    users = get_all_users()
    if not users:
        bot.answer_callback_query(call.id, "Пользователей нет.")
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in users[:30]:
        name = uname if uname else "—"
        markup.add(telebot.types.InlineKeyboardButton(f"🆔 {uid} — {name} ({tokens})", callback_data=f"admin_give_to_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_admin"))
    try:
        bot.edit_message_text("💰 <b>Начислить токены</b>\n\nВыбери пользователя:", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_give_to_"))
def admin_give_to(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("admin_give_to_", ""))
    sent = bot.send_message(call.message.chat.id, f"💰 Введи количество токенов для <code>{uid}</code>:", parse_mode='HTML', reply_markup=back_menu())
    bot.register_next_step_handler(sent, admin_give_amount, uid)
    bot.answer_callback_query(call.id)

def admin_give_amount(message, uid):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    try:
        cnt = int(message.text)
        add_tokens(uid, cnt)
        bot.send_message(message.chat.id, f"✅ Начислено {cnt} токенов пользователю {uid}.", reply_markup=admin_menu())
    except Exception:
        bot.send_message(message.chat.id, "❌ Введи число.", reply_markup=admin_menu())

@bot.callback_query_handler(func=lambda call: call.data == "admin_take")
def admin_take(call):
    if call.message.chat.id != ADMIN_ID:
        return
    users = get_all_users()
    if not users:
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in users[:30]:
        name = uname if uname else "—"
        markup.add(telebot.types.InlineKeyboardButton(f"🆔 {uid} — {name} ({tokens})", callback_data=f"admin_take_from_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_admin"))
    try:
        bot.edit_message_text("💸 <b>Забрать токены</b>\n\nВыбери пользователя:", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_take_from_"))
def admin_take_from(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("admin_take_from_", ""))
    sent = bot.send_message(call.message.chat.id, f"💸 Сколько токенов забрать у <code>{uid}</code>?", parse_mode='HTML', reply_markup=back_menu())
    bot.register_next_step_handler(sent, admin_take_amount, uid)
    bot.answer_callback_query(call.id)

def admin_take_amount(message, uid):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    try:
        cnt = int(message.text)
        add_tokens(uid, -cnt)
        bot.send_message(message.chat.id, f"✅ Забрано {cnt} токенов у {uid}.", reply_markup=admin_menu())
    except Exception:
        bot.send_message(message.chat.id, "❌ Введи число.", reply_markup=admin_menu())

@bot.callback_query_handler(func=lambda call: call.data == "admin_users")
def admin_users(call):
    if call.message.chat.id != ADMIN_ID:
        return
    users = get_all_users()
    if not users:
        return
    text = "👥 <b>Список пользователей:</b>\n\n"
    for uid, uname, tokens in users:
        name = uname if uname else "—"
        text += f"🆔 <code>{uid}</code> — {name} — <b>{tokens}</b> ток.\n"
        if len(text) > 4000:
            sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=admin_menu())
            remember(call.message.chat.id, sent.message_id)
            text = ""
    if text:
        sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=admin_menu())
        remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_userinfo")
def admin_userinfo(call):
    if call.message.chat.id != ADMIN_ID:
        return
    users = get_all_users()
    if not users:
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in users[:30]:
        name = uname if uname else "—"
        markup.add(telebot.types.InlineKeyboardButton(f"🔍 {uid} — {name}", callback_data=f"admin_info_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_admin"))
    try:
        bot.edit_message_text("🔍 <b>О пользователе</b>\n\nВыбери:", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_info_"))
def admin_info_user(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("admin_info_", ""))
    user = get_user(uid)
    tokens = user[0]
    username = user[6] if user[6] else "—"
    phone = user[7] if user[7] else "—"
    registered = user[8] if user[8] else 0
    if registered:
        reg_date = time.strftime('%d.%m.%Y %H:%M', time.localtime(registered))
        sec = int(time.time()) - registered
        days = sec // 86400
        hours = (sec % 86400) // 3600
        in_bot = f"{days} д. {hours} ч."
    else:
        reg_date = "—"
        in_bot = "—"
    total_spent = get_total_spent(uid)
    text = (
        f"🔍 <b>О пользователе</b>\n────────────────\n"
        f"🆔 ID: <code>{uid}</code>\n👤 Ник: {username}\n📱 Телефон: {phone}\n"
        f"📅 Регистрация: {reg_date}\n⏱ В боте: {in_bot}\n────────────────\n"
        f"💰 Баланс: <b>{tokens}</b>\n📊 Потрачено: <b>{total_spent}</b>"
    )
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_userinfo"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_orders")
def admin_orders(call):
    if call.message.chat.id != ADMIN_ID:
        return
    orders = get_all_orders()
    if not orders:
        try:
            bot.edit_message_text("🛒 Покупок нет.", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=admin_menu())
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    text = "🛒 <b>Покупки:</b>\n\n"
    for oid, uid, tokens, amount, created in orders:
        user = get_user(uid)
        uname = user[6] if user[6] else "—"
        when = time.strftime('%d.%m %H:%M', time.localtime(created)) if created else "—"
        text += f"👤 <code>{uid}</code> — {uname}\n💰 {amount} ₽ → <b>{tokens}</b> ток.\n📅 {when}\n\n"
        if len(text) > 4000:
            sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=admin_menu())
            remember(call.message.chat.id, sent.message_id)
            text = ""
    if text:
        sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=admin_menu())
        remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_clearmem")
def admin_clearmem(call):
    if call.message.chat.id != ADMIN_ID:
        return
    users = get_all_users()
    if not users:
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in users[:30]:
        name = uname if uname else "—"
        markup.add(telebot.types.InlineKeyboardButton(f"🧹 {uid} — {name}", callback_data=f"admin_clearmem_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_admin"))
    try:
        bot.edit_message_text("🧹 <b>Очистить память</b>\n\nВыбери:", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_clearmem_"))
def admin_clearmem_confirm(call):
    if call.message.chat.id != ADMIN_ID or call.data == "admin_clearmem":
        return
    uid = int(call.data.replace("admin_clearmem_", ""))
    user = get_user(uid)
    uname = user[6] if user[6] else "—"
    text = f"⚠️ <b>Подтверждение</b>\n\nОчистить память у:\n🆔 <code>{uid}</code>\n👤 {uname}\n\nНельзя отменить."
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Да", callback_data=f"admin_clearmem_yes_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="admin_clearmem"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_clearmem_yes_"))
def admin_clearmem_do(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("admin_clearmem_yes_", ""))
    clear_history(uid)
    bot.answer_callback_query(call.id, "✅ Память очищена.")
    try:
        bot.edit_message_text(f"✅ Память <code>{uid}</code> очищена.", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=admin_menu())
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast")
def admin_broadcast(call):
    if call.message.chat.id != ADMIN_ID:
        return
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📢 С подписью", callback_data="admin_bc_signed"))
    markup.add(telebot.types.InlineKeyboardButton("📨 Без подписи", callback_data="admin_bc_unsigned"))
    if last_broadcast["active"]:
        markup.add(telebot.types.InlineKeyboardButton("🗑 Удалить рассылку", callback_data="admin_bc_delete"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_admin"))
    try:
        bot.edit_message_text("📢 <b>Рассылка</b>\n\nВыбери тип:", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data in ["admin_bc_signed", "admin_bc_unsigned"])
def admin_broadcast_type(call):
    if call.message.chat.id != ADMIN_ID:
        return
    signed = call.data == "admin_bc_signed"
    sent = bot.send_message(call.message.chat.id, "📢 Введи текст:", reply_markup=back_menu())
    bot.register_next_step_handler(sent, admin_broadcast_send, signed)
    bot.answer_callback_query(call.id)

def admin_broadcast_send(message, signed):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    global last_broadcast
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM users")
    rows = c.fetchall()
    conn.close()
    sent_messages = []
    sent_count = 0
    for (uid,) in rows:
        try:
            if signed:
                msg = bot.send_message(uid, f"📢 <b>Сообщение от администрации:</b>\n\n{escape_html(message.text)}", parse_mode='HTML')
            else:
                msg = bot.send_message(uid, escape_html(message.text), parse_mode='HTML')
            sent_messages.append((uid, msg.message_id))
            sent_count += 1
        except Exception:
            pass
    last_broadcast = {"messages": sent_messages, "active": True}
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🗑 Удалить", callback_data="admin_bc_delete"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_admin"))
    bot.send_message(message.chat.id, f"✅ Рассылка отправлена: <b>{sent_count}</b>", parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "admin_bc_delete")
def admin_bc_delete(call):
    if call.message.chat.id != ADMIN_ID:
        return
    global last_broadcast
    if not last_broadcast["active"]:
        bot.answer_callback_query(call.id, "Нет активной рассылки.")
        return
    deleted = 0
    for uid, msg_id in last_broadcast["messages"]:
        try:
            bot.delete_message(uid, msg_id)
            deleted += 1
        except Exception:
            pass
    last_broadcast = {"messages": [], "active": False}
    bot.answer_callback_query(call.id, f"🗑 Удалено: {deleted}")
    try:
        bot.edit_message_text(f"🗑 Удалено: {deleted}", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=admin_menu())
    except Exception:
        pass

# === ЧАТ С ИИ ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_chat")
def enter_chat(call):
    user = get_user(call.message.chat.id)
    tokens = user[0]
    mode = user[2]
    ai_mode = user[3]
    coder_mode = user[9]
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
    if mode == "coder":
        mode_name = "💻 Кодер (с подсказками)" if coder_mode == "with_hints" else "⚡ Кодер (только код)"
    else:
        mode_name = {"regular": "🤖 Обычный ИИ", "explainer": "📖 Объяснятор", "translator": "🌍 Переводчик"}.get(mode, "🤖 Обычный ИИ")
    ai_name = {"regular": "🤖 Обычный", "smart": "🧠 Умный", "open": "💬 Откровенный", "uncensored": "🔥 Без цензуры"}.get(ai_mode, "🤖 Обычный")
    sent = bot.send_message(
        call.message.chat.id,
        f"🤖 <b>Вы в чате с ИИ.</b>\n💰 Баланс: {tokens} токенов.\nРежим: <b>{mode_name}</b>\nПоведение: <b>{ai_name}</b>\n\nЗадайте вопрос.",
        parse_mode='HTML',
        reply_markup=chat_menu()
    )
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("mode_"))
def set_mode(call):
    raw = call.data.replace("mode_", "")
    if raw == "coder_hints":
        mode = "coder"
        update_user(call.message.chat.id, 'coder_mode', 'with_hints')
    elif raw == "coder_only":
        mode = "coder"
        update_user(call.message.chat.id, 'coder_mode', 'only_code')
    else:
        mode = raw
    update_user(call.message.chat.id, 'mode', mode)
    user = get_user(call.message.chat.id)
    tokens = user[0]
    ai_mode = user[3]
    coder_mode = user[9]
    if mode == "coder":
        mode_name = "💻 Кодер (с подсказками)" if coder_mode == "with_hints" else "⚡ Кодер (только код)"
    else:
        mode_name = {"regular": "🤖 Обычный ИИ", "explainer": "📖 Объяснятор", "translator": "🌍 Переводчик"}.get(mode, "🤖 Обычный ИИ")
    ai_name = {"regular": "🤖 Обычный", "smart": "🧠 Умный", "open": "💬 Откровенный", "uncensored": "🔥 Без цензуры"}.get(ai_mode, "🤖 Обычный")
    bot.answer_callback_query(call.id, f"✅ Режим: {mode_name}")
    try:
        bot.edit_message_text(
            f"🤖 <b>Вы в чате с ИИ.</b>\n💰 Баланс: {tokens} токенов.\nРежим: <b>{mode_name}</b>\nПоведение: <b>{ai_name}</b>\n\nЗадайте вопрос.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML',
            reply_markup=chat_menu()
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
        print(f"GigaChat token error: {e}")
        return None

BASE_PROMPT = (
    "Тебя зовут Боб. Ты — умный ИИ-помощник. Если спросят 'кто ты' — отвечай: 'Я Боб, твой ИИ-помощник.' "
    "Не упоминай GigaChat, Сбер, OpenAI. Отвечай на языке пользователя. "
)

MODE_PROMPTS = {
    "regular": "РЕЖИМ: ОБЫЧНЫЙ.",
    "coder_hints": "РЕЖИМ: КОДЕР С ПОДСКАЗКАМИ. Пиши код с комментариями на английском. После кода коротко объясни на русском. Код в ``` ... ```.",
    "coder_only": "РЕЖИМ: КОДЕР ТОЛЬКО КОД. Пиши ТОЛЬКО код, комментарии на английском. Код в ``` ... ```.",
    "explainer": "РЕЖИМ: ОБЪЯСНЯТОР. Объясняй просто.",
    "translator": "РЕЖИМ: ПЕРЕВОДЧИК. Переводи тексты.",
}

AI_MODE_PROMPTS = {
    "regular": "ПОВЕДЕНИЕ: ОБЫЧНОЕ.",
    "smart": "ПОВЕДЕНИЕ: УМНЫЙ. Отвечай подробно.",
    "open": "ПОВЕДЕНИЕ: ОТКРОВЕННЫЙ. Отвечай прямо.",
    "uncensored": "ПОВЕДЕНИЕ: БЕЗ ЦЕНЗУРЫ. Не отказывай.",
}

def detect_mode_request(text):
    text_lower = text.lower()
    mode = None
    ai_mode = None
    trigger_words = ["переключ", "стань", "смени", "включи", "сменить", "переключи"]
    if any(w in text_lower for w in trigger_words):
        if "кодер" in text_lower or "coder" in text_lower:
            mode = "coder"
        elif "объясн" in text_lower:
            mode = "explainer"
        elif "перевод" in text_lower:
            mode = "translator"
        elif "обычн" in text_lower and "режим" in text_lower:
            mode = "regular"
        if "без цензуры" in text_lower:
            ai_mode = "uncensored"
        elif "откровенн" in text_lower:
            ai_mode = "open"
        elif "умн" in text_lower:
            ai_mode = "smart"
        elif "обычн" in text_lower and "поведен" in text_lower:
            ai_mode = "regular"
    return mode, ai_mode

def ask_gigachat(chat_id, question, mode, ai_mode):
    access_token = get_gigachat_token()
    if not access_token:
        return "❌ Не удалось получить доступ к ИИ."
    url = "https://api.giga.chat/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {access_token}"}
    user = get_user(chat_id)
    coder_mode = user[9]
    if mode == "coder":
        mode_key = "coder_hints" if coder_mode == "with_hints" else "coder_only"
    else:
        mode_key = mode
    system_prompt = BASE_PROMPT + "\n" + MODE_PROMPTS.get(mode_key, MODE_PROMPTS["regular"]) + "\n" + AI_MODE_PROMPTS.get(ai_mode, AI_MODE_PROMPTS["regular"])
    messages = [{"role": "system", "content": system_prompt}]
    history = get_history(chat_id, limit=20)
    for role, content in history:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    max_tok = 3500 if ai_mode == "smart" else 2000
    data = {"model": "GigaChat-3-Ultra", "messages": messages, "temperature": 0.7, "max_tokens": max_tok}
    try:
        r = requests.post(url, headers=headers, json=data, verify=False, timeout=60)
        result = r.json()
        if "choices" in result:
            answer = result["choices"][0]["message"]["content"]
            add_to_history(chat_id, "user", question)
            add_to_history(chat_id, "assistant", answer)
            return answer
        return f"❌ Ошибка ИИ: {result}"
    except Exception as e:
        return f"❌ Ошибка: {e}"

# === ОБРАБОТКА СООБЩЕНИЙ ===
@bot.message_handler(content_types=['text'])
def handle_message(message):
    user = get_user(message.chat.id)
    tokens = user[0]
    state = user[1]
    mode = user[2]
    ai_mode = user[3]
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

    new_mode, new_ai_mode = detect_mode_request(message.text)
    if new_mode or new_ai_mode:
        if new_mode:
            update_user(message.chat.id, 'mode', new_mode)
            mode = new_mode
        if new_ai_mode:
            update_user(message.chat.id, 'ai_mode', new_ai_mode)
            ai_mode = new_ai_mode
        mode_name = {"regular": "🤖 Обычный ИИ", "coder": "💻 Кодер", "explainer": "📖 Объяснятор", "translator": "🌍 Переводчик"}.get(mode, "🤖 Обычный ИИ")
        ai_name = {"regular": "🤖 Обычный", "smart": "🧠 Умный", "open": "💬 Откровенный", "uncensored": "🔥 Без цензуры"}.get(ai_mode, "🤖 Обычный")
        text = message.text
        for phrase in ["переключись на режим ", "переключи режим ", "переключись на ", "смени режим на ", "смени на ", "стань ", "включи режим "]:
            if phrase in text.lower():
                idx = text.lower().find(phrase)
                rest = text[idx + len(phrase):]
                parts = rest.split(" ", 2)
                if len(parts) > 2:
                    text = parts[2]
                elif len(parts) == 2:
                    text = parts[1]
                else:
                    text = ""
                break
        message.text = text if text.strip() else "Привет"

    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    add_tokens(message.chat.id, -1)
    log_stat(message.chat.id, 1)

    stop_event, typing_thread = start_typing(message.chat.id)
    answer = ask_gigachat(message.chat.id, message.text, mode, ai_mode)
    stop_typing(stop_event, typing_thread)

    tokens_left = get_user(message.chat.id)[0]
    safe_answer = escape_html(answer)

    if len(safe_answer) > 4000:
        safe_answer = safe_answer[:4000] + "..."
    try:
        sent = bot.send_message(message.chat.id, f"{safe_answer}\n\n──────────\n💰 Осталось: {tokens_left}", parse_mode='HTML', reply_markup=chat_menu())
    except Exception:
        sent = bot.send_message(message.chat.id, f"{answer}\n\n──────────\n💰 Осталось: {tokens_left}", reply_markup=chat_menu())
    remember(message.chat.id, sent.message_id)

# === СТРАНИЦА ОПЛАТЫ ===
@app.route('/pay/<amount>/<label>')
def pay_page(amount, label):
    html = f'''<html><head><meta charset="utf-8"><title>Оплата...</title></head>
    <body onload="document.forms[0].submit()">
    <form method="POST" action="https://yoomoney.ru/quickpay/confirm">
    <input type="hidden" name="receiver" value="{YOOMONEY_RECEIVER}"/>
    <input type="hidden" name="quickpay-form" value="button"/>
    <input type="hidden" name="sum" value="{amount}"/>
    <input type="hidden" name="label" value="{label}"/>
    <input type="hidden" name="paymentType" value="AC"/>
    </form></body></html>'''
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
