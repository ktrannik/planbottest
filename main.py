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
    return datetime.now(timezone.utc).replace(tzinfo=None)

def now_msk():
    return datetime.now(timezone.utc).astimezone(MSK)

def utc_to_msk(dt_utc):
    if dt_utc is None:
        return None
    return dt_utc + timedelta(hours=3)

def msk_to_utc(dt_msk):
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

# ============================================================
# АДМИНЫ — полный доступ ко ВСЕМ командам бота.
# Добавляй сюда ID своих админов строками.
# ============================================================
ADMIN_IDS = [
    "5206039766",
    "7384358785",
    "6607609864",
]

HASHTAGS = [
    "#Новое_поколение", "#Игра_бога", "#Идеальный_мир", "#Голос_времени",
    "#Тринадцать_огней", "#Последняя_реальность", "#Сердце_вселенной",
    "#Точка_невозврата", "#Мастерская_47", "#внесезонов"
]

# --- ПРОВЕРКА ПРАВ ---
def is_admin(user_id) -> bool:
    return str(user_id) in [str(x) for x in ADMIN_IDS]


async def require_admin(update_or_query) -> bool:
    if hasattr(update_or_query, "callback_query") and update_or_query.callback_query:
        user = update_or_query.callback_query.from_user
        query = update_or_query.callback_query
        if not is_admin(user.id):
            try:
                await query.answer("⛔ Нет прав", show_alert=True)
            except Exception:
                pass
            return False
        return True

    user = update_or_query.effective_user
    if not is_admin(user.id):
        await update_or_query.message.reply_text(
            "⛔ У тебя нет прав для использования этого бота.\n\n"
            "Бот предназначен только для администраторов канала."
        )
        return False
    return True


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

# --- ВИКТОРИНЫ ---
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

def get_scheduled_by_id(quiz_id):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        SELECT id, chat_id, username, question, publish_time
        FROM scheduled WHERE id = ?
    ''', (quiz_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_full_scheduled(quiz_id):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        SELECT id, chat_id, username, question, options, correct_option_id,
               hashtag, file_id, publish_time, explanation
        FROM scheduled WHERE id = ?
    ''', (quiz_id,))
    row = c.fetchone()
    conn.close()
    return row

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

def update_scheduled_field(chat_id, quiz_id, field, value):
    allowed = {'question', 'options', 'correct_option_id', 'hashtag', 'explanation', 'file_id'}
    if field not in allowed:
        return False
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute(f'UPDATE scheduled SET {field} = ? WHERE chat_id = ? AND id = ?',
              (value, chat_id, quiz_id))
    changed = c.rowcount
    conn.commit()
    conn.close()
    return changed > 0

# --- МЕМЫ ---
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

