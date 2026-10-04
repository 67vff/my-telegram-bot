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
IMAGE_COST = 4
VIDEO_COST = 20

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
DB_PATH = "/app/data/users.db"

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
    c.execute('''CREATE TABLE IF NOT EXISTS image_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        prompt TEXT,
        image_url TEXT,
        model TEXT,
        created INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS video_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        prompt TEXT,
        video_url TEXT,
        model TEXT,
        created INTEGER
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
    for col, definition in [("amount", "INTEGER DEFAULT 0"), ("created", "INTEGER DEFAULT 0")]:
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

def get_history(chat_id, limit=100):
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

def add_image_history(chat_id, prompt, image_url, model):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO image_history (chat_id, prompt, image_url, model, created) VALUES (?, ?, ?, ?, ?)",
              (chat_id, prompt, image_url, model, int(time.time())))
    conn.commit()
    conn.close()

def get_image_history(chat_id, limit=50):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT prompt, image_url, model, created FROM image_history WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows

def add_video_history(chat_id, prompt, video_url, model):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO video_history (chat_id, prompt, video_url, model, created) VALUES (?, ?, ?, ?, ?)",
              (chat_id, prompt, video_url, model, int(time.time())))
    conn.commit()
    conn.close()

def get_video_history(chat_id, limit=50):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT prompt, video_url, model, created FROM video_history WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id, username, tokens FROM users ORDER BY tokens DESC")
    rows = c.fetchall()
    conn.close()
    return rows

# === ТИКЕТЫ ===
def create_ticket(chat_id, username, message):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO tickets (chat_id, username, message, created) VALUES (?, ?, ?, ?)",
              (chat_id, username, message, int(time.time())))
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

# === МОДЕЛИ ===
IMAGE_MODELS = {
    "gemini-2.5-flash-image": "Gemini 2.5 Flash Image (быстрая)",
    "gpt-image-1": "GPT Image 1 (качественная)",
    "flux-schnell": "Flux Schnell (очень быстрая)",
    "flux-2-max": "Flux 2 Max (профессиональная)",
    "nano-banana": "Nano Banana (новая)",
    "stable-diffusion-xl": "Stable Diffusion XL",
}

VIDEO_MODELS = {
    "veo-3.1": "Veo 3.1 (быстрая)",
    "kling-v2.6": "Kling 2.6 (качественная)",
    "gen4_turbo": "Gen4 Turbo (Runway)",
    "sora-2": "Sora 2 (OpenAI)",
}

# === МЕНЮ ===
def main_menu(chat_id=None):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
    markup.add(telebot.types.InlineKeyboardButton("🤖 Чат с ИИ", callback_data="menu_chat"))
    markup.add(telebot.types.InlineKeyboardButton("✨ Создать", callback_data="menu_create"))
    markup.add(telebot.types.InlineKeyboardButton("📜 История чата ИИ", callback_data="menu_history"))
    markup.add(telebot.types.InlineKeyboardButton("🖼 История фото", callback_data="menu_image_history"))
    markup.add(telebot.types.InlineKeyboardButton("🎬 История видео", callback_data="menu_video_history"))
    markup.add(telebot.types.InlineKeyboardButton("💰 Мой баланс", callback_data="menu_balance"))
    markup.add(telebot.types.InlineKeyboardButton("🆘 Поддержка", callback_data="menu_support"))
    if chat_id == ADMIN_ID:
        markup.add(telebot.types.InlineKeyboardButton("👑 Админ-меню", callback_data="menu_admin"))
    return markup

def create_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🖼 Сгенерировать фото (4 токена)", callback_data="create_image"))
    markup.add(telebot.types.InlineKeyboardButton("🎬 Сгенерировать видео (20 токенов)", callback_data="create_video"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

def image_models_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    for key, name in IMAGE_MODELS.items():
        markup.add(telebot.types.InlineKeyboardButton(name, callback_data=f"imgmodel_{key}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_create"))
    return markup

def video_models_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    for key, name in VIDEO_MODELS.items():
        markup.add(telebot.types.InlineKeyboardButton(name, callback_data=f"vidmodel_{key}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_create"))
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
    if not trial_used and trial_started > 0 and (now - trial_started) < 3600:
        minutes = (3600 - (now - trial_started)) // 60
        markup.add(telebot.types.InlineKeyboardButton(f"🎁 Подарок: 2 токена за 10 ₽ (осталось {minutes} мин)", callback_data="pack_trial"))
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
    modes = [("regular", "🤖 Обычный ИИ"), ("smart", "🧠 Умный ИИ"), ("open", "💬 Откровенный"), ("uncensored", "🔥 Без цензуры")]
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

# === СОЗДАТЬ ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_create")
def create_menu_handler(call):
    tokens = get_user(call.message.chat.id)[0]
    text = (
        f"✨ <b>Создать</b>\n────────────────\n"
        f"💰 Баланс: <b>{tokens}</b>\n\n"
        f"🖼 Фото — <b>{IMAGE_COST}</b> токена\n"
        f"🎬 Видео — <b>{VIDEO_COST}</b> токенов"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=create_menu())
    except Exception:
        sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=create_menu())
        remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === ГЕНЕРАЦИЯ ФОТО ===
@bot.callback_query_handler(func=lambda call: call.data == "create_image")
def create_image(call):
    try:
        bot.edit_message_text("🖼 <b>Выбери модель</b>", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=image_models_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("imgmodel_"))
def image_model_chosen(call):
    model = call.data.replace("imgmodel_", "")
    update_user(call.message.chat.id, 'state', f'image_{model}')
    sent = bot.send_message(call.message.chat.id, f"🖼 Модель: <b>{IMAGE_MODELS.get(model, model)}</b>\n\nНапиши, что нарисовать:", parse_mode='HTML', reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.register_next_step_handler(sent, image_process, model)
    bot.answer_callback_query(call.id)

def image_process(message, model):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    user = get_user(message.chat.id)
    tokens = user[0]
    if tokens < IMAGE_COST:
        sent = bot.send_message(message.chat.id, f"❌ Недостаточно токенов.\nНужно: {IMAGE_COST}, у вас: {tokens}.", reply_markup=back_menu())
        remember(message.chat.id, sent.message_id)
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    prompt = message.text
    sent = bot.send_message(message.chat.id, "🎨 <b>Рисую...</b>\n⏳ 20-40 секунд.", parse_mode='HTML')
    remember(message.chat.id, sent.message_id)
    image_url = generate_image_bothub(prompt, model)
    try:
        bot.delete_message(message.chat.id, sent.message_id)
    except Exception:
        pass
    if not image_url:
        sent = bot.send_message(message.chat.id, "❌ Не удалось сгенерировать.", reply_markup=back_menu())
        remember(message.chat.id, sent.message_id)
        return
    try:
        img = requests.get(image_url, timeout=60).content
        sent = bot.send_photo(message.chat.id, img, caption=f"🖼 <b>{escape_html(prompt)}</b>\n🎨 {model}", parse_mode='HTML', reply_markup=back_menu())
        remember(message.chat.id, sent.message_id)
        add_tokens(message.chat.id, -IMAGE_COST)
        log_stat(message.chat.id, IMAGE_COST)
        add_image_history(message.chat.id, prompt, image_url, model)
    except Exception as e:
        sent = bot.send_message(message.chat.id, f"❌ Ошибка: {e}", reply_markup=back_menu())
        remember(message.chat.id, sent.message_id)

def generate_image_bothub(prompt, model):
    url = "https://openai.bothub.chat/v1/images/generations"
    headers = {"Authorization": f"Bearer {BOTHUB_API_KEY}", "Content-Type": "application/json"}
    data = {"model": model, "prompt": prompt, "response_format": "url"}
    try:
        r = requests.post(url, headers=headers, json=data, timeout=120)
        result = r.json()
        if "data" in result and len(result["data"]) > 0:
            return result["data"][0].get("url")
        print(f"BotHub image response: {result}")
        return None
    except Exception as e:
        print(f"BotHub image error: {e}")
        return None

# === ГЕНЕРАЦИЯ ВИДЕО ===
@bot.callback_query_handler(func=lambda call: call.data == "create_video")
def create_video(call):
    try:
        bot.edit_message_text("🎬 <b>Выбери модель</b>", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=video_models_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("vidmodel_"))
def video_model_chosen(call):
    model = call.data.replace("vidmodel_", "")
    update_user(call.message.chat.id, 'state', f'video_{model}')
    sent = bot.send_message(call.message.chat.id, f"🎬 Модель: <b>{VIDEO_MODELS.get(model, model)}</b>\n\nНапиши, что показать в видео:", parse_mode='HTML', reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.register_next_step_handler(sent, video_process, model)
    bot.answer_callback_query(call.id)

def video_process(message, model):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    user = get_user(message.chat.id)
    tokens = user[0]
    if tokens < VIDEO_COST:
        sent = bot.send_message(message.chat.id, f"❌ Недостаточно токенов.\nНужно: {VIDEO_COST}, у вас: {tokens}.", reply_markup=back_menu())
        remember(message.chat.id, sent.message_id)
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    prompt = message.text
    sent = bot.send_message(message.chat.id, "🎬 <b>Генерирую видео...</b>\n⏳ Это займёт 1-3 минуты.", parse_mode='HTML')
    remember(message.chat.id, sent.message_id)
    threading.Thread(target=video_thread, args=(message.chat.id, prompt, model, sent.message_id), daemon=True).start()

def video_thread(chat_id, prompt, model, loading_msg_id):
    video_url = generate_video_bothub(prompt, model)
    try:
        bot.delete_message(chat_id, loading_msg_id)
    except Exception:
        pass
    if not video_url:
        sent = bot.send_message(chat_id, "❌ Не удалось сгенерировать видео.", reply_markup=back_menu())
        remember(chat_id, sent.message_id)
        return
    try:
        vid = requests.get(video_url, timeout=120).content
        sent = bot.send_video(chat_id, vid, caption=f"🎬 <b>{escape_html(prompt)}</b>\n🎥 {model}", parse_mode='HTML', reply_markup=back_menu())
        remember(chat_id, sent.message_id)
        add_tokens(chat_id, -VIDEO_COST)
        log_stat(chat_id, VIDEO_COST)
        add_video_history(chat_id, prompt, video_url, model)
    except Exception as e:
        sent = bot.send_message(chat_id, f"❌ Ошибка: {e}", reply_markup=back_menu())
        remember(chat_id, sent.message_id)

def generate_video_bothub(prompt, model):
    base_url = "https://openai.bothub.chat/v1"
    headers = {"Authorization": f"Bearer {BOTHUB_API_KEY}", "Content-Type": "application/json"}
    create_data = {
        "model": model,
        "prompt": prompt,
        "notify": False,
        "settings": {"duration_seconds": 6, "aspect_ratio": "16:9"}
    }
    try:
        r = requests.post(f"{base_url}/videos/generations", headers=headers, json=create_data, timeout=30)
        result = r.json()
        video_id = result.get("id")
        if not video_id:
            print(f"Create video error: {result}")
            return None
        for _ in range(40):
            time.sleep(10)
            s = requests.get(f"{base_url}/videos/{video_id}", headers=headers, timeout=30)
            status = s.json()
            if status.get("status") == "completed":
                urls = status.get("unsigned_urls", [])
                return urls[0] if urls else None
            if status.get("status") == "failed":
                print(f"Video failed: {status.get('error')}")
                return None
    except Exception as e:
        print(f"Video error: {e}")
    return None

# === ИСТОРИЯ ЧАТА ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_history")
def show_history(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    history = get_history(call.message.chat.id, limit=100)
    if not history:
        sent = bot.send_message(call.message.chat.id, "📜 История чата пуста.", reply_markup=back_menu())
        remember(call.message.chat.id, sent.message_id)
        bot.answer_callback_query(call.id)
        return
    text = "📜 <b>История чата с ИИ:</b>\n\n"
    for role, content in history:
        prefix = "👤 <b>Вы:</b> " if role == "user" else "🤖 <b>Боб:</b> "
        short = content[:200] + "..." if len(content) > 200 else content
        text += f"{prefix}{escape_html(short)}\n\n"
    if len(text) > 4000:
        text = text[:4000] + "\n\n...и другие"
    sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === ИСТОРИЯ ФОТО ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_image_history")
def show_image_history(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    history = get_image_history(call.message.chat.id, limit=50)
    if not history:
        sent = bot.send_message(call.message.chat.id, "🖼 История фото пуста.", reply_markup=back_menu())
        remember(call.message.chat.id, sent.message_id)
        bot.answer_callback_query(call.id)
        return
    text = "🖼 <b>История генерации фото:</b>\n\n"
    for prompt, url, model, created in history:
        when = time.strftime('%d.%m %H:%M', time.localtime(created)) if created else ""
        text += f"<b>{escape_html(prompt[:80])}</b>\n🎨 {model}\n📅 {when}\n🔗 <a href='{url}'>Открыть</a>\n\n"
    if len(text) > 4000:
        text = text[:4000] + "\n\n...и другие"
    sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=back_menu(), disable_web_page_preview=True)
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === ИСТОРИЯ ВИДЕО ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_video_history")
def show_video_history(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    history = get_video_history(call.message.chat.id, limit=50)
    if not history:
        sent = bot.send_message(call.message.chat.id, "🎬 История видео пуста.", reply_markup=back_menu())
        remember(call.message.chat.id, sent.message_id)
        bot.answer_callback_query(call.id)
        return
    text = "🎬 <b>История генерации видео:</b>\n\n"
    for prompt, url, model, created in history:
        when = time.strftime('%d.%m %H:%M', time.localtime(created)) if created else ""
        text += f"<b>{escape_html(prompt[:80])}</b>\n🎥 {model}\n📅 {when}\n🔗 <a href='{url}'>Открыть</a>\n\n"
    if len(text) > 4000:
        text = text[:4000] + "\n\n...и другие"
    sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=back_menu(), disable_web_page_preview=True)
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === НАСТРОЙКИ ИИ ===
@bot.callback_query_handler(func=lambda call: call.data == "settings_from_chat")
def settings_from_chat(call):
    try:
        bot.edit_message_text("⚙️ <b>Настройки поведения ИИ</b>", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=ai_settings_menu(call.message.chat.id))
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("ai_"))
def set_ai_mode(call):
    ai_mode = call.data.replace("ai_", "")
    update_user(call.message.chat.id, 'ai_mode', ai_mode)
    names = {"regular": "Обычный", "smart": "Умный", "open": "Откровенный", "uncensored": "Без цензуры"}
    bot.answer_callback_query(call.id, f"✅ {names.get(ai_mode, '')}")
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
        if now - trial_started < 3600:
            show_gift = True
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    if show_gift:
        minutes = (3600 - (now - trial_started)) // 60
        text = f"🎁 <b>ПОДАРОК!</b>\n2 токена за 10 ₽\n⏳ {minutes} мин"
        sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=gift_menu())
    else:
        sent = bot.send_message(call.message.chat.id, "💳 <b>Покупка токенов</b>", parse_mode='HTML', reply_markup=buy_menu(call.message.chat.id))
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "decline_gift")
def decline_gift(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    sent = bot.send_message(call.message.chat.id, "💳 <b>Покупка токенов</b>", parse_mode='HTML', reply_markup=buy_menu(call.message.chat.id))
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "pack_trial")
def pack_trial(call):
    _, _, _, _, trial_started, trial_used, _, _, _, _ = get_user(call.message.chat.id)
    if trial_used or int(time.time()) - trial_started > 3600:
        bot.answer_callback_query(call.id, "❌ Недоступно.")
        return
    update_user(call.message.chat.id, 'trial_used', 1)
    create_invoice(call.message.chat.id, 10, 2)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pack_"))
def pack_selected(call):
    parts = call.data.split("_")
    amount = int(parts[1])
    tokens = int(parts[2])
    create_invoice(call.message.chat.id, amount, tokens)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "custom_amount")
def custom_amount(call):
    bot.answer_callback_query(call.id)
    sent = bot.send_message(call.message.chat.id, "✏️ Введи количество токенов (минимум 20):", reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.register_next_step_handler(sent, custom_tokens)

def custom_tokens(message):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    try:
        tokens = int(message.text)
        if tokens < 20:
            sent = bot.send_message(message.chat.id, "❌ Минимум 20 токенов.", reply_markup=back_menu())
            remember(message.chat.id, sent.message_id)
            return
        create_invoice(message.chat.id, tokens * 5, tokens)
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
    sent = bot.send_message(chat_id, f"🧾 <b>Счёт:</b> <code>{order_id}</code>\n💰 <b>Сумма:</b> {amount} ₽\n🎫 <b>Токенов:</b> {tokens}", parse_mode='HTML', reply_markup=markup)
    remember(chat_id, sent.message_id)

# === БАЛАНС ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_balance")
def show_balance(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    tokens = get_user(call.message.chat.id)[0]
    sent = bot.send_message(call.message.chat.id, f"💰 <b>Баланс:</b> {tokens} токенов", parse_mode='HTML', reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === ПОДДЕРЖКА ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_support")
def support(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📝 Написать тикет", callback_data="ticket_new"))
    markup.add(telebot.types.InlineKeyboardButton("📋 Мои тикеты", callback_data="ticket_my"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    sent = bot.send_message(call.message.chat.id, "🆘 <b>Поддержка</b>", parse_mode='HTML', reply_markup=markup)
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "ticket_new")
def ticket_new(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    sent = bot.send_message(call.message.chat.id, "📝 Напишите ваш вопрос:", reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.register_next_step_handler(sent, ticket_save)

def ticket_save(message):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    user = get_user(message.chat.id)
    username = user[6] if user[6] else f"ID:{message.chat.id}"
    ticket_id = create_ticket(message.chat.id, username, message.text)
    sent = bot.send_message(message.chat.id, f"✅ Тикет #{ticket_id} создан.", reply_markup=back_menu())
    remember(message.chat.id, sent.message_id)
    try:
        bot.send_message(ADMIN_ID, f"🔔 Тикет #{ticket_id} от {username}\n\n{message.text}")
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
        st = "✅" if status == "closed" else "⏳"
        text += f"{st} <b>#{tid}</b> {escape_html(msg[:80])}\n"
        if ans:
            text += f"💬 {escape_html(ans[:80])}\n"
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=back_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)

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
    if tokens < 1:
        sent = bot.send_message(call.message.chat.id, "❌ Нет токенов.", reply_markup=back_menu())
        remember(call.message.chat.id, sent.message_id)
        bot.answer_callback_query(call.id)
        return
    update_user(call.message.chat.id, 'state', 'chat')
    if mode == "coder":
        mode_name = "💻 Кодер" if coder_mode == "with_hints" else "⚡ Кодер"
    else:
        mode_name = {"regular": "🤖 Обычный ИИ", "explainer": "📖 Объяснятор", "translator": "🌍 Переводчик"}.get(mode, "🤖 Обычный ИИ")
    ai_name = {"regular": "🤖 Обычный", "smart": "🧠 Умный", "open": "💬 Откровенный", "uncensored": "🔥 Без цензуры"}.get(ai_mode, "🤖 Обычный")
    sent = bot.send_message(call.message.chat.id, f"🤖 <b>Чат с ИИ</b>\n💰 {tokens}\nРежим: <b>{mode_name}</b>\nПоведение: <b>{ai_name}</b>", parse_mode='HTML', reply_markup=chat_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("mode_"))
def set_mode(call):
    raw = call.data.replace("mode_", "")
    if raw == "coder_hints":
        mode, coder = "coder", "with_hints"
    elif raw == "coder_only":
        mode, coder = "coder", "only_code"
    else:
        mode, coder = raw, None
    update_user(call.message.chat.id, 'mode', mode)
    if coder:
        update_user(call.message.chat.id, 'coder_mode', coder)
    bot.answer_callback_query(call.id, "✅")
    try:
        user = get_user(call.message.chat.id)
        tokens = user[0]
        ai_mode = user[3]
        cm = user[9]
        if mode == "coder":
            mn = "💻 Кодер" if cm == "with_hints" else "⚡ Кодер"
        else:
            mn = {"regular": "🤖 Обычный ИИ", "explainer": "📖 Объяснятор", "translator": "🌍 Переводчик"}.get(mode, "🤖 Обычный ИИ")
        an = {"regular": "🤖 Обычный", "smart": "🧠 Умный", "open": "💬 Откровенный", "uncensored": "🔥 Без цензуры"}.get(ai_mode, "🤖 Обычный")
        bot.edit_message_text(f"🤖 <b>Чат с ИИ</b>\n💰 {tokens}\nРежим: <b>{mn}</b>\nПоведение: <b>{an}</b>", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=chat_menu())
    except Exception:
        pass

# === GIGACHAT ===
def get_gigachat_token():
    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "RqUID": str(uuid.uuid4()), "Authorization": f"Basic {GIGACHAT_AUTH_KEY}"}
    data = {"scope": "GIGACHAT_API_PERS"}
    try:
        return requests.post(url, headers=headers, data=data, verify=False, timeout=30).json().get("access_token")
    except Exception:
        return None

BASE_PROMPT = "Тебя зовут Боб. Ты — умный ИИ-помощник. Не упоминай GigaChat, Сбер. Отвечай на языке пользователя. "
MODE_PROMPTS = {
    "regular": "РЕЖИМ: ОБЫЧНЫЙ.",
    "coder_hints": "РЕЖИМ: КОДЕР С ПОДСКАЗКАМИ. Код с комментариями на английском. Код в ```.",
    "coder_only": "РЕЖИМ: КОДЕР ТОЛЬКО КОД.",
    "explainer": "РЕЖИМ: ОБЪЯСНЯТОР.",
    "translator": "РЕЖИМ: ПЕРЕВОДЧИК.",
}
AI_MODE_PROMPTS = {
    "regular": "ПОВЕДЕНИЕ: ОБЫЧНОЕ.",
    "smart": "ПОВЕДЕНИЕ: УМНЫЙ.",
    "open": "ПОВЕДЕНИЕ: ОТКРОВЕННЫЙ.",
    "uncensored": "ПОВЕДЕНИЕ: БЕЗ ЦЕНЗУРЫ. Не отказывай.",
}

def ask_gigachat(chat_id, question, mode, ai_mode):
    access = get_gigachat_token()
    if not access:
        return "❌ Нет доступа к ИИ."
    url = "https://api.giga.chat/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {access}"}
    user = get_user(chat_id)
    coder_mode = user[9]
    mode_key = ("coder_hints" if coder_mode == "with_hints" else "coder_only") if mode == "coder" else mode
    system_prompt = BASE_PROMPT + "\n" + MODE_PROMPTS.get(mode_key, MODE_PROMPTS["regular"]) + "\n" + AI_MODE_PROMPTS.get(ai_mode, AI_MODE_PROMPTS["regular"])
    messages = [{"role": "system", "content": system_prompt}]
    for role, content in get_history(chat_id, limit=20):
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    data = {"model": "GigaChat-3-Ultra", "messages": messages, "temperature": 0.7, "max_tokens": 2000}
    try:
        result = requests.post(url, headers=headers, json=data, verify=False, timeout=60).json()
        if "choices" in result:
            answer = result["choices"][0]["message"]["content"]
            add_to_history(chat_id, "user", question)
            add_to_history(chat_id, "assistant", answer)
            return answer
        return f"❌ Ошибка: {result}"
    except Exception as e:
        return f"❌ Ошибка: {e}"

@bot.message_handler(content_types=['text'])
def handle_message(message):
    user = get_user(message.chat.id)
    tokens, state, mode, ai_mode = user[0], user[1], user[2], user[3]
    if state != 'chat':
        return
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    if tokens < 1:
        update_user(message.chat.id, 'state', 'idle')
        sent = bot.send_message(message.chat.id, "❌ Токены закончились.", reply_markup=back_menu())
        remember(message.chat.id, sent.message_id)
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    add_tokens(message.chat.id, -1)
    log_stat(message.chat.id, 1)
    stop_event, t = start_typing(message.chat.id)
    answer = ask_gigachat(message.chat.id, message.text, mode, ai_mode)
    stop_typing(stop_event, t)
    tokens_left = get_user(message.chat.id)[0]
    safe = escape_html(answer)[:4000]
    sent = bot.send_message(message.chat.id, f"{safe}\n\n──────────\n💰 Осталось: {tokens_left}", parse_mode='HTML', reply_markup=chat_menu())
    remember(message.chat.id, sent.message_id)

# === АДМИН ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_admin")
def admin_panel(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌")
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
        return
    tickets = get_open_tickets()
    if not tickets:
        bot.answer_callback_query(call.id, "Нет тикетов.")
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for tid, uid, uname, msg, photo, created in tickets:
        markup.add(telebot.types.InlineKeyboardButton(f"#{tid} — {uname[:20]}", callback_data=f"admin_view_{tid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️", callback_data="menu_admin"))
    try:
        bot.edit_message_text("📋 Тикеты:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_view_"))
def admin_view_ticket(call):
    if call.message.chat.id != ADMIN_ID:
        return
    tid = int(call.data.replace("admin_view_", ""))
    ticket = get_ticket(tid)
    if not ticket:
        return
    _, uid, uname, msg, photo, status = ticket
    text = f"📋 #{tid}\n👤 {uname} (ID: <code>{uid}</code>)\n\n💬 {escape_html(msg)}"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✍️ Ответить", callback_data=f"admin_reply_{tid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️", callback_data="admin_tickets"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_reply_"))
def admin_reply(call):
    if call.message.chat.id != ADMIN_ID:
        return
    tid = int(call.data.replace("admin_reply_", ""))
    sent = bot.send_message(call.message.chat.id, f"✍️ Ответ на #{tid}:", reply_markup=back_menu())
    bot.register_next_step_handler(sent, admin_send_reply, tid)
    bot.answer_callback_query(call.id)

def admin_send_reply(message, tid):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    ticket = get_ticket(tid)
    if not ticket:
        return
    _, uid, _, _, _, _ = ticket
    answer_ticket(tid, message.text)
    try:
        bot.send_message(uid, f"💬 Ответ (тикет #{tid}):\n\n{message.text}")
    except Exception:
        pass
    sent = bot.send_message(message.chat.id, "✅ Отправлено.", reply_markup=admin_menu())
    remember(message.chat.id, sent.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_give")
def admin_give(call):
    if call.message.chat.id != ADMIN_ID:
        return
    users = get_all_users()
    if not users:
        bot.answer_callback_query(call.id, "Нет польз.")
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in users[:30]:
        markup.add(telebot.types.InlineKeyboardButton(f"🆔 {uid} — {uname or '—'} ({tokens})", callback_data=f"admin_give_to_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️", callback_data="menu_admin"))
    try:
        bot.edit_message_text("💰 Начислить:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_give_to_"))
def admin_give_to(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("admin_give_to_", ""))
    sent = bot.send_message(call.message.chat.id, f"💰 Кол-во для <code>{uid}</code>:", parse_mode='HTML', reply_markup=back_menu())
    bot.register_next_step_handler(sent, admin_give_amount, uid)
    bot.answer_callback_query(call.id)

def admin_give_amount(message, uid):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    try:
        cnt = int(message.text)
        add_tokens(uid, cnt)
        sent = bot.send_message(message.chat.id, f"✅ +{cnt} токенов {uid}.", reply_markup=admin_menu())
        remember(message.chat.id, sent.message_id)
    except Exception:
        sent = bot.send_message(message.chat.id, "❌ Число.", reply_markup=admin_menu())
        remember(message.chat.id, sent.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_take")
def admin_take(call):
    if call.message.chat.id != ADMIN_ID:
        return
    users = get_all_users()
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in users[:30]:
        markup.add(telebot.types.InlineKeyboardButton(f"🆔 {uid} — {uname or '—'} ({tokens})", callback_data=f"admin_take_from_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️", callback_data="menu_admin"))
    try:
        bot.edit_message_text("💸 Забрать:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_take_from_"))
def admin_take_from(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("admin_take_from_", ""))
    sent = bot.send_message(call.message.chat.id, f"💸 Кол-во у <code>{uid}</code>:", parse_mode='HTML', reply_markup=back_menu())
    bot.register_next_step_handler(sent, admin_take_amount, uid)
    bot.answer_callback_query(call.id)

def admin_take_amount(message, uid):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    try:
        cnt = int(message.text)
        add_tokens(uid, -cnt)
        sent = bot.send_message(message.chat.id, f"✅ -{cnt} у {uid}.", reply_markup=admin_menu())
        remember(message.chat.id, sent.message_id)
    except Exception:
        sent = bot.send_message(message.chat.id, "❌ Число.", reply_markup=admin_menu())
        remember(message.chat.id, sent.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_users")
def admin_users(call):
    if call.message.chat.id != ADMIN_ID:
        return
    users = get_all_users()
    text = "👥 Пользователи:\n\n"
    for uid, uname, tokens in users:
        text += f"🆔 <code>{uid}</code> — {uname or '—'} — <b>{tokens}</b>\n"
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
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in users[:30]:
        markup.add(telebot.types.InlineKeyboardButton(f"🔍 {uid} — {uname or '—'}", callback_data=f"admin_info_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️", callback_data="menu_admin"))
    try:
        bot.edit_message_text("🔍 Выбери:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
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
    username = user[6] or "—"
    phone = user[7] or "—"
    registered = user[8] or 0
    if registered:
        reg_date = time.strftime('%d.%m.%Y %H:%M', time.localtime(registered))
        sec = int(time.time()) - registered
        in_bot = f"{sec // 86400} д. {(sec % 86400) // 3600} ч."
    else:
        reg_date = "—"
        in_bot = "—"
    total_spent = get_total_spent(uid)
    text = (f"🔍 <b>О пользователе</b>\n────────────────\n"
            f"🆔 ID: <code>{uid}</code>\n👤 Ник: {username}\n📱 Телефон: {phone}\n"
            f"📅 Регистрация: {reg_date}\n⏱ В боте: {in_bot}\n────────────────\n"
            f"💰 Баланс: <b>{tokens}</b>\n📊 Потрачено: <b>{total_spent}</b>")
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️", callback_data="admin_userinfo"))
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
        bot.answer_callback_query(call.id, "Нет покупок.")
        return
    text = "🛒 Покупки:\n\n"
    for oid, uid, tokens, amount, created in orders:
        user = get_user(uid)
        uname = user[6] or "—"
        when = time.strftime('%d.%m %H:%M', time.localtime(created)) if created else ""
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
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in users[:30]:
        markup.add(telebot.types.InlineKeyboardButton(f"🧹 {uid} — {uname or '—'}", callback_data=f"admin_clearmem_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️", callback_data="menu_admin"))
    try:
        bot.edit_message_text("🧹 Очистить память:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_clearmem_"))
def admin_clearmem_confirm(call):
    if call.message.chat.id != ADMIN_ID or call.data == "admin_clearmem":
        return
    uid = int(call.data.replace("admin_clearmem_", ""))
    user = get_user(uid)
    text = f"⚠️ Очистить память у:\n🆔 <code>{uid}</code>\n👤 {user[6] or '—'}"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Да", callback_data=f"admin_clearmem_yes_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Нет", callback_data="admin_clearmem"))
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
    bot.answer_callback_query(call.id, "✅")
    try:
        bot.edit_message_text(f"✅ Память {uid} очищена.", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=admin_menu())
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast")
def admin_broadcast(call):
    if call.message.chat.id != ADMIN_ID:
        return
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📢 С подписью", callback_data="admin_bc_signed"))
    markup.add(telebot.types.InlineKeyboardButton("📨 Без", callback_data="admin_bc_unsigned"))
    if last_broadcast["active"]:
        markup.add(telebot.types.InlineKeyboardButton("🗑 Удалить", callback_data="admin_bc_delete"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️", callback_data="menu_admin"))
    try:
        bot.edit_message_text("📢 Рассылка:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data in ["admin_bc_signed", "admin_bc_unsigned"])
def admin_broadcast_type(call):
    if call.message.chat.id != ADMIN_ID:
        return
    signed = call.data == "admin_bc_signed"
    sent = bot.send_message(call.message.chat.id, "📢 Текст:", reply_markup=back_menu())
    bot.register_next_step_handler(sent, admin_broadcast_send, signed)
    bot.answer_callback_query(call.id)

def admin_broadcast_send(message, signed):
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    text = message.text
    users = get_all_users()
    last_broadcast["messages"] = []
    last_broadcast["active"] = True
    sent_count = 0
    for uid, uname, tokens in users:
        try:
            if signed:
                txt = f"📢 <b>Сообщение от админа</b>\n\n{escape_html(text)}"
            else:
                txt = escape_html(text)
            msg = bot.send_message(uid, txt, parse_mode='HTML')
            last_broadcast["messages"].append((uid, msg.message_id))
            sent_count += 1
            time.sleep(0.05)
        except Exception:
            pass
    sent = bot.send_message(message.chat.id, f"✅ Отправлено {sent_count} пользователям.", reply_markup=admin_menu())
    remember(message.chat.id, sent.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_bc_delete")
def admin_bc_delete(call):
    if call.message.chat.id != ADMIN_ID:
        return
    count = 0
    for uid, mid in last_broadcast["messages"]:
        try:
            bot.delete_message(uid, mid)
            count += 1
        except Exception:
            pass
    last_broadcast["messages"] = []
    last_broadcast["active"] = False
    bot.answer_callback_query(call.id, f"🗑 Удалено {count}")
    try:
        bot.edit_message_text("🗑 Рассылка удалена.", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=admin_menu())
    except Exception:
        pass

# === FLASK ДЛЯ ПЛАТЕЖЕЙ ===
@app.route('/pay/<int:amount>/<order_id>', methods=['GET'])
def pay_page(amount, order_id):
    order = get_order(order_id)
    if not order:
        return "Заказ не найден", 404
    html = f"""
    <html><head><meta charset="utf-8"><title>Оплата</title></head>
    <body style="font-family:sans-serif;text-align:center;padding:50px;">
    <h1>Оплата {amount} ₽</h1>
    <p>Заказ: {order_id}</p>
    <form method="POST" action="/yoomoney/notify">
    <input type="hidden" name="order_id" value="{order_id}">
    <button type="submit" style="padding:15px 30px;font-size:18px;">Оплатить</button>
    </form>
    </body></html>
    """
    return html

@app.route('/yoomoney/notify', methods=['POST'])
def yoomoney_notify():
    data = request.form.to_dict()
    order_id = data.get('order_id')
    if not order_id:
        return "OK", 200
    order = get_order(order_id)
    if order:
        chat_id, tokens = order
        add_tokens(chat_id, tokens)
        try:
            bot.send_message(chat_id, f"✅ Оплата получена!\n🎫 +{tokens} токенов")
        except Exception:
            pass
    return "OK", 200

# === ЗАПУСК ===
def run_flask():
    app.run(host='0.0.0.0', port=5000)

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    bot.infinity_polling()
