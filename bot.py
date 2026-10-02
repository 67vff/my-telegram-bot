import os
import telebot
from telebot import types
from yookassa import Configuration, Payment

# Настройка ЮKassa (ключи берутся из переменных окружения хостинга)
Configuration.account_id = os.getenv('YOOKASSA_SHOP_ID')
Configuration.secret_key = os.getenv('YOOKASSA_SECRET_KEY')

bot = telebot.TeleBot(os.getenv('TELEGRAM_BOT_TOKEN'))

# Хранилище для сумм, которые вводят пользователи
user_amounts = {}

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn = types.KeyboardButton("Создать ссылку оплаты")
    markup.add(btn)
    bot.send_message(message.chat.id, "Привет! Нажми на кнопку, чтобы создать счёт на оплату 👇", reply_markup=markup)

@bot.message_handler(func=lambda message: message.text == "Создать ссылку оплаты")
def ask_amount(message):
    msg = bot.send_message(message.chat.id, "Введи сумму в рублях (минимум 10):")
    bot.register_next_step_handler(msg, create_payment_link)

def create_payment_link(message):
    try:
        amount = float(message.text)
        if amount < 10:
            bot.send_message(message.chat.id, "Минимум 10 рублей. Попробуй снова.")
            return
        
        # Создаём платёж в ЮKassa
        payment = Payment.create({
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/твой_бот"},
            "capture": True,
            "description": f"Оплата от {message.from_user.id}"
        })
        
        # Отправляем ссылку пользователю
        link = payment.confirmation.confirmation_url
        bot.send_message(message.chat.id, f"Ссылка на оплату: {link}")
        
    except ValueError:
        bot.send_message(message.chat.id, "Это не число. Попробуй снова.")
    except Exception as e:
        bot.send_message(message.chat.id, f"Ошибка: {e}")

# Эхо для всех остальных сообщений (можно убрать)
@bot.message_handler(func=lambda message: True)
def echo_all(message):
    bot.reply_to(message, message.text)

bot.polling(none_stop=True)
