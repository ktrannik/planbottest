import time
import re
import requests
import threading
import sqlite3
import shutil
import os
import sys
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# --- МОСКОВСКОЕ ВРЕМЯ (UTC+3) ---
MSK = timezone(timedelta(hours=3))

def now_utc():
    """Текущее время в UTC (не зависит от хостинга)"""
    return datetime.now(timezone.utc).replace(tzinfo=None)

def now_msk():
    """Текущее время в МСК"""
    return datetime.now(timezone.utc).astimezone(MSK)

def utc_to_msk(dt_utc):
    """UTC (naive) → МСК (naive)"""
    if dt_utc is None:
        return None
    return dt_utc + timedelta(hours=3)

def msk_to_utc(dt_msk):
    """МСК (naive) → UTC (naive)"""
    if dt_msk is None:
        return None
    return dt_msk - timedelta(hours=3)


# --- КОНФИГИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = "@trassa993"
SUGGESTION_LINK = "https://t.me/trassa993?direct"
QUIZZES_DB = 'quizzes.db'
BASE_QUIZZES_DB = 'basequizzes.db'
MEME_ADMIN_ID = "6607609864"

HASHTAGS = [
    "#Новое_поколение", "#Игра_бога", "#Идеальный_мир", "#Голос_времени",
    "#Тринадцать_огней", "#Последняя_реальность", "#Сердце_вселенной",
    "#Точка_невозврата", "#Мастерская_47", "#внесезонов"
]
ADMIN_IDS = [
    "5206039766",
    "123456789",
    "987654321",
]

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT,
            options TEXT,
            correct_option_id INTEGER,
            hashtag TEXT,
            date TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS scheduled (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            username TEXT,
            question TEXT,
            options TEXT,
            correct_option_id INTEGER,
            hashtag TEXT,
            file_id TEXT,
            publish_time TEXT,
            explanation TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS memes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            username TEXT,
            file_id TEXT,
            file_type TEXT,
            hashtag TEXT,
            post_text TEXT, 
            publish_time TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
           chat_id TEXT PRIMARY KEY,
           username TEXT,
           first_seen TEXT,
           last_seen TEXT
        )
    ''')
    try:
        c.execute('ALTER TABLE scheduled ADD COLUMN explanation TEXT')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    print("✅ База данных готова")

def save_user(chat_id, username):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    now = now_utc().isoformat()
    c.execute('''
        INSERT OR REPLACE INTO users (chat_id, username, first_seen, last_seen)
        VALUES (?, ?, COALESCE((SELECT first_seen FROM users WHERE chat_id = ?), ?), ?)
    ''', (chat_id, username, chat_id, now, now))
    conn.commit()
    conn.close()

# --- ФУНКЦИИ ДЛЯ ВИКТОРИН ---
def save_scheduled(chat_id, username, question, options, correct_option_id, hashtag, file_id, publish_time_utc, explanation=None):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        INSERT INTO scheduled (chat_id, username, question, options, correct_option_id, hashtag, file_id, publish_time, explanation)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (chat_id, username, question, options, correct_option_id, hashtag, file_id, publish_time_utc.isoformat(), explanation))
    conn.commit()
    conn.close()

def get_due_quizzes():
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    now = now_utc().isoformat()
    c.execute('''
        SELECT id, chat_id, question, options, correct_option_id, hashtag, file_id, publish_time, explanation
        FROM scheduled WHERE publish_time <= ?
    ''', (now,))
    rows = c.fetchall()
    conn.close()
    return rows

def delete_scheduled(quiz_id):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('DELETE FROM scheduled WHERE id = ?', (quiz_id,))
    conn.commit()
    conn.close()

def get_user_scheduled(chat_id):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        SELECT id, question, publish_time FROM scheduled WHERE chat_id = ? ORDER BY publish_time
    ''', (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_user_scheduled_by_chat_id(chat_id):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        SELECT id, username, question, publish_time FROM scheduled WHERE chat_id = ? ORDER BY publish_time
    ''', (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_user_scheduled_by_username(username):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        SELECT id, chat_id, username, question, publish_time FROM scheduled WHERE username = ? ORDER BY publish_time
    ''', (username,))
    rows = c.fetchall()
    conn.close()
    return rows

def delete_user_scheduled(chat_id, quiz_id=None):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    if quiz_id:
        c.execute('DELETE FROM scheduled WHERE chat_id = ? AND id = ?', (chat_id, quiz_id))
    else:
        c.execute('DELETE FROM scheduled WHERE chat_id = ?', (chat_id,))
    conn.commit()
    conn.close()

def update_scheduled_time(chat_id, quiz_id, new_time_utc):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        UPDATE scheduled SET publish_time = ?
        WHERE chat_id = ? AND id = ?
    ''', (new_time_utc.isoformat(), chat_id, quiz_id))
    changed = c.rowcount
    conn.commit()
    conn.close()
    return changed > 0

# --- ФУНКЦИИ ДЛЯ МЕМОВ ---
def save_meme(chat_id, username, file_id, file_type, hashtag, post_text, publish_time_utc):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        INSERT INTO memes (chat_id, username, file_id, file_type, hashtag, post_text, publish_time)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (chat_id, username, file_id, file_type, hashtag, post_text, publish_time_utc.isoformat()))
    conn.commit()
    conn.close()

def get_due_memes():
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    now = now_utc().isoformat()
    c.execute('''
        SELECT id, chat_id, file_id, file_type, hashtag, post_text, publish_time
        FROM memes WHERE publish_time <= ?
    ''', (now,))
    rows = c.fetchall()
    conn.close()
    return rows

def delete_meme(meme_id):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('DELETE FROM memes WHERE id = ?', (meme_id,))
    conn.commit()
    conn.close()

def get_user_memes(chat_id):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        SELECT id, file_type, hashtag, publish_time FROM memes WHERE chat_id = ? ORDER BY publish_time
    ''', (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_user_memes_by_chat_id(chat_id):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        SELECT id, username, file_type, hashtag, publish_time
        FROM memes WHERE chat_id = ? ORDER BY publish_time
    ''', (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_user_memes_by_username(username):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        SELECT id, chat_id, username, file_type, hashtag, publish_time
        FROM memes WHERE username = ? ORDER BY publish_time
    ''', (username,))
    rows = c.fetchall()
    conn.close()
    return rows

def delete_user_memes(chat_id, meme_id=None):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    if meme_id:
        c.execute('DELETE FROM memes WHERE chat_id = ? AND id = ?', (chat_id, meme_id))
    else:
        c.execute('DELETE FROM memes WHERE chat_id = ?', (chat_id,))
    conn.commit()
    conn.close()

