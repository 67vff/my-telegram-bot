import os
import telebot
from telebot import types

bot = telebot.TeleBot(os.getenv('TELEGRAM_BOT_TOKEN'))

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    # Создаём клавиатуру с кнопкой
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn = types.KeyboardButton("Как дела?")
    markup.add(btn)
    bot.send_message(message.chat.id, "Привет! Нажми на кнопку 👇", reply_markup=markup)

@bot.message_handler(func=lambda message: message.text == "Как дела?")
def how_are_you(message):
    bot.reply_to(message, "Нормально! А у тебя как? 😊")

@bot.message_handler(func=lambda message: True)
def echo_all(message):
    bot.reply_to(message, message.text)

bot.polling(none_stop=True)
