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
        mode TEXT DEFAULT 'regular',
        lang TEXT DEFAULT 'ru',
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
    conn.commit()
    for col, definition in [
        ("mode", "TEXT DEFAULT 'regular'"),
        ("lang", "TEXT DEFAULT 'ru'"),
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
        c.execute("SELECT tokens, state, mode, lang, theme, night_mode, notifications FROM users WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
    except sqlite3.OperationalError:
        conn.close()
        init_db()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT tokens, state, mode, lang, theme, night_mode, notifications FROM users WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        row = (1000, 'idle', 'regular', 'ru', 'bright', 1, 1)
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

# === ЭМОДЗИ ПО ТЕМЕ ===
def emoji(chat_id, key):
    _, _, _, _, theme, _, _ = get_user(chat_id)
    if theme == 'minimal':
        return ""
    icons = {"buy": "💳 ", "chat": "🤖 ", "balance": "💰 ", "support": "🆘 ", "settings": "⚙️ ", "back": "⬅️ "}
    return icons.get(key, "")

# === ГЛАВНОЕ МЕНЮ ===
def main_menu(chat_id):
    e = lambda k: emoji(chat_id, k)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(f"{e('buy')}Купить токены", callback_data="menu_buy"))
    markup.add(telebot.types.InlineKeyboardButton(f"{e('chat')}Чат с ИИ", callback_data="menu_chat"))
    markup.add(telebot.types.InlineKeyboardButton(f"{e('balance')}Мой баланс", callback_data="menu_balance"))
    markup.add(telebot.types.InlineKeyboardButton(f"{e('settings')}Настройки", callback_data="menu_settings"))
    markup.add(telebot.types.InlineKeyboardButton(f"{e('support')}Поддержка", callback_data="menu_support"))
    return markup

def buy_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🎁 10 ₽ — 2 токена", callback_data="pack_10_2"))
    markup.add(telebot.types.InlineKeyboardButton("100 ₽ — 20 токенов", callback_data="pack_100_20"))
    markup.add(telebot.types.InlineKeyboardButton("250 ₽ — 50 токенов", callback_data="pack_250_50"))
    markup.add(telebot.types.InlineKeyboardButton("500 ₽ — 100 токенов", callback_data="pack_500_100"))
    markup.add(telebot.types.InlineKeyboardButton("✏️ Своя сумма", callback_data="custom_amount"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

def mode_menu():
    """7 режимов + настройки + назад"""
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🤖 Обычный ИИ", callback_data="mode_regular"))
    markup.add(telebot.types.InlineKeyboardButton("💻 Кодер", callback_data="mode_coder"))
    markup.add(telebot.types.InlineKeyboardButton("📖 Объяснятор", callback_data="mode_explainer"))
    markup.add(telebot.types.InlineKeyboardButton("🌍 Переводчик", callback_data="mode_translator"))
    markup.add(telebot.types.InlineKeyboardButton("✍️ Редактор", callback_data="mode_editor"))
    markup.add(telebot.types.InlineKeyboardButton("📝 Резюме", callback_data="mode_summary"))
    markup.add(telebot.types.InlineKeyboardButton("⚖️ Юрист", callback_data="mode_lawyer"))
    markup.add(telebot.types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings_from_chat"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

def settings_menu(chat_id):
    _, _, _, _, theme, night, notif = get_user(chat_id)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(f"🌙 Ночной режим: {'ВКЛ' if night else 'ВЫКЛ'}", callback_data="set_night"))
    markup.add(telebot.types.InlineKeyboardButton(f"🎨 Тема: {'Яркая' if theme == 'bright' else 'Минимализм'}", callback_data="set_theme"))
    markup.add(telebot.types.InlineKeyboardButton(f"🔔 Уведомления: {'ВКЛ' if notif else 'ВЫКЛ'}", callback_data="set_notif"))
    markup.add(telebot.types.InlineKeyboardButton("🌐 Язык: Русский / English", callback_data="set_lang"))
    markup.add(telebot.types.InlineKeyboardButton("📊 Статистика", callback_data="set_stats"))
    markup.add(telebot.types.InlineKeyboardButton("📜 Очистить историю", callback_data="set_clear"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

def back_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

def send_main_menu(chat_id):
    tokens, _, _, _, _, _, _ = get_user(chat_id)
    update_user(chat_id, 'state', 'idle')
    text = f"👋 <b>Главное меню</b>\n💰 Токенов: <b>{tokens}</b>\n────────────────\nВыбери действие:"
    sent = bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=main_menu(chat_id))
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
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    sent = bot.send_message(call.message.chat.id, "💳 <b>Покупка токенов</b>\n────────────────\nВыбери пакет:", parse_mode='HTML', reply_markup=buy_menu())
    remember(call.message.chat.id, sent.message_id)
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
    tokens, _, _, _, _, _, _ = get_user(call.message.chat.id)
    sent = bot.send_message(call.message.chat.id, f"💰 <b>Ваш баланс:</b> {tokens} токенов", parse_mode='HTML', reply_markup=back_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === НАСТРОЙКИ ===
@bot.callback_query_handler(func=lambda call: call.data in ["menu_settings", "settings_from_chat"])
def open_settings(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    sent = bot.send_message(call.message.chat.id, "⚙️ <b>Настройки</b>\n────────────────\nВыбери параметр:", parse_mode='HTML', reply_markup=settings_menu(call.message.chat.id))
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "set_night")
def set_night(call):
    _, _, _, _, _, night, _ = get_user(call.message.chat.id)
    update_user(call.message.chat.id, 'night_mode', 0 if night else 1)
    bot.answer_callback_query(call.id, f"Ночной режим: {'ВКЛ' if not night else 'ВЫКЛ'}")
    try:
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=settings_menu(call.message.chat.id))
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "set_theme")
def set_theme(call):
    _, _, _, _, theme, _, _ = get_user(call.message.chat.id)
    new_theme = 'minimal' if theme == 'bright' else 'bright'
    update_user(call.message.chat.id, 'theme', new_theme)
    bot.answer_callback_query(call.id, f"Тема: {'Минимализм' if new_theme == 'minimal' else 'Яркая'}")
    try:
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=settings_menu(call.message.chat.id))
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "set_notif")
def set_notif(call):
    _, _, _, _, _, _, notif = get_user(call.message.chat.id)
    update_user(call.message.chat.id, 'notifications', 0 if notif else 1)
    bot.answer_callback_query(call.id, f"Уведомления: {'ВКЛ' if not notif else 'ВЫКЛ'}")
    try:
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=settings_menu(call.message.chat.id))
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "set_lang")
def set_lang(call):
    _, _, _, lang, _, _, _ = get_user(call.message.chat.id)
    new_lang = 'en' if lang == 'ru' else 'ru'
    update_user(call.message.chat.id, 'lang', new_lang)
    bot.answer_callback_query(call.id, f"Язык: {'English' if new_lang == 'en' else 'Русский'}")
    try:
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=settings_menu(call.message.chat.id))
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "set_stats")
def show_stats(call):
    day, week, month = get_stats(call.message.chat.id)
    text = f"📊 <b>Статистика</b>\n────────────────\nЗа сегодня: <b>{day}</b> токенов\nЗа неделю: <b>{week}</b> токенов\nЗа месяц: <b>{month}</b> токенов"
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=back_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "set_clear")
def clear_chat(call):
    clear_history(call.message.chat.id)
    bot.answer_callback_query(call.id, "📜 История чата очищена.")

