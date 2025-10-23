# -*- coding: utf-8 -*-
import logging
import asyncio
from flask import Flask, request, abort
from dotenv import load_dotenv
import os

# Импорт Aiogram для обработки Update
from aiogram import types 

# Загружаем переменные окружения из .env
load_dotenv()

# Импорт логики бота (должен импортироваться после load_dotenv)
# Убедитесь, что bot.py содержит исправленный код
import bot

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Константы и Инициализация ---

# Считываем переменные окружения
API_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
# TARGET_CHAT_ID считывается как строка, преобразуем в int
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", 0)) 

if not API_TOKEN or not WEBHOOK_URL:
    logging.error("BOT_TOKEN или WEBHOOK_URL не установлены.")
    raise ValueError("Отсутствуют необходимые переменные окружения.")

# Присваиваем глобальные переменные в модуле bot для инициализации
bot.API_TOKEN = API_TOKEN
bot.WEBHOOK_URL = WEBHOOK_URL
bot.TARGET_CHAT_ID = TARGET_CHAT_ID

# Создаем Flask-приложение
app = Flask(__name__)

# Флаг для контроля, был ли Webhook установлен (для Gunicorn/Flask)
app_is_ready = False

# --- Функции запуска (Synchronous Wrapper) ---

def run_async_setup():
    """Синхронная обертка для асинхронной настройки бота."""
    global app_is_ready
    if app_is_ready:
        return
        
    logging.info("Запуск асинхронной настройки (БД, Планировщик, Webhook)...")
    try:
        # Создаем новый цикл событий для Gunicorn worker
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # 1. Запускаем асинхронную функцию настройки (инициализация БД и планировщика)
        loop.run_until_complete(bot.start_webhook()) 
        
        # 2. Устанавливаем Webhook через API Telegram
        # Важно: URL должен быть полным (https://165.227.164.121/webhook)
        loop.run_until_complete(bot.bot.set_webhook(url=WEBHOOK_URL))
        
        logging.info(f"Webhook успешно установлен: {WEBHOOK_URL}")
        app_is_ready = True
    except Exception as e:
        logging.error(f"КРИТИЧЕСКАЯ ОШИБКА во время настройки бота: {e}")
        # Если setup не удался, worker должен завершиться
        raise

# Запускаем настройку при импорте (т.е. при запуске каждого Gunicorn worker)
run_async_setup() 


# --- Webhook Endpoint ---

@app.route('/webhook', methods=['POST'])
async def telegram_webhook():
    """Обработка входящих обновлений от Telegram."""
    if not app_is_ready:
        # Если worker не готов, отклоняем запрос
        return "Worker not initialized", 503 
        
    if request.method == 'POST':
        # Получаем JSON-данные
        update_json = request.get_json(silent=True)
        if update_json:
            try:
                # Преобразуем JSON в объект Update Aiogram
                update = types.Update.model_validate(update_json)
                
                # Передаем обновление диспетчеру для обработки
                await bot.dp.feed_update(bot.bot, update)
                
                return "OK", 200
            except Exception as e:
                logging.error(f"Ошибка обработки обновления: {e}")
                return "Internal Server Error", 500
        
        return "Not JSON", 400
    
    return abort(405) # Метод не разрешен


@app.route('/', methods=['GET'])
def index():
    """Простой health check для проверки, что сервер работает."""
    return "Tennis Ladder Bot is running.", 200


if __name__ == '__main__':
    # Эта часть не используется Gunicorn
    logging.warning("Запуск Flask локально. Используйте Gunicorn/Systemd для production.")
    app.run(host='0.0.0.0', port=8000)
