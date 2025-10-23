# -*- coding: utf-8 -*-
import logging
import time
import math
import re
from typing import List, Tuple, Optional, Dict

# Aiogram imports
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

# APScheduler imports
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore

# Database import
from bot_db import Database

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Глобальные переменные (будут заполнены из app.py) ---
API_TOKEN = ""
WEBHOOK_URL = ""
TARGET_CHAT_ID = 0 

# Инициализация базы данных и планировщика
DB_NAME = 'bot.db'
db = Database(DB_NAME)
scheduler = AsyncIOScheduler(jobstores={'default': MemoryJobStore()})

# Инициализация бота и диспетчера (parse_mode установлен в диспетчере)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage(), parse_mode=ParseMode.HTML)

# --- Настройка Elo ---
DEFAULT_RATING = 1000

def calculate_k_factor(r1: int, r2: int) -> float:
    """Вычисляет K-фактор по формуле: K = 15 + |R1 - R2| / 100"""
    diff = abs(r1 - r2)
    return 15.0 + diff / 100.0

def calculate_elo_change(rating_winner: int, rating_loser: int) -> int:
    """
    Рассчитывает изменение рейтинга Elo.
    :param rating_winner: Рейтинг победителя.
    :param rating_loser: Рейтинг проигравшего.
    :return: Изменение рейтинга (delta R).
    """
    # Ожидаемый результат для победителя (Ea)
    expected_score_winner = 1.0 / (1.0 + 10.0 ** ((rating_loser - rating_winner) / 400.0))
    
    k = calculate_k_factor(rating_winner, rating_loser)
    
    # Изменение рейтинга (Delta R)
    delta_r = k * (1.0 - expected_score_winner)
    
    return int(round(delta_r))

# --- Хелперы ---

def format_match_description(winner_ids: List[int], loser_ids: List[int], score: str, match_type: str) -> str:
    """Форматирует описание матча для сообщения подтверждения."""
    if match_type == '1vs1':
        winner_tag = f"@{bot.get_my_username()}" # Заглушка, позже будет реальный username
        loser_tag = f"@{bot.get_my_username()}" # Заглушка
        return (
            f"🎾 <b>Одиночный матч (1v1)</b>\n"
            f"🥇 Победитель: @winner_tag\n"
            f"🥈 Проигравший: @loser_tag\n"
            f"📊 Счет: {score}"
        )
    elif match_type == '2vs2':
        winners_tags = [f"@{bot.get_my_username()}"] * len(winner_ids) # Заглушка
        losers_tags = [f"@{bot.get_my_username()}"] * len(loser_ids) # Заглушка
        return (
            f"🏓 <b>Парный матч (2v2)</b>\n"
            f"🥇 Победители: {', '.join(winners_tags)}\n"
            f"🥈 Проигравшие: {', '.join(losers_tags)}\n"
            f"📊 Счет: {score}"
        )
    return "Неизвестный тип матча."

async def resolve_usernames(user_ids: List[int]) -> List[str]:
    """Преобразует список ID в список @usernames или полных имен."""
    resolved_names = []
    for user_id in user_ids:
        try:
            # Получаем информацию о пользователе из Telegram
            member = await bot.get_chat_member(TARGET_CHAT_ID, user_id)
            username = member.user.username
            full_name = member.user.full_name
            
            if username:
                resolved_names.append(f"@{username}")
            else:
                resolved_names.append(f"<b>{full_name}</b>")
        except Exception:
            resolved_names.append(f"Неизвестный игрок ({user_id})")
    return resolved_names

async def update_player_info(user_id: int):
    """Обновляет или создает запись игрока в БД."""
    try:
        member = await bot.get_chat_member(TARGET_CHAT_ID, user_id)
        username = member.user.username if member.user.username else str(user_id)
        full_name = member.user.full_name
        db.get_or_create_player(user_id, username, full_name)
    except Exception as e:
        logging.error(f"Не удалось получить или создать игрока {user_id}: {e}")

# --- Обработчик команд ---