# === ЧАТ С ИИ ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_chat")
def enter_chat(call):
    tokens, _, mode, _, _, _, _ = get_user(call.message.chat.id)
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
    mode_name = {"regular": "🤖 Обычный ИИ", "coder": "💻 Кодер", "explainer": "📖 Объяснятор", "translator": "🌍 Переводчик", "editor": "✍️ Редактор", "summary": "📝 Резюме", "lawyer": "⚖️ Юрист"}.get(mode, "🤖 Обычный ИИ")
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

# === РЕЖИМЫ (КНОПКИ) ===
@bot.callback_query_handler(func=lambda call: call.data.startswith("mode_"))
def set_mode(call):
    mode = call.data.replace("mode_", "")
    update_user(call.message.chat.id, 'mode', mode)
    mode_name = {"regular": "🤖 Обычный ИИ", "coder": "💻 Кодер", "explainer": "📖 Объяснятор", "translator": "🌍 Переводчик", "editor": "✍️ Редактор", "summary": "📝 Резюме", "lawyer": "⚖️ Юрист"}.get(mode, "🤖 Обычный ИИ")
    bot.answer_callback_query(call.id, f"Режим: {mode_name}")

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
    "Если тебя спросят 'какая ты модель', 'кто ты' — отвечай: 'Я Боб, твой ИИ-помощник.' "
    "Никогда не упоминай GigaChat, Сбер, OpenAI и другие компании. Ты просто Боб. "
    "Отвечай на том языке, на котором написал пользователь. "
)

