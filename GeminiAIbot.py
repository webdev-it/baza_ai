import os
import logging
import tempfile
import asyncio
import re
import html
import sqlite3
from typing import Any
from functools import wraps
from datetime import date

from dotenv import load_dotenv
from telegram import Update, Message
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)
from telegram.error import TelegramError

import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted
import speech_recognition as sr
from pydub import AudioSegment

# --- Константы и переменные окружения ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHANNEL_ID = "@baza_ai_channel"
BOT_USERNAME = os.getenv("BOT_USERNAME")
DB_PATH = "bot_data.sqlite"

if not TELEGRAM_TOKEN or not GEMINI_API_KEY or not BOT_USERNAME:
    raise EnvironmentError("TELEGRAM_TOKEN, GEMINI_API_KEY и BOT_USERNAME должны быть заданы в .env")

# --- Логирование ---
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- Настройка Gemini AI ---
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("models/gemini-2.0-flash")

# --- Инициализация базы данных ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            user_id INTEGER,
            req_date TEXT,
            count INTEGER,
            PRIMARY KEY(user_id, req_date)
        )
        """
    )
    conn.commit()
    conn.close()

init_db()

# --- Хранилище истории диалогов ---
user_histories: dict[int, list[dict[str, Any]]] = {}
MAX_HISTORY = 10
LIMIT_UNSUB = 20
LIMIT_SUB = 40

# --- Декоратор обработки ошибок ---
def async_error_handler(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except Exception:
            logger.exception("Ошибка в хендлере")
            msg = update.message or update.effective_message
            if msg:
                await send_long_message(msg, "<b>❌ Произошла ошибка.</b> Попробуйте позже.")
    return wrapper

# --- Работа с базой лимитов ---
def get_request_count(user_id: int, req_date: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT count FROM requests WHERE user_id=? AND req_date=?", (user_id, req_date))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def update_request_count(user_id: int, req_date: str, new_count: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "REPLACE INTO requests (user_id, req_date, count) VALUES (?, ?, ?)",
        (user_id, req_date, new_count)
    )
    conn.commit()
    conn.close()

# --- Проверка подписки пользователя на канал ---
async def is_subscribed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

# --- Проверка и учёт лимита запросов ---
async def check_rate_limit(user_id: int, context: ContextTypes.DEFAULT_TYPE, message: Message) -> bool:
    today = date.today().isoformat()
    count = get_request_count(user_id, today)
    subscribed = await is_subscribed(user_id, context)
    limit = LIMIT_SUB if subscribed else LIMIT_UNSUB
    if count >= limit:
        notice = (
            f"⏳ Вы достигли лимита в {limit} запросов за сегодня. "
            f"{'Подпишитесь на канал для расширения лимита.' if not subscribed else 'Попробуйте завтра.'}"
        )
        await message.reply_text(notice, parse_mode=ParseMode.HTML)
        return False
    update_request_count(user_id, today, count + 1)
    return True

# --- Декоратор учёта лимита ---
def require_rate_limit(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not await check_rate_limit(uid, context, update.message):
            return
        return await func(update, context)
    return wrapped

# --- Конвертация Markdown → HTML ---
def markdown_to_html(text: str) -> str:
    segments = re.split(r"(```[\s\S]*?```)", text)
    html_parts = []
    for seg in segments:
        if seg.startswith('```') and seg.endswith('```'):
            code = seg.strip('`')
            html_parts.append(f"<pre><code>{html.escape(code)}</code></pre>")
        else:
            part = html.escape(seg)
            part = re.sub(r"\*\*(.*?)\*\*|__(.*?)__", lambda m: f"<b>{m.group(1) or m.group(2)}</b>", part)
            part = re.sub(r"\*(.*?)\*|_(.*?)_", lambda m: f"<i>{m.group(1) or m.group(2)}</i>", part)
            part = re.sub(r"`([^`\n]+?)`", lambda m: f"<code>{html.escape(m.group(1))}</code>", part)
            part = re.sub(r"^&gt; (.+)$", r"<blockquote>\1</blockquote>", part, flags=re.M)
            html_parts.append(part)
    return ''.join(html_parts)

# --- Отправка длинных сообщений с HTML ---
TELEGRAM_MSG_LIMIT = 4096
async def send_long_message(message: Message, text: str):
    html_content = markdown_to_html(text)
    parts = []
    while len(html_content) > TELEGRAM_MSG_LIMIT:
        idx = html_content.rfind('\n', 0, TELEGRAM_MSG_LIMIT)
        idx = idx if idx > 0 else TELEGRAM_MSG_LIMIT
        parts.append(html_content[:idx])
        html_content = html_content[idx:]
    parts.append(html_content)
    for part in parts:
        try:
            await message.reply_text(part, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except TelegramError:
            fallback = re.sub(r"<[^>]+>", "", part)
            await message.reply_text(fallback)

# --- Обращение к Gemini с историей и обработкой ошибок ---
async def ask_gemini_with_history(user_id: int, prompt: str) -> str:
    hist = user_histories.get(user_id, [])
    hist.append({'role': 'user', 'parts': [prompt]})
    try:
        resp = await asyncio.to_thread(gemini_model.generate_content, hist)
        reply = resp.text.strip()
    except ResourceExhausted:
        logger.warning('Gemini quota exceeded')
        return '⚠️ Квота исчерпана, попробуйте чуть позже.'
    except Exception:
        logger.exception('Gemini API error')
        return '⚠️ Ошибка при обращении к Gemini AI.'
    hist.append({'role': 'model', 'parts': [reply]})
    user_histories[user_id] = hist[-MAX_HISTORY:]
    return reply

# --- Хендлеры ---
@async_error_handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f'<b>👋 Привет!</b>\n'
        f'У вас есть {LIMIT_UNSUB} запросов в день. Подпишитесь на канал {CHANNEL_ID} для расширения до {LIMIT_SUB}.\n'
        'Отправьте /help для справки.'
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

@async_error_handler
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        '<b>ℹ️ Помощь:</b>\n'
        f'• Базовый лимит: {LIMIT_UNSUB} запросов в день.\n'
        f'• С подпиской: {LIMIT_SUB} запросов в день.\n'
        '• Отправьте текст или голос.\nАдминистрация - @abdumalikovvvvvvv\nКанал - @Baza_ai_channel\n\n'
        '• /reset — сбросить историю.'
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

@async_error_handler
@require_rate_limit
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_histories.pop(update.effective_user.id, None)
    await update.message.reply_text('<b>✅ История сброшена.</b>', parse_mode=ParseMode.HTML)

@async_error_handler
@require_rate_limit
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    logger.info(f'Text from {update.effective_user.id}: {user_text}')
    reply = await ask_gemini_with_history(update.effective_user.id, user_text)
    await send_long_message(update.message, reply)

@async_error_handler
@require_rate_limit
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice
    if not voice:
        return await update.message.reply_text('Не найдено голосового сообщения.')
    with tempfile.TemporaryDirectory() as tmp:
        ogg = os.path.join(tmp, 'voice.ogg')
        wav = os.path.join(tmp, 'voice.wav')
        file = await context.bot.get_file(voice.file_id)
        await asyncio.to_thread(file.download_to_drive, ogg)
        try:
            AudioSegment.from_file(ogg).export(wav, format='wav')
        except Exception:
            logger.exception('Audio convert error')
            return await update.message.reply_text('Ошибка обработки аудио.')
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav) as src:
            audio_data = recognizer.record(src)
            try:
                text = recognizer.recognize_google(audio_data, language='ru-RU')
            except sr.UnknownValueError:
                return await update.message.reply_text('Не удалось распознать речь.')
        reply = await ask_gemini_with_history(update.effective_user.id, text)
        await send_long_message(update.message, reply)

# --- Запуск бота ---
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('reset', reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_error_handler(lambda upd, ctx: logger.exception(f'Global error: {ctx.error}'))
    logger.info('✅ Bot started')
    app.run_polling()

if __name__ == '__main__':
    main()
