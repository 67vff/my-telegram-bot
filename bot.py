import os
import hashlib
import threading
import telebot
from flask import Flask, request, jsonify

BOT_TOKEN = os.getenv('BOT_TOKEN')
YOOMONEY_RECEIVER = os.getenv('YOOMONEY_RECEIVER')
YOOMONEY_SECRET = os.getenv('YOOMONEY_SECRET')

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

user_orders = {}
last_messages = {}  # chat_id -> [message_id, ...]

# === УДАЛЕНИЕ СТАРЫХ СООБЩЕНИЙ ===
def clear_old_messages(chat_id):
    for msg_id in last_messages.get(chat_id, []):
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    last_messages[chat_id] = []

def remember(chat_id, msg_id):
    last_messages.setdefault(chat_id, []).append(msg_id)

# === REPLY-КЛАВИАТУРА (снизу, как на фото) ===
def main_menu():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(telebot.types.KeyboardButton("💳 Создать счёт"))
    markup.add(telebot.types.KeyboardButton("📋 Мои счета"))
    markup.add(telebot.types.KeyboardButton("🆘 Поддержка"))
    return markup

# === /start ===
@bot.message_handler(commands=['start'])
def start(message):
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    clear_old_messages(message.chat.id)

    text = (
        "👋 <b>Рады видеть вас!</b>\n"
        f"Ваш ID: <code>{message.chat.id}</code>\n"
        "────────────────\n"
        "💎 Здесь вы можете создать счёт и оплатить его."
    )
    sent = bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=main_menu())
    remember(message.chat.id, sent.message_id)

# === "Создать счёт" ===
@bot.message_handler(func=lambda m: m.text == "💳 Создать счёт")
def ask_amount(message):
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    clear_old_messages(message.chat.id)

    sent = bot.send_message(message.chat.id, "💰 Введи сумму в рублях (минимум 10):")
    remember(message.chat.id, sent.message_id)
    bot.register_next_step_handler(sent, create_order)

def create_order(message):
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    clear_old_messages(message.chat.id)

    try:
        amount = float(message.text)
        if amount < 10:
            sent = bot.send_message(message.chat.id, "❌ Минимум 10 рублей. Попробуй снова.")
            remember(message.chat.id, sent.message_id)
            return

        order_id = f"ORD-{message.chat.id}-{int(amount)}"
        user_orders[order_id] = {"chat_id": message.chat.id, "amount": amount}

        pay_url = f"https://bot-1790959533-7739-maks746395.bothost.tech/pay/{int(amount)}/{order_id}"

        # Inline-кнопка под сообщением со счётом
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton(
            text=f"💳 Оплатить {amount} ₽",
            url=pay_url
        ))

        sent = bot.send_message(
            message.chat.id,
            f"🧾 <b>Счёт:</b> <code>{order_id}</code>\n"
            f"💰 <b>Сумма:</b> {amount} ₽\n\n"
            f"Нажми кнопку ниже 👇",
            parse_mode='HTML',
            reply_markup=markup
        )
        remember(message.chat.id, sent.message_id)
    except ValueError:
        sent = bot.send_message(message.chat.id, "❌ Это не число.")
        remember(message.chat.id, sent.message_id)

# === "Мои счета" ===
@bot.message_handler(func=lambda m: m.text == "📋 Мои счета")
def my_orders(message):
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    clear_old_messages(message.chat.id)

    user_list = [oid for oid, o in user_orders.items() if o['chat_id'] == message.chat.id]
    if not user_list:
        sent = bot.send_message(message.chat.id, "📭 У вас нет активных счетов.", reply_markup=main_menu())
    else:
        text = "📋 <b>Ваши счета:</b>\n\n"
        for oid in user_list:
            text += f"• <code>{oid}</code> — {user_orders[oid]['amount']} ₽\n"
        sent = bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=main_menu())
    remember(message.chat.id, sent.message_id)

# === "Поддержка" ===
@bot.message_handler(func=lambda m: m.text == "🆘 Поддержка")
def support(message):
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    clear_old_messages(message.chat.id)

    sent = bot.send_message(message.chat.id, "🆘 Напишите: @твой_юзернейм", reply_markup=main_menu())
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
    if label in user_orders:
        order = user_orders.pop(label)
        clear_old_messages(order['chat_id'])
        sent = bot.send_message(
            order['chat_id'],
            f"✅ <b>Счёт</b> <code>{label}</code> <b>оплачен на {amount} ₽!</b>",
            parse_mode='HTML',
            reply_markup=main_menu()
        )
        remember(order['chat_id'], sent.message_id)

    return jsonify({"status": "ok"}), 200

# === ЗАПУСК ===
def run_flask():
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 3000)))

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    bot.polling(none_stop=True)
