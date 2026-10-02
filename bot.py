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

DB_PATH = "/app/data/users.db"

# === БАЗА ДАННЫХ ===
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

def update_state(chat_id, state):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
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

# === ГЛАВНОЕ МЕНЮ (Inline) ===
def main_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
    markup.add(telebot.types.InlineKeyboardButton("🤖 Чат с ИИ", callback_data="menu_chat"))
    markup.add(telebot.types.InlineKeyboardButton("💰 Мой баланс", callback_data="menu_balance"))
    markup.add(telebot.types.InlineKeyboardButton("🆘 Поддержка", callback_data="menu_support"))
    return markup

# === МЕНЮ ПОКУПКИ ===
def buy_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🎁 10 ₽ — 2 токена (пробный)", callback_data="pack_10_2"))
    markup.add(telebot.types.InlineKeyboardButton("100 ₽ — 20 токенов", callback_data="pack_100_20"))
    markup.add(telebot.types.InlineKeyboardButton("250 ₽ — 50 токенов", callback_data="pack_250_50"))
    markup.add(telebot.types.InlineKeyboardButton("500 ₽ — 100 токенов", callback_data="pack_500_100"))
    markup.add(telebot.types.InlineKeyboardButton("✏️ Ввести свою сумму", callback_data="custom_amount"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

# === МЕНЮ ЧАТА ===
def chat_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

# === ОТПРАВКА ГЛАВНОГО МЕНЮ ===
def send_main_menu(chat_id):
    tokens, _ = get_user(chat_id)
    update_state(chat_id, 'idle')
    text = (
        "👋 <b>Главное меню</b>\n"
        f"💰 Токенов: <b>{tokens}</b>\n"
        "────────────────\n"
        "Выбери действие:"
    )
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

# === НАЗАД В ГЛАВНОЕ ===
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
    text = "💳 <b>Покупка токенов</b>\n────────────────\nВыбери пакет:"
    sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=buy_menu())
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
    sent = bot.send_message(call.message.chat.id, "✏️ Введи количество токенов (минимум 20):")
    remember(call.message.chat.id, sent.message_id)
    bot.register_next_step_handler(sent, custom_tokens)

def custom_tokens(message):
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    clear_old_messages(message.chat.id)
    try:
        tokens = int(message.text)
        if tokens < 20:
            sent = bot.send_message(message.chat.id, "❌ Минимум 20 токенов. Попробуй снова.")
            remember(message.chat.id, sent.message_id)
            return
        amount = tokens * 5
        create_invoice(message.chat.id, amount, tokens)
    except ValueError:
        sent = bot.send_message(message.chat.id, "❌ Введи число.")
        remember(message.chat.id, sent.message_id)

# === СОЗДАНИЕ СЧЁТА ===
def create_invoice(chat_id, amount, tokens):
    order_id = f"ORD-{chat_id}-{tokens}-{amount}"
    save_order(order_id, chat_id, tokens)
    pay_url = f"https://bot-1790959533-7739-maks746395.bothost.tech/pay/{amount}/{order_id}"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(text=f"💳 Оплатить {amount} ₽", url=pay_url))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    sent = bot.send_message(
        chat_id,
        f"🧾 <b>Счёт:</b> <code>{order_id}</code>\n"
        f"🎫 <b>Токенов:</b> {tokens}\n"
        f"💰 <b>Сумма:</b> {amount} ₽\n\n"
        f"Нажми кнопку ниже 👇",
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
    tokens, _ = get_user(call.message.chat.id)
    text = f"💰 <b>Ваш баланс:</b> {tokens} токенов"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === ЧАТ С ИИ ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_chat")
def enter_chat(call):
    tokens, _ = get_user(call.message.chat.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)

    if tokens < 1:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
        markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
        sent = bot.send_message(call.message.chat.id, "❌ У вас нет токенов. Купите токены.", reply_markup=markup)
        remember(call.message.chat.id, sent.message_id)
        bot.answer_callback_query(call.id)
        return

    update_state(call.message.chat.id, 'chat')
    sent = bot.send_message(
        call.message.chat.id,
        f"🤖 <b>Вы в чате с ИИ.</b>\n"
        f"💰 Баланс: {tokens} токенов.\n"
        f"Задайте вопрос — 1 запрос = 1 токен.",
        parse_mode='HTML',
        reply_markup=chat_menu()
    )
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
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    sent = bot.send_message(call.message.chat.id, "🆘 Напишите: @твой_юзернейм", reply_markup=markup)
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

# === DEEPSEEK ===
def ask_deepseek(question):
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты полезный ИИ-помощник. Отвечай на русском."},
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

# === СООБЩЕНИЯ В ЧАТЕ ===
@bot.message_handler(func=lambda m: True)
def handle_message(message):
    tokens, state = get_user(message.chat.id)
    if state != 'chat':
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

    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    add_tokens(message.chat.id, -1)
    bot.send_chat_action(message.chat.id, 'typing')
    answer = ask_deepseek(message.text)
    tokens_left, _ = get_user(message.chat.id)
    sent = bot.send_message(
        message.chat.id,
        f"{answer}\n\n──────────\n💰 Осталось токенов: {tokens_left}",
        reply_markup=chat_menu()
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
        clear_old_messages(chat_id)
        sent = bot.send_message(
            chat_id,
            f"✅ <b>Оплата прошла!</b>\n\n"
            f"🧾 Счёт: <code>{label}</code>\n"
            f"💰 Сумма: {amount} ₽\n"
            f"🎫 Начислено токенов: <b>{tokens}</b>\n"
            f"💎 Новый баланс: <b>{new_balance} токенов</b>",
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
