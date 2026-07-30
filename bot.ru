import telebot

TOKEN = "AAFVvdo30UoTpTYfHCc0ztoZLEf74OpFIOI"

bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "Привет! Я твой первый Telegram-бот 🤖")

@bot.message_handler(func=lambda message: True)
def echo(message):
    bot.reply_to(message, message.text)

bot.infinity_polling()