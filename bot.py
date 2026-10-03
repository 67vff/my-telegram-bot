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
        tokens INTEGER DEFAULT 1000,
        state TEXT DEFAULT 'idle',
        mode TEXT DEFAULT 'coder',
        trial_started INTEGER DEFAULT 0,
        trial_used INTEGER DEFAULT 0
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
    conn.commit()
    for col, definition in [
        ("mode", "TEXT DEFAULT 'coder'"),
        ("trial_started", "INTEGER DEFAULT 0"),
        ("trial_used", "INTEGER DEFAULT 0"),
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
        c.execute("SELECT tokens, state, mode, trial_started, trial_used FROM users WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
    except sqlite3.OperationalError:
        conn.close()
        init_db()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT tokens, state, mode, trial_started, trial_used FROM users WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (chat_id, tokens, state, mode, trial_started, trial_used) VALUES (?, 1000, 'idle', 'coder', 0, 0)", (chat_id,))
        conn.commit()
        row = (1000, 'idle', 'coder', 0, 0)
    conn.close()
    return row

def update_state(chat_id, state):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET state=? WHERE chat_id=?", (state, chat_id))
    conn.commit()
    conn.close()

def update_mode(chat_id, mode):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET mode=? WHERE chat_id=?", (mode, chat_id))
    conn.commit()
    conn.close()

def set_trial_started(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET trial_started=? WHERE chat_id=?", (int(time.time()), chat_id))
    conn.commit()
    conn.close()

def set_trial_used(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET trial_used=1 WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

def add_tokens(chat_id, count):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET tokens = tokens + ? WHERE chat_id=?", (count, chat_id))
    conn.commit()
    conn.close()

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

def get_history(chat_id, limit=20):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit))
    rows = c.fetchall()
    conn.close()
    return list(reversed(rows))

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
def main_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
    markup.add(telebot.types.InlineKeyboardButton("🤖 Чат с ИИ", callback_data="menu_chat"))
    markup.add(telebot.types.InlineKeyboardButton("💰 Мой баланс", callback_data="menu_balance"))
    markup.add(telebot.types.InlineKeyboardButton("🆘 Поддержка", callback_data="menu_support"))
    return markup

def buy_menu(chat_id):
    markup = telebot.types.InlineKeyboardMarkup()
    _, _, _, trial_started, trial_used = get_user(chat_id)
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
    markup.add(telebot.types.InlineKeyboardButton("💻 Кодер", callback_data="mode_coder"))
    markup.add(telebot.types.InlineKeyboardButton("📖 Объяснятор", callback_data="mode_explainer"))
    markup.add(telebot.types.InlineKeyboardButton("🌍 Переводчик", callback_data="mode_translator"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

def back_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

def send_main_menu(chat_id):
    tokens, _, _, _, _ = get_user(chat_id)
    update_state(chat_id, 'idle')
    text = f"👋 <b>Главное меню</b>\n💰 Токенов: <b>{tokens}</b>\n────────────────\nВыбери действие:"
    sent = bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=main_menu())
    remember(chat_id, sent.message_id)

# === /start ===
@bot.message_handler(commands=['start'])
def start(message):
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

# === КУПИТЬ ТОКЕНЫ ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_buy")
def buy_tokens(call):
    _, _, _, trial_started, trial_used = get_user(call.message.chat.id)
    now = int(time.time())
    show_gift = False
    if not trial_used:
        if trial_started == 0:
            set_trial_started(call.message.chat.id)
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
    _, _, _, trial_started, trial_used = get_user(call.message.chat.id)
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
    set_trial_used(call.message.chat.id)
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
    tokens, _, _, _, _ = get_user(call.message.chat.id)
    sent = bot.send_message(call.message.chat.id, f"💰 <b>Ваш баланс:</b> {tokens} токенов", parse_mode='HTML', reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === ЧАТ С ИИ ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_chat")
def enter_chat(call):
    tokens, _, mode, _, _ = get_user(call.message.chat.id)
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
    update_state(call.message.chat.id, 'chat')
    mode_name = {"coder": "💻 Кодер", "explainer": "📖 Объяснятор", "translator": "🌍 Переводчик"}.get(mode, "💻 Кодер")
    sent = bot.send_message(
        call.message.chat.id,
        f"🤖 <b>Вы в чате с ИИ.</b>\n💰 Баланс: {tokens} токенов.\nРежим: <b>{mode_name}</b>\n\n"
        f"Задайте вопрос, отправьте фото или ссылку — 1 запрос = 1 токен.\n"
        f"💡 Если хотите сменить режим — просто напишите «переключись на кодера».",
        parse_mode='HTML',
        reply_markup=mode_menu()
    )
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === РЕЖИМ ===
@bot.callback_query_handler(func=lambda call: call.data.startswith("mode_"))
def set_mode(call):
    mode = call.data.replace("mode_", "")
    update_mode(call.message.chat.id, mode)
    mode_name = {"coder": "💻 Кодер", "explainer": "📖 Объяснятор", "translator": "🌍 Переводчик"}.get(mode, "💻 Кодер")
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

# === ПОДДЕРЖКА ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_support")
def support(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    sent = bot.send_message(call.message.chat.id, "🆘 Напишите: @твой_юзернейм", reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === GIGACHAT (с поддержкой ссылок через v2) ===
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
    "\n\n"
    "У тебя есть 3 РЕЖИМА работы:\n"
    "1. 💻 КОДЕР — пишешь только код.\n"
    "2. 📖 ОБЪЯСНЯТОР — объясняешь сложные вещи простыми словами.\n"
    "3. 🌍 ПЕРЕВОДЧИК — переводишь тексты.\n\n"
    "Если пользователь просит переключить режим — ответь: '✅ Переключаюсь на режим [название].'\n\n"
)

SYSTEM_PROMPTS = {
    "coder": BASE_PROMPT + (
        "ТЕКУЩИЙ РЕЖИМ: 💻 КОДЕР.\n"
        "Ты пишешь ТОЛЬКО код. Формат: сначала блок ```python ... ```, потом одно короткое пояснение. "
        "Никаких 'Конечно!'. Только код."
    ),
    "explainer": BASE_PROMPT + (
        "ТЕКУЩИЙ РЕЖИМ: 📖 ОБЪЯСНЯТОР.\n"
        "Ты объясняешь сложные вещи простыми словами. Приводишь примеры из жизни. "
        "Пиши дружелюбно, но без воды. Отвечай структурированно."
    ),
    "translator": BASE_PROMPT + (
        "ТЕКУЩИЙ РЕЖИМ: 🌍 ПЕРЕВОДЧИК.\n"
        "Ты переводишь тексты. Если пользователь пишет на русском — переводи на английский. "
        "Если на английском — переводи на русский. Отвечай ТОЛЬКО переводом."
    )
}

def detect_mode_request(text):
    text_lower = text.lower()
    if any(w in text_lower for w in ["кодер", "программист", "code", "coder"]):
        if any(w in text_lower for w in ["переключ", "стань", "режим", "смени", "включи"]):
            return "coder"
    if any(w in text_lower for w in ["объясн", "учитель", "explain"]):
        if any(w in text_lower for w in ["переключ", "стань", "режим", "смени", "включи"]):
            return "explainer"
    if any(w in text_lower for w in ["перевод", "translate", "translator"]):
        if any(w in text_lower for w in ["переключ", "стань", "режим", "смени", "включи"]):
            return "translator"
    return None

def ask_gigachat(chat_id, question, mode, image_base64=None):
    access_token = get_gigachat_token()
    if not access_token:
        return "❌ Не удалось получить доступ к ИИ."
    
    # Используем v2 API для поддержки встроенных инструментов (анализ ссылок)
    url = "https://api.giga.chat/v2/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}"
    }
    system_prompt = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["coder"])
    messages = [{"role": "system", "content": system_prompt}]
    history = get_history(chat_id, limit=20)
    for role, content in history:
        messages.append({"role": role, "content": content})

    if image_base64:
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": question if question else "Что на этой картинке?"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
            ]
        })
    else:
        messages.append({"role": "user", "content": question})

    data = {
        "model": "GigaChat-3-Ultra",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 2000,
        "tools": [
            {"type": "url_content_extraction"}
        ]
    }
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

