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

# Хранилище счетов
user_orders = {}

# === КОМАНДА /start ===
@bot.message_handler(commands=['start'])
def start(message):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(telebot.types.KeyboardButton("Создать счёт"))
    bot.send_message(message.chat.id, "Привет! Нажми кнопку, чтобы создать счёт.", reply_markup=markup)

# === КНОПКА "Создать счёт" ===
@bot.message_handler(func=lambda m: m.text == "Создать счёт")
def ask_amount(message):
    msg = bot.send_message(message.chat.id, "Введи сумму в рублях (минимум 10):")
    bot.register_next_step_handler(msg, create_order)

def create_order(message):
    try:
        amount = float(message.text)
        if amount < 10:
            bot.send_message(message.chat.id, "Минимальная сумма — 10 рублей. Попробуй снова.")
            return

        order_id = f"ORD-{message.chat.id}-{int(amount)}"
        user_orders[order_id] = {"chat_id": message.chat.id, "amount": amount}

        # Ссылка на нашу Flask-страницу, которая автоматически отправит форму в ЮMoney
        pay_url = f"https://bot-1790959533-7739-maks746395.bothost.tech/pay/{int(amount)}/{order_id}"

        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton(
            text=f"💳 Оплатить {amount} ₽",
            url=pay_url
        ))

        bot.send_message(
            message.chat.id,
            f"Счёт: `{order_id}`\nСумма: {amount} ₽\n\nНажми кнопку, чтобы оплатить:",
            parse_mode='Markdown',
            reply_markup=markup
        )
    except ValueError:
        bot.send_message(message.chat.id, "Это не число. Введи сумму цифрами.")

# === СТРАНИЦА ОПЛАТЫ (автоматически отправляет форму в ЮMoney) ===
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

# === ПРИЁМ УВЕДОМЛЕНИЯ ОТ ЮMONEY ===
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
        bot.send_message(
            order['chat_id'],
            f"✅ Счёт `{label}` на {amount} ₽ оплачен!",
            parse_mode='Markdown'
        )

    return jsonify({"status": "ok"}), 200

# === ЗАПУСК FLASK В ОТДЕЛЬНОМ ПОТОКЕ ===
def run_flask():
    port = int(os.getenv('PORT', 3000))
    app.run(host='0.0.0.0', port=port)

# === ЗАПУСК БОТА ===
if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    bot.polling(none_stop=True)
