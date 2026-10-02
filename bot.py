import os
import hashlib
import threading
import telebot
from flask import Flask, request, jsonify

# === ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ (Bothost) ===
BOT_TOKEN = os.getenv('BOT_TOKEN')
YOOMONEY_RECEIVER = os.getenv('YOOMONEY_RECEIVER')
YOOMONEY_SECRET = os.getenv('YOOMONEY_SECRET')

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# Хранилище счетов и сообщений
user_orders = {}
last_messages = {}  # chat_id -> [message_id, message_id, ...]

# === ФУНКЦИЯ УДАЛЕНИЯ СТАРЫХ СООБЩЕНИЙ ===
def clear_old_messages(chat_id):
    if chat_id in last_messages:
        for msg_id in last_messages[chat_id]:
            try:
                bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
        last_messages[chat_id] = []

def remember_message(chat_id, message_id):
    if chat_id not in last_messages:
        last_messages[chat_id] = []
    last_messages[chat_id].append(message_id)

# === КОМАНДА /start ===
@bot.message_handler(commands=['start'])
def start(message):
    # Удаляем сообщение пользователя (/start)
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    # Удаляем все прошлые сообщения бота
    clear_old_messages(message.chat.id)

    # Отправляем новое сообщение с Reply-кнопкой
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(telebot.types.KeyboardButton("Создать счёт"))
    sent = bot.send_message(message.chat.id, "Привет! Нажми кнопку, чтобы создать счёт.", reply_markup=markup)
    remember_message(message.chat.id, sent.message_id)

# === КНОПКА "Создать счёт" ===
@bot.message_handler(func=lambda m: m.text == "Создать счёт")
def ask_amount(message):
    # Удаляем сообщение пользователя с нажатой кнопкой
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    # Удаляем все прошлые сообщения бота
    clear_old_messages(message.chat.id)

    # Отправляем запрос на ввод суммы
    sent = bot.send_message(message.chat.id, "Введи сумму в рублях (минимум 10):")
    remember_message(message.chat.id, sent.message_id)
    bot.register_next_step_handler(sent, create_order)

def create_order(message):
    # Удаляем сообщение пользователя с суммой
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    # Удаляем все прошлые сообщения бота
    clear_old_messages(message.chat.id)

    try:
        amount = float(message.text)
        if amount < 10:
            sent = bot.send_message(message.chat.id, "Минимальная сумма — 10 рублей. Попробуй снова.")
            remember_message(message.chat.id, sent.message_id)
            return

        order_id = f"ORD-{message.chat.id}-{int(amount)}"
        user_orders[order_id] = {"chat_id": message.chat.id, "amount": amount}

        pay_url = f"https://bot-1790959533-7739-maks746395.bothost.tech/pay/{int(amount)}/{order_id}"

        # Inline-кнопка "Оплатить" — она под сообщением бота
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton(
            text=f"💳 Оплатить {amount} ₽",
            url=pay_url
        ))

        sent = bot.send_message(
            message.chat.id,
            f"Счёт: `{order_id}`\nСумма: {amount} ₽\n\nНажми кнопку, чтобы оплатить:",
            parse_mode='Markdown',
            reply_markup=markup
        )
        remember_message(message.chat.id, sent.message_id)
    except ValueError:
        sent = bot.send_message(message.chat.id, "Это не число. Введи сумму цифрами.")
        remember_message(message.chat.id, sent.message_id)

# === СТРАНИЦА ОПЛАТЫ ===
@app.route('/pay/<amount>/<label>')
def pay_page(amount, label):
    html = f'''
    <html>
    <head><meta charset="utf-8"><title>Переход к оплате...</title></head>
    <body onload="document.forms[0].submit()">
        <p>Перенаправляем на страницу оплаты...</p>
        <form method="POST" action="https://yoomoney.ru/quickpay/confirm">
            <input type="hidden" name="receiver" value="{YOOMONEY_RECEIVER}"/>
            <input type="hidden" name="quickpay-form" value="button"/>
            <input type="hidden" name="sum" value="{amount}"/>
            <input type="hidden" name="label" value="{label}"/>
            <input type="hidden" name="paymentType" value="AC"/>
            <input type="submit" value="Перейти к оплате"/>
        </form>
    </body>
    </html>
    '''
    return html

# === ВЕБХУК ОТ ЮMONEY ===
@app.route('/webhook', methods=['POST'])
def yoomoney_webhook():
    data = request.form.to_dict()

    received_hash = data.get('sha1_hash', '')
    check_string = '&'.join([f"{k}={v}" for k, v in sorted(data.items()) if k != 'sha1_hash'])
    check_string += YOOMONEY_SECRET
    calculated_hash = hashlib.sha1(check_string.encode('utf-8')).hexdigest()

    if calculated_hash != received_hash:
        return jsonify({"status": "error", "message": "Invalid signature"}), 403

    label = data.get('label', '')
    amount = data.get('amount', '')

    if label in user_orders:
        order = user_orders.pop(label)
        # Удаляем прошлые сообщения бота
        clear_old_messages(order['chat_id'])
        sent = bot.send_message(
            order['chat_id'],
            f"✅ Счёт `{label}` на {amount} ₽ оплачен!",
            parse_mode='Markdown'
        )
        remember_message(order['chat_id'], sent.message_id)

    return jsonify({"status": "ok"}), 200

# === ЗАПУСК ===
def run_flask():
    port = int(os.getenv('PORT', 3000))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    bot.polling(none_stop=True)