@dp.message(F.text.startswith('/new_result'))
async def handle_new_result_command(message: types.Message):
    """Обрабатывает команды /new_result_1vs1 и /new_result_2vs2."""
    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("Этот бот работает только в групповых чатах.")
        return

    global TARGET_CHAT_ID
    # Сохраняем ID чата при первой команде
    if TARGET_CHAT_ID == 0:
        TARGET_CHAT_ID = message.chat.id
        logging.info(f"Установлен TARGET_CHAT_ID: {TARGET_CHAT_ID}")

    parts = message.text.split()
    if len(parts) < 4:
        await message.reply(
            "Неверный формат команды. Используйте:\n"
            "<code>/new_result_1vs1 @победитель @проигравший 11-9</code>\n"
            "или для 2v2:\n"
            "<code>/new_result_2vs2 @поб1 @поб2 @проиг1 @проиг2 11-9</code>"
        )
        return

    command = parts[0]
    score = parts[-1]
    tags = [t.lstrip('@') for t in parts[1:-1]]
    
    match_type = '1vs1' if command.endswith('1vs1') else '2vs2'

    if match_type == '1vs1' and len(tags) != 2:
        await message.reply("Для 1v1 нужно 2 игрока: @победитель @проигравший.")
        return
    
    if match_type == '2vs2' and len(tags) != 4:
        await message.reply("Для 2v2 нужно 4 игрока: @поб1 @поб2 @проиг1 @проиг2.")
        return

    # 1. Получаем ID игроков
    # В 1vs1: tags[0]=winner, tags[1]=loser
    # В 2vs2: tags[0], tags[1]=winners, tags[2], tags[3]=losers
    
    all_tags = set(tags)
    user_ids: Dict[str, int] = {}
    
    for tag in all_tags:
        user_id = db.get_user_id_by_tag(tag)
        if user_id is None:
            await message.reply(f"Не найден пользователь с @{tag}. Попросите его отправить любое сообщение в чат.")
            return
        user_ids[tag] = user_id

    # 2. Распределяем ID по командам
    if match_type == '1vs1':
        winner_tags = tags[:1]
        loser_tags = tags[1:2]
    else: # 2vs2
        winner_tags = tags[:2]
        loser_tags = tags[2:]

    winner_ids = [user_ids[tag] for tag in winner_tags]
    loser_ids = [user_ids[tag] for tag in loser_tags]
    participants_ids = winner_ids + loser_ids

    # Проверка на дубликаты
    if len(set(participants_ids)) != len(participants_ids):
        await message.reply("Участники матча должны быть уникальными.")
        return

    # 3. Сохраняем заявку в pending
    pending_id = db.add_pending_match(
        match_type, 
        participants_ids, 
        winner_ids, 
        loser_ids, 
        score
    )

    # 4. Запускаем таймер на отмену (2 часа)
    scheduler.add_job(
        delete_pending_match_job, 
        'date', 
        run_date=time.time() + 2 * 3600, # 2 часа в секундах
        args=[pending_id, message.chat.id, message.message_id]
    )

    # 5. Генерируем сообщение для подтверждения
    
    # Разрешаем имена для отображения
    winner_names = await resolve_usernames(winner_ids)
    loser_names = await resolve_usernames(loser_ids)
    
    description = (
        f"🎾 <b>Матч ({match_type})</b>\n"
        f"🥇 Победители: {', '.join(winner_names)}\n"
        f"🥈 Проигравшие: {', '.join(loser_names)}\n"
        f"📊 Счет: {score}\n\n"
        f"❗️ **ОЖИДАНИЕ ПОДТВЕРЖДЕНИЯ** ❗️"
    )

    # Клавиатура подтверждения
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Подтвердить результат", 
        callback_data=f"confirm_{pending_id}"
    )
    
    await message.reply(description, reply_markup=builder.as_markup())


# --- Обработчик Callback Query (Подтверждение) ---