def get_meme_by_id(meme_id):
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        SELECT id, chat_id, username, file_type, hashtag, publish_time
        FROM memes WHERE id = ?
    ''', (meme_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_full_meme(meme_id):
    """Полная запись мема: id, chat_id, username, file_id, file_type, hashtag, post_text, publish_time."""
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute('''
        SELECT id, chat_id, username, file_id, file_type, hashtag, post_text, publish_time
        FROM memes WHERE id = ?
    ''', (meme_id,))
    row = c.fetchone()
    conn.close()
    return row

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

def update_meme_field(chat_id, meme_id, field, value):
    """
    Обновляет одно поле мема.
    field: 'hashtag' | 'post_text' | 'file_id'
    """
    allowed = {'hashtag', 'post_text', 'file_id'}
    if field not in allowed:
        return False
    conn = sqlite3.connect(QUIZZES_DB)
    c = conn.cursor()
    c.execute(f'UPDATE memes SET {field} = ? WHERE chat_id = ? AND id = ?',
              (value, chat_id, meme_id))
    changed = c.rowcount
    conn.commit()
    conn.close()
    return changed > 0

def find_user_chat_id(target):
    target = target.strip()

    if target.startswith('@'):
        username = target[1:]
        conn = sqlite3.connect(QUIZZES_DB)
        c = conn.cursor()
        c.execute('SELECT chat_id, username FROM users WHERE username = ? LIMIT 1', (username,))
        row = c.fetchone()
        if not row:
            c.execute('SELECT chat_id, username FROM scheduled WHERE username = ? LIMIT 1', (username,))
            row = c.fetchone()
        if not row:
            c.execute('SELECT chat_id, username FROM memes WHERE username = ? LIMIT 1', (username,))
            row = c.fetchone()
        conn.close()
        if row:
            return row[0], row[1]
        return None, None

    if target.lstrip('-').isdigit():
        chat_id = target
        conn = sqlite3.connect(QUIZZES_DB)
        c = conn.cursor()
        c.execute('SELECT chat_id, username FROM users WHERE chat_id = ? LIMIT 1', (chat_id,))
        row = c.fetchone()
        if not row:
            c.execute('SELECT chat_id, username FROM scheduled WHERE chat_id = ? LIMIT 1', (chat_id,))
            row = c.fetchone()
        if not row:
            c.execute('SELECT chat_id, username FROM memes WHERE chat_id = ? LIMIT 1', (chat_id,))
            row = c.fetchone()
        conn.close()
        if row:
            return row[0], row[1]
        return chat_id, None

    return None, None

# --- НАПОМИНАЛКА ---
def get_today_memes_by_time_msk(chat_id, target_hour_msk, target_minute_msk):
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

# --- ПЛАНИРОВЩИК ---
def scheduler_loop():
    while True:
        try:
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
    now_msk_dt = now_msk().replace(tzinfo=None)

    match = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})', text)
    if match:
        day, month, year, hour, minute = map(int, match.groups())
        dt_msk = datetime(year, month, day, hour, minute)
        return msk_to_utc(dt_msk)

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

    match = re.search(r'(\d{1,2}):(\d{2})', text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        dt_msk = now_msk_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt_msk < now_msk_dt:
            dt_msk = dt_msk + timedelta(days=1)
        return msk_to_utc(dt_msk)

    return None

def parse_quiz(text):
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

# --- КОМАНДЫ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text(
            "⛔ У тебя нет прав для использования этого бота.\n\n"
            "Бот предназначен только для администраторов канала."
        )
        return

    await update.message.reply_text(
        "👋 Бот для викторин и мемов (только для админов)\n\n"
        "📝 /quiz — создать викторину\n"
        "🖼️ /meme — создать мем\n"
        "📋 /my — мои запланированные викторины\n"
        "📋 /mymemes — мои запланированные мемы\n"
        "✏️ /editquiz <id> — редактировать свою викторину\n"
        "✏️ /editquiz @username <id> — редактировать чужую\n"
        "✏️ /editmeme <id> — редактировать свой мем\n"
        "✏️ /editmeme @username <id> — редактировать чужой\n"
        "🗑️ /cancel_all — отменить все викторины\n"
        "🗑️ /cancelallmemes — отменить все мемы\n"
        "🔍 /view @username — посмотреть викторины пользователя\n"
        "🖼️ /viewmeme @username — посмотреть мемы пользователя\n"
        "🆔 /id — показать свой ID"
    )

async def my_quizzes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
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
    reply += "✏️ `/editquiz <id>` — редактировать"
    await update.message.reply_text(reply)

async def cancel_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
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

async def cancel_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    chat_id = str(update.effective_user.id)
    delete_user_scheduled(chat_id)
    await update.message.reply_text("✅ Все викторины отменены.")

async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
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
    if not await require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("❌ Укажи: `/view @username` или `/view 123456789`")
        return

    target = context.args[0]

    if target.startswith('@'):
        username = target[1:]
        scheduled = get_user_scheduled_by_username(username)
        if not scheduled:
            await update.message.reply_text(f"📭 У @{username} нет викторин.")
            return
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

    if target.isdigit():
        scheduled = get_user_scheduled_by_chat_id(target)
        if not scheduled:
            await update.message.reply_text(f"📭 У `{target}` нет викторин.")
            return
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
    if not await require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("❌ Укажи: `/viewmeme @username` или `/viewmeme 123456789`")
        return

    target = context.args[0]

    if target.startswith('@'):
        username = target[1:]
        memes = get_user_memes_by_username(username)
        if not memes:
            await update.message.reply_text(f"📭 У @{username} нет мемов.")
            return
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

    if target.isdigit():
        memes = get_user_memes_by_chat_id(target)
        if not memes:
            await update.message.reply_text(f"📭 У `{target}` нет мемов.")
            return
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

# --- РЕДАКТИРОВАНИЕ ВИКТОРИН ---
async def edit_quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    chat_id = str(update.effective_user.id)
    args = context.args

    if not args:
        await update.message.reply_text(
            "❌ Использование:\n"
            "`/editquiz <id>` — своя викторина\n"
            "`/editquiz @username <id>` — чужая по юзернейму\n"
            "`/editquiz 123456789 <id>` — чужая по ID\n\n"
            "Посмотреть ID: /my или /view @username"
        )
        return

    if len(args) == 1:
        target_chat_id = chat_id
        try:
            quiz_id = int(args[0])
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом.")
            return

    elif len(args) == 2:
        target = args[0]
        try:
            quiz_id = int(args[1])
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом.")
            return

        found_chat_id, found_username = find_user_chat_id(target)
        if not found_chat_id:
            await update.message.reply_text(f"❌ Пользователь `{target}` не найден.")
            return
        target_chat_id = found_chat_id
    else:
        await update.message.reply_text("❌ Слишком много аргументов.")
        return

    quiz = get_full_scheduled(quiz_id)
    if not quiz:
        await update.message.reply_text(f"❌ Викторина #{quiz_id} не найдена в БД.")
        return

    real_chat_id = quiz[1]
    real_username = quiz[2]

    if len(args) == 2 and real_chat_id != target_chat_id:
        await update.message.reply_text(
            f"❌ Викторина #{quiz_id} принадлежит @{real_username or real_chat_id}."
        )
        return

    if len(args) == 1 and real_chat_id != chat_id:
        await update.message.reply_text(
            f"❌ Викторина #{quiz_id} не твоя. Используй: `/editquiz @{real_username} {quiz_id}`"
        )
        return

    await show_quiz_edit_menu(update, context, quiz_id)


async def show_quiz_edit_menu(update_or_query, context, quiz_id, edit=False):
    quiz = get_full_scheduled(quiz_id)
    if not quiz:
        text = f"❌ Викторина #{quiz_id} не найдена."
        if edit:
            await update_or_query.edit_message_text(text)
        else:
            await update_or_query.message.reply_text(text)
        return

    options_list = quiz[4].split('|||') if quiz[4] else []
    correct_idx = quiz[5]
    correct_text = options_list[correct_idx] if 0 <= correct_idx < len(options_list) else "?"
    dt_msk = utc_to_msk(datetime.fromisoformat(quiz[8]))
    explanation = quiz[9] or "—"

    text = (
        f"✏️ **Редактирование викторины #{quiz_id}**\n"
        f"👤 Владелец: @{quiz[2] or quiz[1]}\n\n"
        f"❓ **Вопрос:** {quiz[3]}\n"
        f"📝 **Варианты:** {', '.join(options_list)}\n"
        f"✅ **Правильный:** {correct_text}\n"
        f"📖 **Объяснение:** {explanation[:80]}\n"
        f"🏷️ **Хэштег:** {quiz[6]}\n"
        f"📅 **Время:** {dt_msk.strftime('%d.%m.%Y %H:%M')} МСК\n\n"
        f"Что редактируем?"
    )

    keyboard = [
        [InlineKeyboardButton("❓ Вопрос", callback_data=f"qedit_question_{quiz_id}"),
         InlineKeyboardButton("📝 Варианты и ответ", callback_data=f"qedit_options_{quiz_id}")],
        [InlineKeyboardButton("📖 Объяснение", callback_data=f"qedit_explanation_{quiz_id}"),
         InlineKeyboardButton("🏷️ Хэштег", callback_data=f"qedit_hashtag_{quiz_id}")],
        [InlineKeyboardButton("🖼️ Картинка", callback_data=f"qedit_picture_{quiz_id}"),
         InlineKeyboardButton("📅 Время", callback_data=f"qedit_time_{quiz_id}")],
        [InlineKeyboardButton("❌ Закрыть", callback_data=f"qedit_close_{quiz_id}")],
    ]

    if edit:
        await update_or_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        if quiz[7]:
            try:
                await update_or_query.message.reply_photo(
                    photo=quiz[7],
                    caption=f"🖼️ Текущая картинка викторины #{quiz_id}"
                )
            except Exception as e:
                print(f"⚠️ Не удалось отправить картинку: {e}")
                await update_or_query.message.reply_text("⚠️ Не удалось показать текущую картинку.")
        await update_or_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# --- РЕДАКТИРОВАНИЕ МЕМОВ ---
async def edit_meme_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /editmeme <id>              — редактировать свой
    /editmeme @username <id>    — редактировать чужой по юзернейму
    /editmeme 123456789 <id>    — редактировать чужой по chat_id
    """
    if not await require_admin(update):
        return

    chat_id = str(update.effective_user.id)
    args = context.args

    if not args:
        await update.message.reply_text(
            "❌ Использование:\n"
            "`/editmeme <id>` — свой мем\n"
            "`/editmeme @username <id>` — чужой по юзернейму\n"
            "`/editmeme 123456789 <id>` — чужой по ID\n\n"
            "Посмотреть ID: /mymemes или /viewmeme @username"
        )
        return

    if len(args) == 1:
        target_chat_id = chat_id
        try:
            meme_id = int(args[0])
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом.")
            return

    elif len(args) == 2:
        target = args[0]
        try:
            meme_id = int(args[1])
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом.")
            return

        found_chat_id, found_username = find_user_chat_id(target)
        if not found_chat_id:
            await update.message.reply_text(f"❌ Пользователь `{target}` не найден.")
            return
        target_chat_id = found_chat_id
    else:
        await update.message.reply_text("❌ Слишком много аргументов.")
        return

    meme = get_full_meme(meme_id)
    if not meme:
        await update.message.reply_text(f"❌ Мем #{meme_id} не найден в БД.")
        return

    real_chat_id = meme[1]
    real_username = meme[2]

    if len(args) == 2 and real_chat_id != target_chat_id:
        await update.message.reply_text(
            f"❌ Мем #{meme_id} принадлежит @{real_username or real_chat_id}."
        )
        return

    if len(args) == 1 and real_chat_id != chat_id:
        await update.message.reply_text(
            f"❌ Мем #{meme_id} не твой. Используй: `/editmeme @{real_username} {meme_id}`"
        )
        return

    await show_meme_edit_menu(update, context, meme_id)


async def show_meme_edit_menu(update_or_query, context, meme_id, edit=False):
    """
    Показывает картинку/видео мема + меню редактирования.
    """
    meme = get_full_meme(meme_id)
    if not meme:
        text = f"❌ Мем #{meme_id} не найден."
        if edit:
            await update_or_query.edit_message_text(text)
        else:
            await update_or_query.message.reply_text(text)
        return

    # meme = (id, chat_id, username, file_id, file_type, hashtag, post_text, publish_time)
    file_id = meme[3]
    file_type = meme[4]
    hashtag = meme[5]
    post_text = meme[6] or "—"
    dt_msk = utc_to_msk(datetime.fromisoformat(meme[7]))

    text = (
        f"✏️ **Редактирование мема #{meme_id}**\n"
        f"👤 Владелец: @{meme[2] or meme[1]}\n\n"
        f"🖼️ **Тип:** {file_type}\n"
        f"🏷️ **Хэштег:** {hashtag}\n"
        f"📝 **Текст:** {post_text[:100] if post_text else '—'}\n"
        f"📅 **Время:** {dt_msk.strftime('%d.%m.%Y %H:%M')} МСК\n\n"
        f"Что редактируем?"
    )

    keyboard = [
        [InlineKeyboardButton("🖼️ Картинка/видео", callback_data=f"medit_picture_{meme_id}"),
         InlineKeyboardButton("🏷️ Хэштег", callback_data=f"medit_hashtag_{meme_id}")],
        [InlineKeyboardButton("📝 Текст", callback_data=f"medit_text_{meme_id}"),
         InlineKeyboardButton("📅 Время", callback_data=f"medit_time_{meme_id}")],
        [InlineKeyboardButton("❌ Закрыть", callback_data=f"medit_close_{meme_id}")],
    ]

    if edit:
        await update_or_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        # Сначала шлём медиа, потом текст
        try:
            if file_type == 'photo':
                await update_or_query.message.reply_photo(
                    photo=file_id,
                    caption=f"🖼️ Текущая картинка мема #{meme_id}"
                )
            else:
                await update_or_query.message.reply_video(
                    video=file_id,
                    caption=f"🎬 Текущее видео мема #{meme_id}"
                )
        except Exception as e:
            print(f"⚠️ Не удалось отправить медиа: {e}")
            await update_or_query.message.reply_text("⚠️ Не удалось показать текущее медиа.")
        await update_or_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# --- СОЗДАНИЕ МЕМОВ ---
async def start_meme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
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
    if not await require_admin(update):
        return
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
    reply += "✏️ `/editmeme <id>` — редактировать"
    await update.message.reply_text(reply)

async def cancel_meme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
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

async def cancel_all_memes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    chat_id = str(update.effective_user.id)
    delete_user_memes(chat_id)
    await update.message.reply_text("✅ Все мемы отменены.")

# --- БЭКАПЫ ---
async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
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
    if not await require_admin(update):
        return
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

# --- ОСНОВНОЙ ОБРАБОТЧИК ТЕКСТА ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        return

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

    # --- Редактирование полей викторины ---
    if step == 'waiting_for_edit_quiz_question':
        quiz_id = context.user_data.get('edit_quiz_id')
        owner_chat_id = context.user_data.get('edit_quiz_chat_id')
        new_question = text.strip()
        if not new_question:
            await update.message.reply_text("❌ Вопрос не может быть пустым.")
            return
        ok = update_scheduled_field(owner_chat_id, quiz_id, 'question', new_question)
        if ok:
            await update.message.reply_text(f"✅ Вопрос обновлён: {new_question}")
        else:
            await update.message.reply_text("❌ Не удалось обновить.")
        context.user_data.clear()
        await show_quiz_edit_menu(update, context, quiz_id)
        return

    if step == 'waiting_for_edit_quiz_options':
        quiz_id = context.user_data.get('edit_quiz_id')
        owner_chat_id = context.user_data.get('edit_quiz_chat_id')

        parts = [p.strip() for p in text.split(';') if p.strip()]
        if len(parts) < 2:
            await update.message.reply_text("❌ Нужно минимум 2 варианта через `;`")
            return

        correct_idx = None
        cleaned = []
        for i, p in enumerate(parts):
            if p.endswith('*'):
                correct_idx = i
                cleaned.append(p[:-1].strip())
            else:
                cleaned.append(p)

        if correct_idx is None:
            await update.message.reply_text("❌ Отметь правильный ответ `*` в конце варианта.")
            return

        if len(cleaned) > 10:
            await update.message.reply_text("❌ Телеграм разрешает максимум 10 вариантов.")
            return

        conn = sqlite3.connect(QUIZZES_DB)
        c = conn.cursor()
        c.execute('''
            UPDATE scheduled SET options = ?, correct_option_id = ?
            WHERE chat_id = ? AND id = ?
        ''', ('|||'.join(cleaned), correct_idx, owner_chat_id, quiz_id))
        ok = c.rowcount > 0
        conn.commit()
        conn.close()

        if ok:
            await update.message.reply_text(
                f"✅ Варианты обновлены!\n"
                f"📝 {', '.join(cleaned)}\n"
                f"✅ Правильный: {cleaned[correct_idx]}"
            )
        else:
            await update.message.reply_text("❌ Не удалось обновить.")
        context.user_data.clear()
        await show_quiz_edit_menu(update, context, quiz_id)
        return

    if step == 'waiting_for_edit_quiz_explanation':
        quiz_id = context.user_data.get('edit_quiz_id')
        owner_chat_id = context.user_data.get('edit_quiz_chat_id')
        explanation = text.strip()
        if explanation.lower() in ('-', 'нет', 'нет.', 'удалить'):
            explanation = None
        if explanation and len(explanation) > 200:
            await update.message.reply_text("❌ Максимум 200 символов (ограничение Telegram).")
            return
        ok = update_scheduled_field(owner_chat_id, quiz_id, 'explanation', explanation)
        if ok:
            await update.message.reply_text(f"✅ Объяснение обновлено: {explanation or '— (удалено)'}")
        else:
            await update.message.reply_text("❌ Не удалось обновить.")
        context.user_data.clear()
        await show_quiz_edit_menu(update, context, quiz_id)
        return

    if step == 'waiting_for_edit_quiz_hashtag':
        quiz_id = context.user_data.get('edit_quiz_id')
        owner_chat_id = context.user_data.get('edit_quiz_chat_id')
        new_hashtag = text.strip()
        if not new_hashtag.startswith('#'):
            new_hashtag = '#' + new_hashtag
        ok = update_scheduled_field(owner_chat_id, quiz_id, 'hashtag', new_hashtag)
        if ok:
            await update.message.reply_text(f"✅ Хэштег обновлён: {new_hashtag}")
        else:
            await update.message.reply_text("❌ Не удалось обновить.")
        context.user_data.clear()
        await show_quiz_edit_menu(update, context, quiz_id)
        return

    if step == 'waiting_for_edit_quiz_time':
        dt_utc = parse_datetime(text)
        if dt_utc is None:
            await update.message.reply_text("❌ Не понял формат. Пример: `20:33` или `08.07 20:33`")
            return
        if dt_utc < now_utc():
            await update.message.reply_text("❌ Время уже прошло! Укажи будущее время.")
            return

        quiz_id = context.user_data.get('edit_quiz_id')
        owner_chat_id = context.user_data.get('edit_quiz_chat_id')
        ok = update_scheduled_time(owner_chat_id, quiz_id, dt_utc)

        if ok:
            msk_time = utc_to_msk(dt_utc).strftime('%d.%m.%Y в %H:%M')
            await update.message.reply_text(f"✅ Время викторины #{quiz_id} изменено на **{msk_time}** МСК!")
        else:
            await update.message.reply_text("❌ Не удалось обновить.")
        context.user_data.clear()
        await show_quiz_edit_menu(update, context, quiz_id)
        return

    # --- Редактирование полей мема ---
    if step == 'waiting_for_edit_meme_hashtag':
        meme_id = context.user_data.get('edit_meme_id')
        owner_chat_id = context.user_data.get('edit_meme_chat_id')
        new_hashtag = text.strip()
        if not new_hashtag.startswith('#'):
            new_hashtag = '#' + new_hashtag
        ok = update_meme_field(owner_chat_id, meme_id, 'hashtag', new_hashtag)
        if ok:
            await update.message.reply_text(f"✅ Хэштег обновлён: {new_hashtag}")
        else:
            await update.message.reply_text("❌ Не удалось обновить.")
        context.user_data.clear()
        await show_meme_edit_menu(update, context, meme_id)
        return

    if step == 'waiting_for_edit_meme_text':
        meme_id = context.user_data.get('edit_meme_id')
        owner_chat_id = context.user_data.get('edit_meme_chat_id')
        new_text = text.strip()
        if new_text.lower() in ('-', 'нет', 'нет.', 'удалить', 'без текста'):
            new_text = None
        ok = update_meme_field(owner_chat_id, meme_id, 'post_text', new_text)
        if ok:
            await update.message.reply_text(f"✅ Текст обновлён: {new_text or '— (удалён)'}")
        else:
            await update.message.reply_text("❌ Не удалось обновить.")
        context.user_data.clear()
        await show_meme_edit_menu(update, context, meme_id)
        return

    if step == 'waiting_for_edit_meme_time':
        dt_utc = parse_datetime(text)
        if dt_utc is None:
            await update.message.reply_text("❌ Не понял формат. Пример: `20:33` или `08.07 20:33`")
            return
        if dt_utc < now_utc():
            await update.message.reply_text("❌ Время уже прошло! Укажи будущее время.")
            return

        meme_id = context.user_data.get('edit_meme_id')
        owner_chat_id = context.user_data.get('edit_meme_chat_id')
        ok = update_meme_time(owner_chat_id, meme_id, dt_utc)

        if ok:
            msk_time = utc_to_msk(dt_utc).strftime('%d.%m.%Y в %H:%M')
            await update.message.reply_text(f"✅ Время мема #{meme_id} изменено на **{msk_time}** МСК!")
        else:
            await update.message.reply_text("❌ Не удалось обновить. Возможно, запись уже удалена.")
        context.user_data.clear()
        await show_meme_edit_menu(update, context, meme_id)
        return

    # --- Создание мема ---
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

    # --- Создание викторины ---
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
        "/editquiz <id> — редактировать викторину\n"
        "/editmeme <id> — редактировать мем\n"
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
    user_id = query.from_user.id
    print(f"🔘 Нажата кнопка: {data} от user_id={user_id}")

    if not is_admin(user_id):
        try:
            await query.edit_message_text("⛔ У тебя нет прав.")
        except Exception:
            pass
        return

    # --- Редактирование викторины через кнопки ---
    if data.startswith("qedit_"):
        parts = data.split("_")
        field = parts[1]
        quiz_id = int(parts[2])

        quiz = get_full_scheduled(quiz_id)
        if not quiz:
            await query.edit_message_text(f"❌ Викторина #{quiz_id} не найдена.")
            return

        owner_chat_id = quiz[1]
        context.user_data['edit_quiz_id'] = quiz_id
        context.user_data['edit_quiz_chat_id'] = owner_chat_id

        if field == "close":
            await query.edit_message_text("✅ Редактирование закрыто.")
            context.user_data.clear()
            return

        if field == "question":
            context.user_data['step'] = 'waiting_for_edit_quiz_question'
            await query.edit_message_text(
                f"✏️ **Новый вопрос** для #{quiz_id}:\n\n"
                f"Текущий: {quiz[3]}\n\n"
                "Напиши новый вопрос:"
            )
            return

        if field == "options":
            context.user_data['step'] = 'waiting_for_edit_quiz_options'
            options_list = quiz[4].split('|||') if quiz[4] else []
            await query.edit_message_text(
                f"✏️ **Новые варианты** для #{quiz_id}:\n\n"
                f"Текущие: {', '.join(options_list)}\n\n"
                "Напиши новые варианты через `;`.\n"
                "Правильный отметь `*` в конце.\n\n"
                "Пример:\n"
                "`Вася; Петя*; Коля; Женя`"
            )
            return

        if field == "explanation":
            context.user_data['step'] = 'waiting_for_edit_quiz_explanation'
            current = quiz[9] or "—"
            await query.edit_message_text(
                f"✏️ **Новое объяснение** для #{quiz_id}:\n\n"
                f"Текущее: {current}\n\n"
                "Напиши новое (макс. 200 символов).\n"
                "Или напиши `-` чтобы удалить."
            )
            return

        if field == "hashtag":
            context.user_data['step'] = 'waiting_for_edit_quiz_hashtag'
            await query.edit_message_text(
                f"✏️ **Новый хэштег** для #{quiz_id}:\n\n"
                f"Текущий: {quiz[6]}\n\n"
                "Напиши новый хэштег:"
            )
            return

        if field == "picture":
            context.user_data['step'] = 'waiting_for_edit_quiz_picture'
            await query.edit_message_text(
                f"🖼️ **Новая картинка** для #{quiz_id}:\n\n"
                "Отправь новую картинку (фото) для этой викторины."
            )
            return

        if field == "time":
            context.user_data['step'] = 'waiting_for_edit_quiz_time'
            dt_msk = utc_to_msk(datetime.fromisoformat(quiz[8]))
            await query.edit_message_text(
                f"✏️ **Новое время** для #{quiz_id}:\n\n"
                f"Текущее: {dt_msk.strftime('%d.%m.%Y %H:%M')} МСК\n\n"
                "Напиши новое время (МСК):\n"
                "`20:33` или `08.07 20:33`"
            )
            return

        return

    # --- Редактирование мема через кнопки ---
    if data.startswith("medit_"):
        parts = data.split("_")
        field = parts[1]
        meme_id = int(parts[2])

        meme = get_full_meme(meme_id)
        if not meme:
            await query.edit_message_text(f"❌ Мем #{meme_id} не найден.")
            return

        # meme = (id, chat_id, username, file_id, file_type, hashtag, post_text, publish_time)
        owner_chat_id = meme[1]
        context.user_data['edit_meme_id'] = meme_id
        context.user_data['edit_meme_chat_id'] = owner_chat_id

        if field == "close":
            await query.edit_message_text("✅ Редактирование закрыто.")
            context.user_data.clear()
            return

        if field == "picture":
            context.user_data['step'] = 'waiting_for_edit_meme_picture'
            await query.edit_message_text(
                f"🖼️ **Новое медиа** для мема #{meme_id}:\n\n"
                "Отправь новую картинку или видео.\n"
                "Тип заменится автоматически."
            )
            return

        if field == "hashtag":
            context.user_data['step'] = 'waiting_for_edit_meme_hashtag'
            await query.edit_message_text(
                f"✏️ **Новый хэштег** для мема #{meme_id}:\n\n"
                f"Текущий: {meme[5]}\n\n"
                "Напиши новый хэштег (можно без `#`):"
            )
            return

        if field == "text":
            context.user_data['step'] = 'waiting_for_edit_meme_text'
            current = meme[6] or "—"
            await query.edit_message_text(
                f"✏️ **Новый текст** для мема #{meme_id}:\n\n"
                f"Текущий: {current}\n\n"
                "Напиши новый текст.\n"
                "Или напиши `-` чтобы удалить текст."
            )
            return

        if field == "time":
            context.user_data['step'] = 'waiting_for_edit_meme_time'
            dt_msk = utc_to_msk(datetime.fromisoformat(meme[7]))
            await query.edit_message_text(
                f"✏️ **Новое время** для мема #{meme_id}:\n\n"
                f"Текущее: {dt_msk.strftime('%d.%m.%Y %H:%M')} МСК\n\n"
                "Напиши новое время (МСК):\n"
                "`20:33` или `08.07 20:33`"
            )
            return

        return

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

# --- КАРТИНКА ДЛЯ НОВОЙ ВИКТОРИНЫ ---
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
    user_id = update.effective_user.id

    if not is_admin(user_id):
        return

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

    # --- Редактирование картинки викторины ---
    if step == 'waiting_for_edit_quiz_picture':
        if not update.message.photo:
            await update.message.reply_text("❌ Для викторины нужна именно картинка (фото).")
            return
        photo = update.message.photo[-1]
        quiz_id = context.user_data.get('edit_quiz_id')
        owner_chat_id = context.user_data.get('edit_quiz_chat_id')
        ok = update_scheduled_field(owner_chat_id, quiz_id, 'file_id', photo.file_id)
        if ok:
            await update.message.reply_text(f"✅ Картинка викторины #{quiz_id} обновлена!")
        else:
            await update.message.reply_text("❌ Не удалось обновить картинку.")
        context.user_data.clear()
        await show_quiz_edit_menu(update, context, quiz_id)
        return

    # --- Редактирование медиа мема ---
    if step == 'waiting_for_edit_meme_picture':
        if not update.message.photo and not update.message.video:
            await update.message.reply_text("❌ Нужна картинка или видео.")
            return

        meme_id = context.user_data.get('edit_meme_id')
        owner_chat_id = context.user_data.get('edit_meme_chat_id')

        if update.message.photo:
            new_file_id = update.message.photo[-1].file_id
            new_file_type = 'photo'
        else:
            new_file_id = update.message.video.file_id
            new_file_type = 'video'

        conn = sqlite3.connect(QUIZZES_DB)
        c = conn.cursor()
        c.execute('''
            UPDATE memes SET file_id = ?, file_type = ?
            WHERE chat_id = ? AND id = ?
        ''', (new_file_id, new_file_type, owner_chat_id, meme_id))
        ok = c.rowcount > 0
        conn.commit()
        conn.close()

        if ok:
            await update.message.reply_text(f"✅ Медиа мема #{meme_id} обновлено ({new_file_type}).")
        else:
            await update.message.reply_text("❌ Не удалось обновить медиа.")
        context.user_data.clear()
        await show_meme_edit_menu(update, context, meme_id)
        return

    await update.message.reply_text("❌ Я не жду медиа. Используй /quiz или /meme чтобы начать.")

async def show_memes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
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
    app.add_handler(CommandHandler("editquiz", edit_quiz_command))
    app.add_handler(CommandHandler("editmeme", edit_meme_command))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("backupbase", backup_base_command))
    app.add_handler(CommandHandler("showmemes", show_memes))

    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))

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