def update_meme_time(chat_id, meme_id, new_time_utc):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        UPDATE memes SET publish_time = ?
        WHERE chat_id = ? AND id = ?
    ''', (new_time_utc.isoformat(), chat_id, meme_id))
    changed = c.rowcount
    conn.commit()
    conn.close()
    return changed > 0

# --- НАПОМИНАЛКА ---
def get_today_memes_by_time_msk(chat_id, target_hour_msk, target_minute_msk):
    """Проверяет, запланирован ли мем на конкретное МСК-время сегодня"""
    now_msk_dt = now_msk()
    target_msk = now_msk_dt.replace(hour=target_hour_msk, minute=target_minute_msk, second=0, microsecond=0)
    target_utc = msk_to_utc(target_msk.replace(tzinfo=None))

    start_utc = (target_utc - timedelta(seconds=30)).isoformat()
    end_utc = (target_utc + timedelta(seconds=30)).isoformat()

    print(f"🔍 ИЩУ В БД: chat_id={chat_id}, МСК={target_hour_msk:02d}:{target_minute_msk:02d} → UTC [{start_utc} .. {end_utc}]")

    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        SELECT id FROM memes 
        WHERE chat_id = ? 
        AND publish_time >= ? 
        AND publish_time <= ?
    ''', (chat_id, start_utc, end_utc))
    rows = c.fetchall()
    conn.close()
    return rows

def send_reminder(bot_token, chat_id, time_str):
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        text = f"⚠️ **Напоминание!**\n\nТы ещё не запланировал мем!\n\n🖼️ Используй `/meme` чтобы создать и запланировать мем."
        requests.post(url, data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        })
        print(f"✅ Напоминание отправлено на {time_str}")
    except Exception as e:
        print(f"❌ Ошибка отправки напоминания: {e}")

def get_all_users():
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('SELECT chat_id FROM users')
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

def notify_all_users(bot_token, message):
    users = get_all_users()
    if not users:
        print("📭 Нет пользователей для уведомления")
        return
    print(f"📤 Отправка уведомления {len(users)} пользователям...")
    for chat_id in users:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            requests.post(url, data={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML"
            })
            time.sleep(0.1)
        except Exception as e:
            print(f"❌ Не удалось отправить {chat_id}: {e}")

def self_health_check():
    while True:
        try:
            conn = sqlite3.connect(QUIZZES_DB)
            c = conn.cursor()
            c.execute("SELECT 1")
            conn.close()
            print("✅ Бот жив")
        except Exception as e:
            for admin_id in ADMIN_IDS:
                try:
                    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                    requests.post(url, data={
                        "chat_id": admin_id,
                        "text": f"⚠️ Бот сломался! Ошибка: {e}"
                    })
                except:
                    pass
        time.sleep(600)

# --- НАПОМИНАЛКИ ---
def reminder_loop():
    while True:
        try:
            now_msk_dt = now_msk()
            current_hour = now_msk_dt.hour
            current_minute = now_msk_dt.minute

            print(f"🔄 Проверка (МСК): {current_hour:02d}:{current_minute:02d}")

            reminder_times = [
                {"hour": 17, "minute": 30, "start_remind": 17, "start_minute": 5},
                {"hour": 18, "minute": 30, "start_remind": 18, "start_minute": 5},
                {"hour": 19, "minute": 30, "start_remind": 19, "start_minute": 5},
            ]

            for rt in reminder_times:
                if current_hour == rt["start_remind"] and rt["start_minute"] <= current_minute <= rt["start_minute"] + 20:
                    existing = get_today_memes_by_time_msk(MEME_ADMIN_ID, rt["hour"], rt["minute"])
                    print(f"📊 Найдено мемов в БД: {len(existing)}")
                    if not existing:
                        if current_minute % 5 == 0:
                            send_reminder(
                                BOT_TOKEN,
                                MEME_ADMIN_ID,
                                f"{rt['hour']:02d}:{rt['minute']:02d} МСК"
                            )
                            print(f"⏰ Напоминание отправлено на {rt['hour']:02d}:{rt['minute']:02d} МСК")

        except Exception as e:
            print(f"❌ Ошибка в напоминалке: {e}")

        time.sleep(60)

# --- ФОНОВЫЙ ПОТОК ---
def scheduler_loop():
    while True:
        try:
            # --- ПРОВЕРКА ВИКТОРИН ---
            due = get_due_quizzes()
            for row in due:
                quiz_id, chat_id, question, options, correct_option_id, hashtag, file_id, publish_time, explanation = row
                options_list = options.split('|||') if options else []

                try:
                    caption = f"Викторина\n{hashtag}\n\n<a href=\"{SUGGESTION_LINK}\">ТрясЛо №993 | Скинуть что-нибудь в предложку</a>"

                    url_photo = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
                    requests.post(url_photo, data={
                        "chat_id": CHANNEL_ID,
                        "photo": file_id,
                        "caption": caption,
                        "parse_mode": "HTML"
                    })

                    url_poll = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPoll"
                    poll_data = {
                        "chat_id": CHANNEL_ID,
                        "question": question,
                        "options": options_list,
                        "type": "quiz",
                        "correct_option_id": correct_option_id,
                        "is_anonymous": True
                    }
                    if explanation:
                        poll_data["explanation"] = explanation[:200]
                        poll_data["explanation_parse_mode"] = "HTML"
                    resp = requests.post(url_poll, json=poll_data)

                    if resp.json().get('ok'):
                        print(f"✅ Опубликовано: {question[:30]}...")
                    else:
                        print(f"❌ Ошибка: {resp.json()}")

                    delete_scheduled(quiz_id)

                except Exception as e:
                    print(f"❌ Ошибка публикации: {e}")

            # --- ПРОВЕРКА МЕМОВ ---
            due_memes = get_due_memes()
            for row in due_memes:
                meme_id, chat_id, file_id, file_type, hashtag, post_text, publish_time = row
                try:
                    caption = f"Мем\n{hashtag}"
                    if post_text:
                        caption += f"\n\n{post_text}"
                    caption += f"\n\n<a href=\"{SUGGESTION_LINK}\">ТрясЛо №993 | Скинуть что-нибудь в предложку</a>"

                    if file_type == 'photo':
                        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
                        requests.post(url, data={
                            "chat_id": CHANNEL_ID,
                            "photo": file_id,
                            "caption": caption,
                            "parse_mode": "HTML"
                        })
                    else:
                        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
                        requests.post(url, data={
                            "chat_id": CHANNEL_ID,
                            "video": file_id,
                            "caption": caption,
                            "parse_mode": "HTML"
                        })

                    print(f"✅ Мем опубликован: {hashtag}")
                    delete_meme(meme_id)

                except Exception as e:
                    print(f"❌ Ошибка публикации мема: {e}")

        except Exception as e:
            print(f"❌ Ошибка в планировщике: {e}")

        time.sleep(10)

