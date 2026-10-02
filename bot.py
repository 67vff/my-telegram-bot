import os
import sqlite3
import hashlib
import threading
import telebot
import requests
from flask import Flask, request, jsonify

BOT_TOKEN = os.getenv('BOT_TOKEN')
YOOMONEY_RECEIVER = os.getenv('YOOMONEY_RECEIVER')
YOOMONEY_SECRET = os.getenv('YOOMONEY_SECRET')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# === БАЗА ДАННЫХ ===
DB_PATH = "/app/data/users.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        tokens INTEGER DEFAULT 0,
        state TEXT DEFAULT 'idle'
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        tokens INTEGER
    )''')
    conn.commit()
    conn.close()

init_db()

def get_user(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT tokens, state FROM users WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (chat_id, tokens, state) VALUES (?, 0, 'idle')", (chat_id,))
        conn.commit()
        row = (0, 'idle')
    conn.close()
    return row

def update_user(chat_id, tokens=None, state=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if tokens is not None:
        c.execute("UPDATE users SET tokens=? WHERE chat_id=?", (tokens, chat_id))
    if state is not None:
        c.execute("UPDATE users SET state=? WHERE chat_id=?", (state, chat_id))
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

# === КЛАВИАТУРЫ ===
def main_menu():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(telebot.types.KeyboardButton("🤖 ИИ-чат"))
    markup.add(telebot.types.KeyboardButton("💰 Купить токены"))
    markup.add(telebot.types.KeyboardButton("👤 Мой баланс"))
    markup.add(telebot.types.KeyboardButton("🆘 Поддержка"))
    return markup

def in_chat_menu():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(telebot.types.KeyboardButton("🚪 Выйти из чата"))
    return markup

# === /start ===
@bot.message_handler(commands=['start'])
def start(message):
    tokens, _ = get_user(message.chat.id)
    update_user(message.chat.id, state='idle')
    text = (
        "👋 <b>Привет!</b>\n"
        f"Ваш ID: <code>{message.chat.id}</code>\n"
        f"💰 Баланс: <b>{tokens} токенов</b>\n"
        "────────────────\n\n"
        "🤖 Я ИИ-помощник. Могу писать код, отвечать на вопросы, помогать с текстами.\n\n"
        "💎 <b>1 токен = 1 запрос к ИИ = 5 ₽</b>"
    )
    bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=main_menu())

# === "Мой баланс" ===
@bot.message_handler(func=lambda m: m.text == "👤 Мой баланс")
def balance(message):
    tokens, _ = get_user(message.chat.id)
    bot.send_message(
        message.chat.id,
        f"💰 <b>Ваш баланс:</b> {tokens} токенов\n"
        f"💵 Это примерно {tokens * 5} ₽",
        parse_mode='HTML',
        reply_markup=main_menu()
    )

# === "Купить токены" ===
@bot.message_handler(func=lambda m: m.text == "💰 Купить токены")
def buy_tokens(message):
    msg = bot.send_message(message.chat.id, "Сколько токенов купить? (1 токен = 5 ₽, минимум 2 токена = 10 ₽)")
    bot.register_next_step_handler(msg, create_order)

def create_order(message):
    try:
        count = int(message.text)
        if count < 2:
            bot.send_message(message.chat.id, "❌ Минимум 2 токена (10 ₽).")
            return
        amount = count * 5
        order_id = f"ORD-{message.chat.id}-{count}"
        save_order(order_id, message.chat.id, count)

        pay_url = f"https://bot-1790959533-7739-maks746395.bothost.tech/pay/{amount}/{order_id}"

        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton(
            text=f"💳 Оплатить {amount} ₽",
            url=pay_url
        ))

        bot.send_message(
            message.chat.id,
            f"🧾 <b>Счёт:</b> <code>{order_id}</code>\n"
            f"🎫 <b>Токенов:</b> {count}\n"
            f"💰 <b>Сумма:</b> {amount} ₽\n\n"
            f"Нажми кнопку ниже 👇",
            parse_mode='HTML',
            reply_markup=markup
        )
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи число.")

# === "ИИ-чат" ===
@bot.message_handler(func=lambda m: m.text == "🤖 ИИ-чат")
def enter_chat(message):
    tokens, _ = get_user(message.chat.id)
    if tokens < 1:
        bot.send_message(
            message.chat.id,
            "❌ У вас нет токенов. Купите токены, чтобы использовать ИИ.",
            reply_markup=main_menu()
        )
        return
    update_user(message.chat.id, state='chat')
    bot.send_message(
        message.chat.id,
        f"🤖 <b>Вы в ИИ-чате.</b>\n"
        f"💰 Баланс: {tokens} токенов.\n"
        f"Задайте любой вопрос — 1 запрос = 1 токен.",
        parse_mode='HTML',
        reply_markup=in_chat_menu()
    )

# === "Выйти из чата" ===
@bot.message_handler(func=lambda m: m.text == "🚪 Выйти из чата")
def exit_chat(message):
    update_user(message.chat.id, state='idle')
    bot.send_message(message.chat.id, "🚪 Вы вышли из ИИ-чата.", reply_markup=main_menu())

# === "Поддержка" ===
@bot.message_handler(func=lambda m: m.text == "🆘 Поддержка")
def support(message):
    bot.send_message(message.chat.id, "🆘 Напишите: @твой_юзернейм", reply_markup=main_menu())

# === ЗАПРОС В DEEPSEEK ===
def ask_deepseek(question):
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты полезный ИИ-помощник. Отвечай на русском языке."},
            {"role": "user", "content": question}
        ],
        "temperature": 0.7
    }
    try:
        r = requests.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=data, timeout=60)
        result = r.json()
        if "choices" in result:
            return result["choices"][0]["message"]["content"]
        return f"❌ Ошибка ИИ: {result}"
    except Exception as e:
        return f"❌ Ошибка: {e}"

# === ОБРАБОТКА СООБЩЕНИЙ В ЧАТЕ ===
@bot.message_handler(func=lambda m: True)
def handle_message(message):
    tokens, state = get_user(message.chat.id)
    if state != 'chat':
        bot.send_message(message.chat.id, "Используйте кнопки меню 👇", reply_markup=main_menu())
        return
    if tokens < 1:
        update_user(message.chat.id, state='idle')
        bot.send_message(message.chat.id, "❌ Токены закончились. Купите ещё.", reply_markup=main_menu())
        return

    # Списываем 1 токен
    add_tokens(message.chat.id, -1)

    bot.send_chat_action(message.chat.id, 'typing')
    answer = ask_deepseek(message.text)

    tokens_left, _ = get_user(message.chat.id)
    bot.send_message(
        message.chat.id,
        f"{answer}\n\n──────────\n💰 Осталось токенов: {tokens_left}",
        reply_markup=in_chat_menu()
    )

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
    received_hash = data.get('sha1_hash', '')
    check_string = '&'.join([f"{k}={v}" for k, v in sorted(data.items()) if k != 'sha1_hash'])
    check_string += YOOMONEY_SECRET
    if hashlib.sha1(check_string.encode('utf-8')).hexdigest() != received_hash:
        return jsonify({"status": "error"}), 403

    label = data.get('label', '')
    amount = data.get('amount', '')
    order = get_order(label)
    if order:
        chat_id, tokens = order
        add_tokens(chat_id, tokens)
        new_balance, _ = get_user(chat_id)
        bot.send_message(
            chat_id,
            f"✅ <b>Оплата прошла!</b>\n\n"
            f"🧾 Счёт: <code>{label}</code>\n"
            f"💰 Сумма: {amount} ₽\n"
            f"🎫 Начислено токенов: <b>{tokens}</b>\n"
            f"💎 Новый баланс: <b>{new_balance} токенов</b>",
            parse_mode='HTML',
            reply_markup=main_menu()
        )

    return jsonify({"status": "ok"}), 200

# === ЗАПУСК ===
def run_flask():
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 3000)))

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    bot.polling(none_stop=True)