@dp.callback_query(F.data.startswith('confirm_'))
async def process_match_confirmation(callback: types.CallbackQuery):
    """Обрабатывает нажатие кнопки подтверждения."""
    match_id = int(callback.data.split('_')[1])
    user_id = callback.from_user.id

    pending_match_data = db.get_pending_match(match_id)
    
    if not pending_match_data:
        await callback.answer("Эта заявка уже обработана или отменена.")
        await callback.message.delete_reply_markup()
        return

    match_type, winner_ids_str, loser_ids_str, score = pending_match_data
    
    # Преобразуем строки ID в списки int
    winner_ids = [int(i) for i in winner_ids_str.split(',')]
    loser_ids = [int(i) for i in loser_ids_str.split(',')]
    
    participants = winner_ids + loser_ids

    # Проверка, является ли нажавший участником матча
    if user_id not in participants:
        await callback.answer("Подтвердить результат может только участник матча.")
        return

    # --- Подтверждение принято, пересчет Elo ---

    # 1. Получаем текущие рейтинги
    winner_ratings = [db.get_player_rating(uid) for uid in winner_ids]
    loser_ratings = [db.get_player_rating(uid) for uid in loser_ids]
    
    # Средний рейтинг команд
    avg_winner_rating = sum(winner_ratings) / len(winner_ratings)
    avg_loser_rating = sum(loser_ratings) / len(loser_ratings)
    
    # 2. Рассчитываем изменение рейтинга
    delta_r = calculate_elo_change(int(avg_winner_rating), int(avg_loser_rating))
    
    # 3. Применяем изменения
    
    # Победители: получают delta_r
    for uid in winner_ids:
        db.update_player_rating(uid, delta_r)
        
    # Проигравшие: теряют delta_r
    for uid in loser_ids:
        db.update_player_rating(uid, -delta_r)

    # 4. Финализация и очистка
    db.finalize_match(match_id, winner_ids, loser_ids, score, match_type)
    
    # 5. Оповещение
    winner_names = await resolve_usernames(winner_ids)
    loser_names = await resolve_usernames(loser_ids)
    
    notification = (
        f"✅ <b>МАТЧ ПОДТВЕРЖДЕН!</b> (ID: {match_id})\n"
        f"🥇 Победители: {', '.join(winner_names)} (<b>+{delta_r}</b>)\n"
        f"🥈 Проигравшие: {', '.join(loser_names)} (<b>{-delta_r}</b>)\n"
        f"📊 Счет: {score}\n"
    )

    await callback.answer("Результат подтвержден и записан!")
    # Удаляем кнопку подтверждения
    await callback.message.delete_reply_markup()
    # Отправляем уведомление
    await bot.send_message(callback.message.chat.id, notification)


# --- Команда /start ---

@dp.message(F.text == '/start')
async def handle_start(message: types.Message):
    """Приветственное сообщение и инициализация игрока."""
    global TARGET_CHAT_ID
    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("Этот бот предназначен для использования в групповом чате.")
        return

    # Сохраняем ID чата
    if TARGET_CHAT_ID == 0:
        TARGET_CHAT_ID = message.chat.id
        # TODO: Сохранить TARGET_CHAT_ID в .env или отдельном хранилище, чтобы он не сбрасывался

    # Регистрируем отправителя
    await update_player_info(message.from_user.id)

    welcome_message = (
        f"🎾 **Бот для настольного тенниса**\n"
        f"Привет, {message.from_user.full_name}! Я буду автоматически вести ваш рейтинг Elo.\n\n"
        f"**Команды:**\n"
        f"<code>/new_result_1vs1 @поб @проиг счет</code> - Добавить одиночный матч\n"
        f"<code>/new_result_2vs2 @п1 @п2 @пр1 @пр2 счет</code> - Добавить парный матч\n"
        f"<code>/leaderboard</code> - Показать рейтинг\n"
        f"<code>/stats @user</code> - Показать статистику игрока\n"
        f"<code>/history</code> - Показать последние матчи"
    )
    await message.reply(welcome_message)


# --- Команда /leaderboard ---

@dp.message(F.text == '/leaderboard')
async def handle_leaderboard(message: types.Message):
    """Показывает таблицу лидеров."""
    leaderboard = db.get_leaderboard()
    
    if not leaderboard:
        await message.reply("Рейтинг пока пуст. Зарегистрируйте первый матч!")
        return

    # Форматирование
    response = "🏆 **ТАБЛИЦА ЛИДЕРОВ ELO** 🏆\n\n"
    
    for i, (username, rating, wins, losses) in enumerate(leaderboard):
        emoji = {0: "🥇", 1: "🥈", 2: "🥉"}.get(i, "▪️")
        response += (
            f"{emoji} <b>{username}</b> (<code>{rating}</code>)\n"
            f"   В: {wins} | П: {losses}\n"
        )

    await message.reply(response)


# --- Команда /stats ---