# --- ПАРСИНГИ ---
def parse_datetime(text):
    """
    Парсит время от пользователя в МСК и возвращает UTC (naive).
      - 20:33 (только время)
      - 08.07 20:33
      - 08.07.2026 20:33
    """
    now_msk_dt = now_msk().replace(tzinfo=None)

    # Дата + время с годом
    match = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})', text)
    if match:
        day, month, year, hour, minute = map(int, match.groups())
        dt_msk = datetime(year, month, day, hour, minute)
        return msk_to_utc(dt_msk)

    # Дата + время
    match = re.search(r'(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})', text)
    if match:
        day, month, hour, minute = map(int, match.groups())
        dt_msk = datetime(now_msk_dt.year, month, day, hour, minute)
        if dt_msk < now_msk_dt:
            if dt_msk.date() == now_msk_dt.date():
                dt_msk = dt_msk + timedelta(days=1)
            else:
                dt_msk = dt_msk.replace(year=now_msk_dt.year + 1)
        return msk_to_utc(dt_msk)

    # Только время
    match = re.search(r'(\d{1,2}):(\d{2})', text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        dt_msk = now_msk_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt_msk < now_msk_dt:
            dt_msk = dt_msk + timedelta(days=1)
        return msk_to_utc(dt_msk)

    return None

def parse_quiz(text):
    """
    Парсит викторину. Формат:
        Вопрос (А; Б*; В; Г)
        Объяснение (необязательно, на той же или следующей строке)
    Правильный ответ помечен *.
    """
    text = text.strip()

    match = re.match(r'^(.*?)\s*\((.+?)\)\s*(.*)$', text, re.DOTALL)
    if not match:
        return None

    question_part = match.group(1).strip()
    options_part = match.group(2).strip()
    explanation = match.group(3).strip() or None

    if not question_part:
        return None

    options = [opt.strip() for opt in options_part.split(';') if opt.strip()]
    if len(options) < 2:
        return None

    correct_option_id = None
    cleaned = []
    for i, opt in enumerate(options):
        if opt.endswith('*'):
            correct_option_id = i
            cleaned.append(opt[:-1].strip())
        else:
            cleaned.append(opt)

    if correct_option_id is None:
        correct_option_id = 0

    return {
        "question": question_part,
        "options": cleaned,
        "correct_option_id": correct_option_id,
        "explanation": explanation,
    }

def init_base_db():
    conn = sqlite3.connect(BASE_QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS base_quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT,
            options TEXT,
            correct_option_id INTEGER,
            date TEXT
        )
    ''')
    conn.commit()
    conn.close()
    print("✅ База базовых вопросов готова")

def backup_base_quizzes():
    if os.path.exists(BASE_QUIZZES_DB):
        backup_name = f"base_quizzes_backup_{now_msk().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(BASE_QUIZZES_DB, backup_name)
        return backup_name
    return None

def backup_quizzes():
    if os.path.exists(QUIZZES_DB):
        backup_name = f"quizzes_backup_{now_msk().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(QUIZZES_DB, backup_name)
        return backup_name
    return None

# --- ОБРАБОТЧИКИ ВИКТОРИН ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Бот для викторин и мемов\n\n"
        "📝 /quiz — создать викторину\n"
        "🖼️ /meme — создать мем\n"
        "📋 /my — мои запланированные викторины\n"
        "📋 /mymemes — мои запланированные мемы\n"
        "✏️ /editquiz <id> — изменить время викторины\n"
        "✏️ /editmeme <id> — изменить время мема\n"
        "🗑️ /cancel_all — отменить все викторины\n"
        "🗑️ /cancelallmemes — отменить все мемы\n"
        "🔍 /view @username — посмотреть викторины пользователя\n"
        "🖼️ /viewmeme @username — посмотреть мемы пользователя\n"
        "🆔 /id — показать свой ID"
    )

async def my_quizzes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    scheduled = get_user_scheduled(chat_id)
    if not scheduled:
        await update.message.reply_text("📭 У тебя нет запланированных викторин.")
        return
    reply = "📋 **Твои запланированные викторины:**\n\n"
    for idx, (quiz_id, question, publish_time) in enumerate(scheduled, 1):
        dt_utc = datetime.fromisoformat(publish_time)
        dt_msk = utc_to_msk(dt_utc)
        reply += f"{idx}. {question[:40]}... → {dt_msk.strftime('%d.%m %H:%M')} МСК\n"
        reply += f"   🆔 {quiz_id} | /cancel {quiz_id} | /editquiz {quiz_id}\n\n"
    reply += "✏️ `/editquiz <id>` — изменить время"
    await update.message.reply_text(reply)

async def cancel_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("❌ Укажи ID: `/cancel 123`")
        return
    try:
        quiz_id = int(context.args[0])
        delete_user_scheduled(chat_id, quiz_id)
        await update.message.reply_text(f"✅ Викторина #{quiz_id} отменена.")
    except:
        await update.message.reply_text("❌ Ошибка. ID должен быть числом.")

async def cancel_quiz_by_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    try:
        number = int(update.message.text.split('_')[1])
    except:
        await update.message.reply_text("❌ Использование: `/cancel_1`, `/cancel_2`...")
        return
    scheduled = get_user_scheduled(chat_id)
    if not scheduled:
        await update.message.reply_text("📭 Нет викторин.")
        return
    if number < 1 or number > len(scheduled):
        await update.message.reply_text(f"❌ Викторины #{number} нет. Всего {len(scheduled)}.")
        return
    quiz_id = scheduled[number - 1][0]
    delete_user_scheduled(chat_id, quiz_id)
    await update.message.reply_text(f"✅ Викторина #{number} отменена.")

async def cancel_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    delete_user_scheduled(chat_id)
    await update.message.reply_text("✅ Все викторины отменены.")

async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['step'] = 'waiting_for_quiz_text'
    await update.message.reply_text(
        "📝 Отправь в формате:\n\n"
        "`Вопрос (Вариант 1; Вариант 2*; Вариант 3; Вариант 4)`\n"
        "`Объяснение (необязательно)`\n\n"
        "Пример:\n"
        "`Как зовут чела? (А*; Б; В; Г)`\n"
        "`А потому что он Вася`\n\n"
        "Где * — правильный ответ.\n"
        "Всё, что после скобок — объяснение, которое появится после ответа на викторину (макс. 200 символов)."
    )

async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or "без юзернейма"
    await update.message.reply_text(f"🆔 **Твой ID:** `{user_id}`\n👤 **Юзернейм:** @{username}")

async def view_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Укажи: `/view @username` или `/view 123456789`")
        return

    target = context.args[0]

    # --- По username ---
    if target.startswith('@'):
        username = target[1:]
        scheduled = get_user_scheduled_by_username(username)
        if not scheduled:
            await update.message.reply_text(f"📭 У @{username} нет викторин.")
            return
        # scheduled = (id, chat_id, username, question, publish_time)
        reply = f"📋 **Викторины @{username}:**\n\n"
        for idx, row in enumerate(scheduled, 1):
            quiz_id, chat_id, _, question, publish_time = row
            dt_msk = utc_to_msk(datetime.fromisoformat(publish_time))
            reply += (
                f"{idx}. {question[:50]}...\n"
                f"   📅 {dt_msk.strftime('%d.%m %H:%M')} МСК\n"
                f"   🆔 {quiz_id} | 👤 `{chat_id}`\n\n"
            )
        await update.message.reply_text(reply)
        return

    # --- По chat_id ---
    if target.isdigit():
        scheduled = get_user_scheduled_by_chat_id(target)
        if not scheduled:
            await update.message.reply_text(f"📭 У `{target}` нет викторин.")
            return
        # scheduled = (id, username, question, publish_time)
        username = scheduled[0][1] if scheduled else "без_юзернейма"
        reply = f"📋 **Викторины @{username}** (`{target}`):\n\n"
        for idx, row in enumerate(scheduled, 1):
            quiz_id, _, question, publish_time = row
            dt_msk = utc_to_msk(datetime.fromisoformat(publish_time))
            reply += (
                f"{idx}. {question[:50]}...\n"
                f"   📅 {dt_msk.strftime('%d.%m %H:%M')} МСК\n"
                f"   🆔 {quiz_id}\n\n"
            )
        await update.message.reply_text(reply)
        return

    await update.message.reply_text("❌ Неправильный формат. Пример: `/view @username` или `/view 123456789`")

async def view_meme_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Укажи: `/viewmeme @username` или `/viewmeme 123456789`")
        return

    target = context.args[0]

    # --- По username ---
    if target.startswith('@'):
        username = target[1:]
        memes = get_user_memes_by_username(username)
        if not memes:
            await update.message.reply_text(f"📭 У @{username} нет мемов.")
            return
        # memes = (id, chat_id, username, file_type, hashtag, publish_time)
        reply = f"🖼️ **Мемы @{username}:**\n\n"
        for idx, row in enumerate(memes, 1):
            meme_id, chat_id, _, file_type, hashtag, publish_time = row
            dt_msk = utc_to_msk(datetime.fromisoformat(publish_time))
            reply += (
                f"{idx}. {file_type} | {hashtag}\n"
                f"   📅 {dt_msk.strftime('%d.%m %H:%M')} МСК\n"
                f"   🆔 {meme_id} | 👤 `{chat_id}`\n\n"
            )
        await update.message.reply_text(reply)
        return

    # --- По chat_id ---
    if target.isdigit():
        memes = get_user_memes_by_chat_id(target)
        if not memes:
            await update.message.reply_text(f"📭 У `{target}` нет мемов.")
            return
        # memes = (id, username, file_type, hashtag, publish_time)
        username = memes[0][1] if memes else "без_юзернейма"
        reply = f"🖼️ **Мемы @{username}** (`{target}`):\n\n"
        for idx, row in enumerate(memes, 1):
            meme_id, _, file_type, hashtag, publish_time = row
            dt_msk = utc_to_msk(datetime.fromisoformat(publish_time))
            reply += (
                f"{idx}. {file_type} | {hashtag}\n"
                f"   📅 {dt_msk.strftime('%d.%m %H:%M')} МСК\n"
                f"   🆔 {meme_id}\n\n"
            )
        await update.message.reply_text(reply)
        return

    await update.message.reply_text("❌ Неправильный формат. Пример: `/viewmeme @username` или `/viewmeme 123456789`")

async def edit_quiz_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /editquiz <id> — затем бот спросит новое время """
    chat_id = str(update.effective_user.id)

    if not context.args:
        await update.message.reply_text(
            "❌ Укажи ID викторины: `/editquiz 123`\n\n"
            "Посмотреть ID: /my"
        )
        return

    try:
        quiz_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
        return

    # Проверяем, что викторина принадлежит юзеру
    scheduled = get_user_scheduled(chat_id)
    found = None
    for row in scheduled:
        if row[0] == quiz_id:
            found = row
            break

    if not found:
        await update.message.reply_text(f"❌ Викторина #{quiz_id} не найдена в твоих.")
        return

    context.user_data['edit_quiz_id'] = quiz_id
    context.user_data['step'] = 'waiting_for_edit_quiz_time'
    await update.message.reply_text(
        f"✏️ Меняем время викторины #{quiz_id}\n\n"
        f"❓ {found[1][:60]}...\n\n"
        "📅 Напиши новое время (МСК):\n"
        "`20:33` или `08.07 20:33` или `08.07.2026 20:33`"
    )

async def edit_meme_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /editmeme <id> — затем бот спросит новое время """
    chat_id = str(update.effective_user.id)

    if not context.args:
        await update.message.reply_text(
            "❌ Укажи ID мема: `/editmeme 123`\n\n"
            "Посмотреть ID: /mymemes"
        )
        return

    try:
        meme_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
        return

    memes = get_user_memes(chat_id)
    found = None
    for row in memes:
        if row[0] == meme_id:
            found = row
            break

    if not found:
        await update.message.reply_text(f"❌ Мем #{meme_id} не найден в твоих.")
        return

    context.user_data['edit_meme_id'] = meme_id
    context.user_data['step'] = 'waiting_for_edit_meme_time'
    await update.message.reply_text(
        f"✏️ Меняем время мема #{meme_id}\n\n"
        f"🏷️ {found[2]}\n\n"
        "📅 Напиши новое время (МСК):\n"
        "`20:33` или `08.07 20:33` или `08.07.2026 20:33`"
    )

# --- ОБРАБОТЧИКИ МЕМОВ ---
async def start_meme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['step'] = 'waiting_for_meme_media'
    await update.message.reply_text(
        "🖼️ Отправь картинку или видео для мема.\n\n"
        "После загрузки выбери действие."
    )

async def handle_meme_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('step') != 'waiting_for_meme_media':
        return
    file_id = None
    file_type = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_type = 'photo'
    elif update.message.video:
        file_id = update.message.video.file_id
        file_type = 'video'
    else:
        await update.message.reply_text("❌ Отправь картинку или видео.")
        return
    context.user_data['meme_file_id'] = file_id
    context.user_data['meme_file_type'] = file_type
    context.user_data['step'] = 'waiting_for_meme_hashtag'
    keyboard = [
        [InlineKeyboardButton("#мемло", callback_data="meme_h_memlo")],
        [InlineKeyboardButton("#линчфд", callback_data="meme_h_newgen")],
        [InlineKeyboardButton("#МШфд", callback_data="meme_h_igra")],
        [InlineKeyboardButton("#неошафд", callback_data="meme_h_ideal")],
        [InlineKeyboardButton("✏️ Свой", callback_data="meme_h_custom")],
    ]
    await update.message.reply_text(
        "🏷️ Выбери основной хэштег для мема:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def meme_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "meme_publish_now":
        file_id = context.user_data.get('meme_file_id')
        file_type = context.user_data.get('meme_file_type')
        hashtag = context.user_data.get('meme_hashtag', '#мемло')
        post_text = context.user_data.get('meme_post_text')

        if not file_id:
            await query.edit_message_text("❌ Ошибка. Начни заново через /meme")
            context.user_data.clear()
            return

        await query.edit_message_text("📤 Публикую мем сейчас...")

        try:
            caption = f"Мем\n{hashtag}"
            if post_text:
                caption += f"\n\n{post_text}"
            caption += f"\n\n<a href=\"{SUGGESTION_LINK}\">ТрясЛо №993 | Скинуть что-нибудь в предложку</a>"

            if file_type == 'photo':
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
                requests.post(url, data={
                    "chat_id": CHANNEL_ID,
                    "photo": file_id,
                    "caption": caption,
                    "parse_mode": "HTML"
                })
            else:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
                requests.post(url, data={
                    "chat_id": CHANNEL_ID,
                    "video": file_id,
                    "caption": caption,
                    "parse_mode": "HTML"
                })

            await query.edit_message_text(
                f"✅ Мем ОПУБЛИКОВАН!\n\n"
                f"🏷️ {hashtag}\n"
                f"📝 {post_text if post_text else 'Без текста'}"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")
        context.user_data.clear()
        return

    if data == "meme_schedule":
        context.user_data['step'] = 'waiting_for_meme_time'
        await query.edit_message_text("📅 **Укажи время публикации** (МСК):\nНапример: `20:33`")
        return

    if data == "meme_cancel":
        await query.edit_message_text("❌ Отменено.")
        context.user_data.clear()
        return

async def handle_meme_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('step') != 'waiting_for_meme_time':
        return
    dt_utc = parse_datetime(update.message.text)
    if dt_utc is None:
        await update.message.reply_text("❌ Не понял формат. Пример: `20:33`")
        return
    if dt_utc < now_utc():
        await update.message.reply_text("❌ Время уже прошло!")
        return
    chat_id = str(update.effective_user.id)
    username = update.effective_user.username or "без_юзернейма"
    file_id = context.user_data.get('meme_file_id')
    file_type = context.user_data.get('meme_file_type')
    hashtag = context.user_data.get('meme_hashtag', '#мемло')
    post_text = context.user_data.get('meme_post_text')
    save_meme(chat_id, username, file_id, file_type, hashtag, post_text, dt_utc)
    print(f"✅ МЕМ СОХРАНЁН В БД (UTC): {dt_utc.isoformat()}, chat_id: {chat_id}")
    msk_time = utc_to_msk(dt_utc).strftime('%d.%m.%Y в %H:%M')
    delay = int((dt_utc - now_utc()).total_seconds())
    await update.message.reply_text(
        f"✅ Мем запланирован на **{msk_time}** МСК!\n"
        f"⏳ Осталось: {delay} сек\n"
        f"🏷️ {hashtag}\n"
        f"📝 {post_text if post_text else 'Без текста'}"
    )
    context.user_data.clear()

async def my_memes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    memes = get_user_memes(chat_id)
    if not memes:
        await update.message.reply_text("📭 У тебя нет запланированных мемов.")
        return
    reply = "📋 **Твои запланированные мемы:**\n\n"
    for idx, (meme_id, file_type, hashtag, publish_time) in enumerate(memes, 1):
        dt_msk = utc_to_msk(datetime.fromisoformat(publish_time))
        reply += f"{idx}. {file_type} | {hashtag} → {dt_msk.strftime('%d.%m %H:%M')} МСК\n"
        reply += f"   🆔 {meme_id} | /cancelmeme {meme_id} | /editmeme {meme_id}\n\n"
    reply += "✏️ `/editmeme <id>` — изменить время"
    await update.message.reply_text(reply)

async def cancel_meme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("❌ Укажи ID: `/cancelmeme 123`")
        return
    try:
        meme_id = int(context.args[0])
        delete_user_memes(chat_id, meme_id)
        await update.message.reply_text(f"✅ Мем #{meme_id} отменён.")
    except:
        await update.message.reply_text("❌ Ошибка.")

async def cancel_meme_by_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    try:
        number = int(update.message.text.split('_')[1])
    except:
        await update.message.reply_text("❌ Использование: `/cancelmeme_1`, `/cancelmeme_2`...")
        return
    memes = get_user_memes(chat_id)
    if not memes:
        await update.message.reply_text("📭 Нет мемов.")
        return
    if number < 1 or number > len(memes):
        await update.message.reply_text(f"❌ Мема #{number} нет. Всего {len(memes)}.")
        return
    meme_id = memes[number - 1][0]
    delete_user_memes(chat_id, meme_id)
    await update.message.reply_text(f"✅ Мем #{number} отменён.")

async def cancel_all_memes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    delete_user_memes(chat_id)
    await update.message.reply_text("✅ Все мемы отменены.")

# --- БЭКАПЫ ---
async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💾 Создаю бэкап...")
    try:
        backup_file = backup_quizzes()
        if not backup_file:
            await update.message.reply_text("❌ База не найдена.")
            return
        with open(backup_file, 'rb') as f:
            await update.message.reply_document(document=f, filename=os.path.basename(backup_file), caption="✅ Бэкап создан!")
        os.remove(backup_file)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def backup_base_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💾 Создаю бэкап базы вопросов...")
    try:
        backup_file = backup_base_quizzes()
        if not backup_file:
            await update.message.reply_text("❌ База не найдена.")
            return
        with open(backup_file, 'rb') as f:
            await update.message.reply_document(document=f, filename=os.path.basename(backup_file), caption="✅ Бэкап создан!")
        os.remove(backup_file)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

# --- ОСНОВНОЙ ОБРАБОТЧИК ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    username = update.effective_user.username or "без_юзернейма"
    save_user(chat_id, username)
    text = update.message.text
    print(f"📩 Текст: {text}")
    print(f"📍 Шаг: {context.user_data.get('step')}")
    if not text:
        await update.message.reply_text("❌ Отправь текст")
        return

    step = context.user_data.get('step')

    # --- Редактирование времени викторины ---
    if step == 'waiting_for_edit_quiz_time':
        dt_utc = parse_datetime(text)
        if dt_utc is None:
            await update.message.reply_text("❌ Не понял формат. Пример: `20:33` или `08.07 20:33`")
            return
        if dt_utc < now_utc():
            await update.message.reply_text("❌ Время уже прошло! Укажи будущее время.")
            return

        quiz_id = context.user_data.get('edit_quiz_id')
        chat_id = str(update.effective_user.id)
        ok = update_scheduled_time(chat_id, quiz_id, dt_utc)

        if ok:
            msk_time = utc_to_msk(dt_utc).strftime('%d.%m.%Y в %H:%M')
            await update.message.reply_text(
                f"✅ Время викторины #{quiz_id} изменено на **{msk_time}** МСК!"
            )
        else:
            await update.message.reply_text("❌ Не удалось обновить. Возможно, запись уже удалена.")
        context.user_data.clear()
        return

    # --- Редактирование времени мема ---
    if step == 'waiting_for_edit_meme_time':
        dt_utc = parse_datetime(text)
        if dt_utc is None:
            await update.message.reply_text("❌ Не понял формат. Пример: `20:33` или `08.07 20:33`")
            return
        if dt_utc < now_utc():
            await update.message.reply_text("❌ Время уже прошло! Укажи будущее время.")
            return

        meme_id = context.user_data.get('edit_meme_id')
        chat_id = str(update.effective_user.id)
        ok = update_meme_time(chat_id, meme_id, dt_utc)

        if ok:
            msk_time = utc_to_msk(dt_utc).strftime('%d.%m.%Y в %H:%M')
            await update.message.reply_text(
                f"✅ Время мема #{meme_id} изменено на **{msk_time}** МСК!"
            )
        else:
            await update.message.reply_text("❌ Не удалось обновить. Возможно, запись уже удалена.")
        context.user_data.clear()
        return

    if step == 'waiting_for_meme_post_text':
        context.user_data['meme_post_text'] = text
        context.user_data['step'] = 'waiting_for_meme_action'
        keyboard = [
           [InlineKeyboardButton("✅ Опубликовать сейчас", callback_data="meme_publish_now")],
           [InlineKeyboardButton("⏰ Запланировать на время", callback_data="meme_schedule")],
           [InlineKeyboardButton("❌ Отмена", callback_data="meme_cancel")]
        ]
        await update.message.reply_text(
            f"✅ Текст сохранён:\n\n{text}\n\n"
            "Что делаем с мемом?",
           reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if step == 'waiting_for_meme_time':
        await handle_meme_time(update, context)
        return

    if step == 'waiting_for_meme_custom_hashtag':
        text = text.strip()
        if not text.startswith('#'):
            text = '#' + text
        context.user_data['meme_hashtag'] = text
        context.user_data['step'] = 'waiting_for_meme_flud'
        keyboard = [
            [InlineKeyboardButton("✅ Добавить #ФлудНаПМ", callback_data="meme_flud_yes")],
            [InlineKeyboardButton("⏭️ Пропустить", callback_data="meme_flud_no")]
        ]
        await update.message.reply_text(
            f"✅ Хэштег: {text}\n\n📝 Добавить хэштег #ФлудНаПМ?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if step == 'waiting_for_quiz_text':
        parsed = parse_quiz(text)
        if parsed and len(parsed['options']) >= 2:
            context.user_data['quiz_data'] = parsed
            context.user_data['step'] = 'waiting_for_hashtag'

            keyboard = []
            for hashtag in HASHTAGS:
                keyboard.append([InlineKeyboardButton(hashtag, callback_data=f"hashtag_{hashtag}")])
            keyboard.append([InlineKeyboardButton("✏️ Свой", callback_data="hashtag_custom")])

            explanation_line = ""
            if parsed.get('explanation'):
                expl = parsed['explanation']
                expl_short = expl[:80] + ('...' if len(expl) > 80 else '')
                explanation_line = f"\n📖 Объяснение: {expl_short}"

            await update.message.reply_text(
                f"❓ {parsed['question']}\n"
                f"✅ Правильный ответ: {parsed['options'][parsed['correct_option_id']]}"
                f"{explanation_line}\n\n"
                "🏷️ Выбери хэштег:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                "❌ Неправильный формат.\n\n"
                "Пример:\n"
                "`Как зовут чела? (А*; Б; В; Г)`\n"
                "`А потому что он Вася`\n\n"
                "Или в одну строку:\n"
                "`Как зовут чела? (А*; Б; В; Г) А потому что он Вася`\n\n"
                "Где * — правильный ответ, а текст после скобок — объяснение (необязательно)."
            )
        return

    if step == 'waiting_for_time':
        dt_utc = parse_datetime(text)
        if dt_utc is None:
            await update.message.reply_text("❌ Не понял формат. Пример: `20:33` или `08.07 20:33`")
            return
        if dt_utc < now_utc():
            await update.message.reply_text("❌ Время уже прошло! Укажи будущее время.")
            return
        context.user_data['publish_time'] = dt_utc
        context.user_data['step'] = 'waiting_for_confirmation'
        delay = int((dt_utc - now_utc()).total_seconds())
        msk_time = utc_to_msk(dt_utc).strftime('%d.%m.%Y в %H:%M')
        keyboard = [
            [InlineKeyboardButton("✅ Запланировать", callback_data="confirm_publish")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_publish")]
        ]
        explanation = context.user_data['quiz_data'].get('explanation')
        explanation_line = f"\n📖 Объяснение: {explanation[:50]}..." if explanation else "\n📖 Без объяснения"
        await update.message.reply_text(
            f"📅 **Публикация:** {msk_time} МСК\n"
            f"⏳ **Осталось:** {delay} сек\n\n"
            f"❓ {context.user_data['quiz_data']['question']}\n"
            f"🏷️ {context.user_data['quiz_hashtag']}"
            f"{explanation_line}\n\n"
            "✅ Подтверждаешь?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if step == 'waiting_for_custom_hashtag':
        text = text.strip()
        if not text.startswith('#'):
            text = '#' + text
        context.user_data['quiz_hashtag'] = text
        context.user_data['step'] = 'waiting_for_image'
        await update.message.reply_text(
            f"✅ Хэштег: {text}\n\n"
            "🖼️ Отправь картинку для поста.\n\n"
            "После картинки выбери действие."
        )
        return

    await update.message.reply_text(
        "❓ Я не понял.\n\n"
        "Команды:\n"
        "/quiz — викторина\n"
        "/meme — мем\n"
        "/my — мои викторины\n"
        "/mymemes — мои мемы\n"
        "/editquiz <id> — изменить время викторины\n"
        "/editmeme <id> — изменить время мема\n"
        "/cancel_all — отменить все викторины\n"
        "/cancelallmemes — отменить все мемы\n"
        "/id — мой ID\n"
        "/view @username — викторины пользователя\n"
        "/viewmeme @username — мемы пользователя"
    )

# --- КНОПКИ ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    print(f"🔘 Нажата кнопка: {data}")

    if data in ["meme_publish_now", "meme_schedule", "meme_cancel"]:
        await meme_button_callback(update, context)
        return

    if data.startswith("hashtag_"):
        hashtag = data.replace("hashtag_", "")
        if hashtag == "custom":
            await query.edit_message_text("✏️ Напиши свой хэштег (например, #МойХэштег)")
            context.user_data['step'] = 'waiting_for_custom_hashtag'
            return
        context.user_data['quiz_hashtag'] = hashtag
        context.user_data['step'] = 'waiting_for_image'
        await query.edit_message_text(
            f"✅ Хэштег: {hashtag}\n\n"
            "🖼️ Отправь картинку для поста.\n\n"
            "После картинки выбери действие."
        )
        return

    if data.startswith("meme_h_"):
        hashtag = data.replace("meme_h_", "")
        if hashtag == "custom":
            context.user_data['step'] = 'waiting_for_meme_custom_hashtag'
            await query.edit_message_text("✏️ Напиши свой хэштег (например, #МойХэштег)")
            return
        hashtag_map = {
            "memlo": "#мемло",
            "newgen": "#линчфд",
            "igra": "#МШфд",
            "ideal": "#неошафд",
        }
        context.user_data['meme_hashtag'] = hashtag_map.get(hashtag, "#" + hashtag)
        await query.edit_message_text(f"✅ Основной хэштег: {context.user_data['meme_hashtag']}")
        context.user_data['step'] = 'waiting_for_meme_flud'
        keyboard = [
            [InlineKeyboardButton("✅ Добавить #ФлудНаПМ", callback_data="meme_flud_yes")],
            [InlineKeyboardButton("⏭️ Пропустить", callback_data="meme_flud_no")]
        ]
        await query.message.reply_text(
            "📝 Добавить хэштег #ФлудНаПМ?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "meme_flud_yes":
        existing = context.user_data.get('meme_hashtag', '')
        context.user_data['meme_hashtag'] = existing + " #ФлудНаПМ"
        await query.edit_message_text("✅ Хэштег #ФлудНаПМ добавлен!")
        context.user_data['step'] = 'waiting_for_meme_text'
        keyboard = [
            [InlineKeyboardButton("✅ Добавить текст", callback_data="meme_text_yes")],
            [InlineKeyboardButton("⏭️ Без текста", callback_data="meme_text_no")]
        ]
        await query.message.reply_text(
            "📝 Добавить текст к мему?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "meme_flud_no":
        await query.edit_message_text("⏭️ #ФлудНаПМ пропущен.")
        context.user_data['step'] = 'waiting_for_meme_text'
        keyboard = [
            [InlineKeyboardButton("✅ Добавить текст", callback_data="meme_text_yes")],
            [InlineKeyboardButton("⏭️ Без текста", callback_data="meme_text_no")]
        ]
        await query.message.reply_text(
            "📝 Добавить текст к мему?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "meme_text_yes":
        context.user_data['step'] = 'waiting_for_meme_post_text'
        await query.edit_message_text(
            "📝 Напиши текст для мема.\n\n"
            "Он будет подписью к картинке/видео."
        )
        return

    if data == "meme_text_no":
        context.user_data['meme_post_text'] = None
        context.user_data['step'] = 'waiting_for_meme_action'
        keyboard = [
            [InlineKeyboardButton("✅ Опубликовать сейчас", callback_data="meme_publish_now")],
            [InlineKeyboardButton("⏰ Запланировать на время", callback_data="meme_schedule")],
            [InlineKeyboardButton("❌ Отмена", callback_data="meme_cancel")]
        ]
        await query.edit_message_text(
            "⏭️ Без текста.\n\nЧто делаем с мемом?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "schedule":
        context.user_data['step'] = 'waiting_for_time'
        await query.edit_message_text(
            "📅 **Укажи время публикации** (МСК):\nНапример: `20:33` или `08.07 20:33`"
        )
        return

    if data == "confirm_publish":
        chat_id = str(update.effective_user.id)
        username = update.effective_user.username or "без_юзернейма"
        quiz_data = context.user_data.get('quiz_data')
        hashtag = context.user_data.get('quiz_hashtag')
        file_id = context.user_data.get('file_id')
        publish_time_utc = context.user_data.get('publish_time')
        explanation = quiz_data.get('explanation') if quiz_data else None

        if not quiz_data or not hashtag or not file_id or not publish_time_utc:
            await query.edit_message_text("❌ Ошибка. Начни заново через /quiz")
            context.user_data.clear()
            return

        save_scheduled(
            chat_id,
            username,
            quiz_data['question'],
            '|||'.join(quiz_data['options']),
            quiz_data['correct_option_id'],
            hashtag,
            file_id,
            publish_time_utc,
            explanation
        )

        msk_time = utc_to_msk(publish_time_utc).strftime('%d.%m.%Y в %H:%M')
        delay = int((publish_time_utc - now_utc()).total_seconds())
        explanation_line = f"\n📖 Объяснение: {explanation[:50]}..." if explanation else "\n📖 Без объяснения"
        await query.edit_message_text(
            f"✅ Викторина запланирована на **{msk_time}** МСК!\n"
            f"⏳ Осталось: {delay} сек\n"
            f"🏷️ {hashtag}"
            f"{explanation_line}\n"
            "📋 /my — посмотреть все"
        )
        context.user_data.clear()
        return

    if data == "publish_now":
        quiz_data = context.user_data.get('quiz_data')
        hashtag = context.user_data.get('quiz_hashtag')
        file_id = context.user_data.get('file_id')
        explanation = quiz_data.get('explanation') if quiz_data else None

        if not quiz_data or not hashtag or not file_id:
            await query.edit_message_text("❌ Ошибка. Начни заново через /quiz")
            context.user_data.clear()
            return

        await query.edit_message_text("📤 Публикую викторину сейчас...")

        try:
            caption = f"Викторина\n{hashtag}\n\n<a href=\"{SUGGESTION_LINK}\">ТрясЛо №993 | Скинуть что-нибудь в предложку</a>"
            url_photo = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
            requests.post(url_photo, data={
                "chat_id": CHANNEL_ID,
                "photo": file_id,
                "caption": caption,
                "parse_mode": "HTML"
            })
            url_poll = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPoll"
            poll_data = {
                "chat_id": CHANNEL_ID,
                "question": quiz_data['question'],
                "options": quiz_data['options'],
                "type": "quiz",
                "correct_option_id": quiz_data['correct_option_id'],
                "is_anonymous": True
            }
            if explanation:
                poll_data["explanation"] = explanation[:200]
                poll_data["explanation_parse_mode"] = "HTML"
            resp = requests.post(url_poll, json=poll_data)

            if resp.json().get('ok'):
                conn = sqlite3.connect(QUIZZES_DB)
                c = conn.cursor()
                c.execute('''
                    INSERT INTO quizzes (question, options, correct_option_id, hashtag, date)
                    VALUES (?, ?, ?, ?, ?)
                ''', (quiz_data['question'], '|||'.join(quiz_data['options']), quiz_data['correct_option_id'], hashtag, now_utc().isoformat()))
                conn.commit()
                conn.close()
                await query.edit_message_text(
                    f"✅ Викторина ОПУБЛИКОВАНА!\n\n"
                    f"❓ {quiz_data['question']}\n"
                    f"🏷️ {hashtag}"
                )
            else:
                await query.edit_message_text(f"❌ Ошибка: {resp.json()}")
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")
        context.user_data.clear()
        return

    if data == "cancel_publish":
        await query.edit_message_text("❌ Отменено.")
        context.user_data.clear()
        return

    await query.edit_message_text("❌ Неизвестная команда.")

# --- КАРТИНКА ДЛЯ ВИКТОРИНЫ ---
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('step') != 'waiting_for_image':
        return
    if not update.message.photo:
        await update.message.reply_text("❌ Отправь именно картинку")
        return
    photo = update.message.photo[-1]
    context.user_data['file_id'] = photo.file_id
    hashtag = context.user_data.get('quiz_hashtag')
    quiz_data = context.user_data.get('quiz_data')
    caption = f"Викторина\n{hashtag}\n\n<a href=\"{SUGGESTION_LINK}\">ТрясЛо №993 | Скинуть что-нибудь в предложку</a>"
    context.user_data['caption'] = caption
    keyboard = [
        [InlineKeyboardButton("✅ Опубликовать сейчас", callback_data="publish_now")],
        [InlineKeyboardButton("⏰ Запланировать на время", callback_data="schedule")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_publish")]
    ]
    explanation = quiz_data.get('explanation') if quiz_data else None
    explanation_line = f"\n📖 Объяснение: {explanation[:60]}..." if explanation else "\n📖 Без объяснения"
    await update.message.reply_text(
        f"🖼️ Картинка сохранена!\n\n"
        f"❓ {quiz_data['question'] if quiz_data else '?'}\n"
        f"🏷️ {hashtag}"
        f"{explanation_line}\n\n"
        "Что делаем?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get('step')
    if step == 'waiting_for_image':
        if not update.message.photo:
            await update.message.reply_text("❌ Для викторины нужна именно картинка (фото).")
            return
        await handle_image(update, context)
        return
    if step == 'waiting_for_meme_media':
        if not update.message.photo and not update.message.video:
            await update.message.reply_text("❌ Для мема нужна картинка или видео.")
            return
        await handle_meme_media(update, context)
        return
    await update.message.reply_text("❌ Я не жду медиа. Используй /quiz или /meme чтобы начать.")

async def show_memes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('SELECT id, chat_id, publish_time, hashtag FROM memes ORDER BY id DESC LIMIT 10')
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📭 В БД нет мемов.")
        return
    reply = "📋 **Последние 10 мемов в БД:**\n\n"
    for row in rows:
        dt_msk = utc_to_msk(datetime.fromisoformat(row[2]))
        reply += f"🆔 {row[0]}\n"
        reply += f"📅 {dt_msk.strftime('%d.%m %H:%M')} МСК\n"
        reply += f"🏷️ {row[3]}\n"
        reply += f"👤 {row[1]}\n\n"
    await update.message.reply_text(reply)

async def testrem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hour = 17
    minute = 40
    await update.message.reply_text(f"🔍 Проверяю мемы на {hour:02d}:{minute:02d} МСК...")
    existing = get_today_memes_by_time_msk(MEME_ADMIN_ID, hour, minute)
    if existing:
        await update.message.reply_text(f"✅ НАЙДЕНО {len(existing)} МЕМОВ! Напоминалка НЕ должна прийти.")
    else:
        await update.message.reply_text("❌ МЕМОВ НЕ НАЙДЕНО. Напоминалка ПРИДЁТ.")

# --- ЗАПУСК ---
def main():
    init_db()
    init_base_db()

    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()
    print("🔄 Планировщик запущен")

    reminder_thread = threading.Thread(target=reminder_loop, daemon=True)
    reminder_thread.start()
    print("⏰ Напоминалка запущена")

    health_thread = threading.Thread(target=self_health_check, daemon=True)
    health_thread.start()
    print("🩺 Самопроверка запущена (каждые 10 минут)")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", start_quiz))
    app.add_handler(CommandHandler("meme", start_meme))
    app.add_handler(CommandHandler("my", my_quizzes))
    app.add_handler(CommandHandler("mymemes", my_memes))
    app.add_handler(CommandHandler("cancel_all", cancel_all))
    app.add_handler(CommandHandler("cancelallmemes", cancel_all_memes))
    app.add_handler(CommandHandler("cancel", cancel_quiz))
    app.add_handler(CommandHandler("cancelmeme", cancel_meme))
    app.add_handler(CommandHandler("id", get_id))
    app.add_handler(CommandHandler("view", view_command))
    app.add_handler(CommandHandler("viewmeme", view_meme_command))
    app.add_handler(CommandHandler("editquiz", edit_quiz_time))
    app.add_handler(CommandHandler("editmeme", edit_meme_time))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("backupbase", backup_base_command))
    app.add_handler(CommandHandler("testrem", testrem))

    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(CommandHandler("showmemes", show_memes))

    import signal
    print("🤖 Бот запущен!")

    def handle_shutdown(signum, frame):
        print("\n🛑 Получен сигнал остановки (SIGTERM)")
        notify_all_users(BOT_TOKEN, "🛑 Бот остановлен! Обновление или перезапуск. Скоро вернусь.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        app.run_polling()
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен (Ctrl+C)")
        notify_all_users(BOT_TOKEN, "🛑 Бот остановлен! Обновление или перезапуск. Скоро вернусь.")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        notify_all_users(BOT_TOKEN, f"❌ Бот упал с ошибкой: {e}")

if __name__ == "__main__":
    main()