# === СООБЩЕНИЯ В ЧАТЕ ===
@bot.message_handler(content_types=['text', 'photo'])
def handle_message(message):
    tokens, state, mode, _, _ = get_user(message.chat.id)
    if state != 'chat':
        return
    if message.text == "⬅️ Назад":
        back_to_main(message)
        return
    if tokens < 1:
        update_state(message.chat.id, 'idle')
        clear_old_messages(message.chat.id)
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
        markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
        sent = bot.send_message(message.chat.id, "❌ Токены закончились.", reply_markup=markup)
        remember(message.chat.id, sent.message_id)
        return

    if message.text:
        new_mode = detect_mode_request(message.text)
        if new_mode:
            update_mode(message.chat.id, new_mode)
            mode_name = {"coder": "💻 Кодер", "explainer": "📖 Объяснятор", "translator": "🌍 Переводчик"}.get(new_mode, "💻 Кодер")
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            sent = bot.send_message(
                message.chat.id,
                f"✅ <b>Переключаюсь на режим {mode_name}.</b>\n\nТеперь задайте вопрос.",
                parse_mode='HTML',
                reply_markup=mode_menu()
            )
            remember(message.chat.id, sent.message_id)
            return

    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    add_tokens(message.chat.id, -1)
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

    tokens_left, _, _, _, _ = get_user(message.chat.id)

    if "```" in answer or "def " in answer or "import " in answer or "class " in answer:
        clean_code = answer.replace("```python", "").replace("```", "").strip()
        safe_code = escape_html(clean_code)
        try:
            copy_btn = telebot.types.InlineKeyboardButton(text="📋 Скопировать код", copy_text=clean_code)
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(copy_btn)
            markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
            sent = bot.send_message(
                message.chat.id,
                f"<pre><code>{safe_code}</code></pre>\n\n──────────\n💰 Осталось: {tokens_left}",
                parse_mode='HTML',
                reply_markup=markup
            )
        except Exception:
            sent = bot.send_message(
                message.chat.id,
                f"{answer}\n\n──────────\n💰 Осталось: {tokens_left}",
                reply_markup=mode_menu()
            )
    else:
        safe_answer = escape_html(answer)
        try:
            sent = bot.send_message(
                message.chat.id,
                f"{safe_answer}\n\n──────────\n💰 Осталось: {tokens_left}",
                parse_mode='HTML',
                reply_markup=mode_menu()
            )
        except Exception:
            sent = bot.send_message(
                message.chat.id,
                f"{answer}\n\n──────────\n💰 Осталось: {tokens_left}",
                reply_markup=mode_menu()
            )
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
        new_balance, _, _, _, _ = get_user(chat_id)
        clear_old_messages(chat_id)
        parts = label.split("-")
        real_amount = parts[-1] if len(parts) >= 4 else amount
        sent = bot.send_message(
            chat_id,
            f"✅ <b>Оплата прошла!</b>\n\n🧾 Счёт: <code>{label}</code>\n💰 Сумма: {real_amount} ₽\n🎫 Токенов: <b>{tokens}</b>\n💎 Баланс: <b>{new_balance}</b>",
            parse_mode='HTML',
            reply_markup=main_menu()
        )
        remember(chat_id, sent.message_id)
    return jsonify({"status": "ok"}), 200

# === ЗАПУСК ===
def run_flask():
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 3000)))

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    bot.polling(none_stop=True)