@dp.message(F.text.startswith('/stats'))
async def handle_stats(message: types.Message):
    """Показывает индивидуальную статистику."""
    parts = message.text.split()
    target_tag = None
    
    if len(parts) == 2:
        target_tag = parts[1].lstrip('@')
    
    if not target_tag:
        # Если тег не указан, показываем статистику отправителя
        await update_player_info(message.from_user.id) # Обновляем на всякий случай
        user_id = message.from_user.id
    else:
        user_id = db.get_user_id_by_tag(target_tag)
        
    if user_id is None:
        await message.reply("Пользователь не найден в базе данных. Попросите его отправить /start.")
        return

    stats = db.get_player_stats(user_id)
    
    if not stats:
        await message.reply("Статистика для этого пользователя пока недоступна.")
        return

    username, rating, wins, losses = stats
    
    response = (
        f"📊 **СТАТИСТИКА ИГРОКА**\n"
        f"👤 Имя: <b>{username}</b>\n"
        f"⚡️ Рейтинг Elo: <code>{rating}</code>\n"
        f"✅ Победы: {wins}\n"
        f"❌ Поражения: {losses}\n"
        f"📈 Процент побед: {wins / (wins + losses) * 100:.1f}%" if (wins + losses) > 0 else "📈 Процент побед: N/A"
    )
    await message.reply(response)


# --- Команда /history ---

@dp.message(F.text == '/history')
async def handle_history(message: types.Message):
    """Показывает последние 10 матчей."""
    history = db.get_match_history(limit=10)

    if not history:
        await message.reply("История матчей пуста.")
        return
        
    response = "📜 **ПОСЛЕДНИЕ 10 МАТЧЕЙ** 📜\n\n"

    for match_id, match_type, winner_ids_str, loser_ids_str, score, timestamp in history:
        winner_ids = [int(i) for i in winner_ids_str.split(',')]
        loser_ids = [int(i) for i in loser_ids_str.split(',')]
        
        winner_names = await resolve_usernames(winner_ids)
        loser_names = await resolve_usernames(loser_ids)
        
        date_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(timestamp))
        
        response += (
            f"#{match_id} ({date_str})\n"
            f"   Тип: {match_type}\n"
            f"   Победили: {', '.join(winner_names)}\n"
            f"   Проиграли: {', '.join(loser_names)}\n"
            f"   Счет: <b>{score}</b>\n\n"
        )
        
    await message.reply(response)


# --- Административная команда /delete_match (для админа) ---

@dp.message(F.text.startswith('/delete_match'))
async def handle_delete_match(message: types.Message):
    """Удаляет матч по ID из истории (только для админа чата)."""
    
    # 1. Проверка на админа
    try:
        chat_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if chat_member.status not in ['administrator', 'creator']:
            await message.reply("Удалять матчи может только администратор чата.")
            return
    except Exception:
        # Если не удалось получить статус, считаем, что это не админ
        await message.reply("Не удалось проверить права администратора.")
        return
    
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.reply("Неверный формат. Используйте: <code>/delete_match ID_МАТЧА</code>")
        return

    match_id = int(parts[1])
    
    if db.delete_match_by_id(match_id):
        await message.reply(f"❌ Матч #{match_id} успешно удален из истории.")
    else:
        await message.reply(f"Матч #{match_id} не найден.")


# --- Фоновые задачи (APScheduler) ---

async def delete_pending_match_job(match_id: int, chat_id: int, original_message_id: int):
    """Задача планировщика: удаляет заявку, если она не подтверждена через 2 часа."""
    pending_match_data = db.get_pending_match(match_id)
    
    if pending_match_data:
        db.delete_pending_match(match_id)
        
        # Оповещение об отмене
        await bot.send_message(
            chat_id, 
            f"🕒 **ВНИМАНИЕ:** Заявка на матч #{match_id} отменена, так как не была подтверждена в течение 2 часов.",
            reply_to_message_id=original_message_id
        )
        logging.info(f"Заявка #{match_id} отменена по тайм-ауту.")

# --- Главная функция запуска Webhook (используется в app.py) ---

async def start_webhook():
    """Инициализирует планировщик и проверяет настройки базы данных."""
    # Убеждаемся, что БД инициализирована
    db.init_db() 

    # Запускаем планировщик
    if not scheduler.running:
        scheduler.start()
        logging.info("Планировщик APScheduler запущен.")
        
    # Диспетчер уже настроен и готов принимать обновления


# --- Главная функция запуска Polling (для локального тестирования) ---

async def main():
    """Запускает бота в режиме Long Polling (для локального запуска)."""
    # Инициализация переменных (для локального теста замените на свой токен)
    global API_TOKEN, WEBHOOK_URL, TARGET_CHAT_ID
    # API_TOKEN = "ВАШ_ТОКЕН" 
    # WEBHOOK_URL = "http://localhost:8080/webhook" 

    await start_webhook() 
    await dp.start_polling(bot)

if __name__ == '__main__':
    # Эта часть не используется Gunicorn, но полезна для локальной отладки.
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен вручную.")