SYSTEM_PROMPTS = {
    "regular": BASE_PROMPT + "Отвечай на любые вопросы. Помогай с чем угодно.",
    "coder": BASE_PROMPT + "РЕЖИМ: КОДЕР. Пиши ТОЛЬКО код. Формат: ```python ... ``` + одно короткое пояснение.",
    "explainer": BASE_PROMPT + "РЕЖИМ: ОБЪЯСНЯТОР. Объясняй сложные вещи простыми словами, с примерами.",
    "translator": BASE_PROMPT + "РЕЖИМ: ПЕРЕВОДЧИК. Переводи тексты. Русский ↔ английский. Только перевод.",
    "editor": BASE_PROMPT + "РЕЖИМ: РЕДАКТОР. Исправляй ошибки, улучшай стиль. Выводи: 'Исправленный текст:' и 'Что исправлено:' со списком.",
    "summary": BASE_PROMPT + "РЕЖИМ: РЕЗЮМЕ. Сделай краткое содержание текста в 5 предложениях.",
    "lawyer": BASE_PROMPT + "РЕЖИМ: ЮРИСТ. Объясняй юридические вопросы простым языком. Если вопрос серьёзный — добавь: '⚠️ Это не юридическая консультация. Обратитесь к профессиональному юристу.'"
}

def detect_mode_request(text):
    text_lower = text.lower()
    for key, words in [
        ("coder", ["кодер", "программист", "code", "coder"]),
        ("explainer", ["объясн", "учитель", "explain"]),
        ("translator", ["перевод", "translate", "translator"]),
        ("editor", ["редактор", "editor", "исправ"]),
        ("summary", ["резюме", "summary", "краткое"]),
        ("lawyer", ["юрист", "lawyer", "юридич"]),
    ]:
        if any(w in text_lower for w in words):
            if any(w in text_lower for w in ["переключ", "стань", "режим", "смени", "включи"]):
                return key
    return None

def count_tokens_for_summary(text):
    length = len(text)
    if length < 1000:
        return 0
    return (length // 1000) * 2

def ask_gigachat(chat_id, question, mode, image_base64=None):
    access_token = get_gigachat_token()
    if not access_token:
        return "❌ Не удалось получить доступ к ИИ."
    url = "https://api.giga.chat/v2/chat/completions"
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
    data = {"model": "GigaChat-3-Ultra", "messages": messages, "temperature": 0.7, "max_tokens": 2000,
            "tools": [{"type": "url_content_extraction"}]}
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
    tokens, state, mode, _, _, night, _ = get_user(message.chat.id)
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

    # === ПЕРЕКЛЮЧЕНИЕ РЕЖИМА + ОТВЕТ В ОДНОМ СООБЩЕНИИ ===
    prefix = ""
    if message.text:
        new_mode = detect_mode_request(message.text)
        if new_mode:
            update_user(message.chat.id, 'mode', new_mode)
            mode_name = {"regular": "🤖 Обычный ИИ", "coder": "💻 Кодер", "explainer": "📖 Объяснятор", "translator": "🌍 Переводчик", "editor": "✍️ Редактор", "summary": "📝 Резюме", "lawyer": "⚖️ Юрист"}.get(new_mode, "🤖 Обычный ИИ")
            prefix = f"✅ Переключился на режим {mode_name}.\n\n"
            mode = new_mode

    # === РЕЖИМ РЕЗЮМЕ — проверка длины ===
    if mode == "summary" and message.text:
        cost = count_tokens_for_summary(message.text)
        if cost == 0:
            sent = bot.send_message(message.chat.id, "❌ Текст слишком короткий. Минимум 1000 символов для резюме.")
            remember(message.chat.id, sent.message_id)
            return
        if tokens < cost:
            sent = bot.send_message(message.chat.id, f"❌ Недостаточно токенов. Нужно {cost}, у вас {tokens}.")
            remember(message.chat.id, sent.message_id)
            return
        add_tokens(message.chat.id, -cost)
        log_stat(message.chat.id, cost)
    else:
        add_tokens(message.chat.id, -1)
        log_stat(message.chat.id, 1)

    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

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

    tokens_left, _, _, _, _, _, _ = get_user(message.chat.id)
    full_answer = prefix + answer

    # === НОЧНОЙ РЕЖИМ ===
    now_hour = time.localtime().tm_hour
    if night and 0 <= now_hour < 6:
        if len(full_answer) > 200:
            full_answer = full_answer[:200] + "..."
        full_answer = full_answer.replace("🤖", "").replace("✅", "").replace("💰", "")

    if "```" in full_answer or "def " in full_answer or "import " in full_answer or "class " in full_answer:
        clean_code = full_answer.replace("```python", "").replace("```", "").strip()
        safe_code = escape_html(clean_code)
        try:
            copy_btn = telebot.types.InlineKeyboardButton(text="📋 Скопировать код", copy_text=clean_code)
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(copy_btn)
            markup.add(telebot.types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings_from_chat"))
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
        new_balance, _, _, _, _, _, _ = get_user(chat_id)
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
