import os
import uuid
import hmac
import time
import base64
import re
import json
import shutil
import sqlite3
import hashlib
import threading
import urllib.parse
import telebot
import requests
import urllib3
from hashlib import sha256
from flask import Flask, request, jsonify

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BOT_TOKEN = os.getenv('BOT_TOKEN')
YOOMONEY_RECEIVER = os.getenv('YOOMONEY_RECEIVER')
YOOMONEY_SECRET = os.getenv('YOOMONEY_SECRET')
GIGACHAT_AUTH_KEY = os.getenv('GIGACHAT_AUTH_KEY')
BOTHUB_API_KEY = os.getenv('BOTHUB_API_KEY')

ADMIN_ID = 8000630493
ERRORS_CHANNEL_ID = int(os.getenv('ERRORS_CHANNEL_ID', '0'))

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

DB_PATH = "/app/data/users.db"
BACKUP_DIR = "/app/data/backups"
TRIAL_PRICE = 50
TRIAL_TOKENS = 10
TRIAL_WINDOW = 3600
IMAGE_COST = 4
EDIT_COST = 4
MIN_CUSTOM_TOKENS = 20
MAX_CUSTOM_TOKENS = 3000
ORDER_TTL = 1200
BACKUP_INTERVAL = 3600
BACKUP_KEEP = 24
WEBAPP_URL = "https://bot-1790959533-7739-maks746395.bothost.tech/webapp/"
PAY_BASE_URL = "https://bot-1790959533-7739-maks746395.bothost.tech"

last_broadcast = {"messages": [], "active": False}
pending_broadcast = {"active": False, "text": "", "signed": False, "waiting_since": 0}
edit_photos_cache = {}
last_messages = {}
_last_action = {}
_last_message_text = {}
_last_error_notify = {}
_last_error_check = int(time.time())
_last_ai_message = {}
_gigachat_cache = {"token": None, "expires": 0}
_gigachat_lock = threading.Lock()


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_code_response(text):
    if not text:
        return False
    if "```" in text:
        return True
    marks = ["def ", "class ", "function ", "<?php",
             "public static", "#include", "fn main", "package main"]
    for m in marks:
        if m in text:
            return True
    return False


def detect_code_language(text):
    if not text:
        return "txt"
    m = re.search(r"```(\w+)", text)
    if m:
        lang = m.group(1).lower()
        mapping = {
            "python": "py", "py": "py", "javascript": "js", "js": "js", "node": "js",
            "typescript": "ts", "ts": "ts", "cpp": "cpp", "c++": "cpp", "cxx": "cpp",
            "c": "c", "java": "java", "go": "go", "golang": "go",
            "rust": "rs", "rs": "rs", "ruby": "rb", "rb": "rb",
            "php": "php", "html": "html", "css": "css", "sql": "sql",
            "bash": "sh", "sh": "sh", "shell": "sh", "yaml": "yaml", "yml": "yaml",
            "json": "json", "xml": "xml", "markdown": "md", "md": "md",
        }
        return mapping.get(lang, "txt")
    text_lower = text.lower()
    if "def " in text_lower and ":" in text:
        return "py"
    if "function " in text_lower and "=>" in text:
        return "js"
    if "#include" in text_lower:
        return "cpp"
    if "public class" in text_lower:
        return "java"
    if "fn main" in text_lower:
        return "rs"
    return "txt"


def extract_code_block(text):
    if not text:
        return ""
    m = re.search(r"```(?:\w+)?\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    return text


def render_progress_bar(percent, width=22):
    percent = max(0, min(100, percent))
    filled = int(width * percent / 100)
    empty = width - filled
    if filled == 0:
        return "░" * width
    if filled >= width:
        return "█" * width
    fill_part = "█" * max(0, filled - 6)
    remains = filled - len(fill_part)
    gradient_part = ""
    if remains > 0:
        g = "▓▒░"
        for i in range(remains):
            gradient_part += g[i % 3]
    return fill_part + gradient_part + ("░" * empty)


PROGRESS_STAGES = [
    (0, "⏳ Анализирую..."),
    (15, "🎨 Начинаю..."),
    (35, "💎 Рисую основу..."),
    (55, "✨ Добавляю детали..."),
    (75, "🔥 Финал..."),
    (100, "✅ Готово!"),
]


def get_stage_text(percent):
    text = "⏳ Обрабатываю..."
    for p, t in PROGRESS_STAGES:
        if percent >= p:
            text = t
    return text


def paginate(items, page, per_page=10):
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end], page, total_pages, total


def pagination_menu(prefix, page, total_pages, back_to="menu_main"):
    markup = telebot.types.InlineKeyboardMarkup()
    buttons = []
    if page > 1:
        buttons.append(telebot.types.InlineKeyboardButton("⬅️", callback_data=f"{prefix}_page_{page - 1}"))
    buttons.append(telebot.types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        buttons.append(telebot.types.InlineKeyboardButton("➡️", callback_data=f"{prefix}_page_{page + 1}"))
    markup.row(*buttons)
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=back_to))
    return markup


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def log_error(error_msg, chat_id=None, username=None):
    error_str = str(error_msg)[:500]
    now = int(time.time())
    path = "/app/data/errors.log"

    ctx = ""
    if chat_id:
        ctx = f" 👤 chat_id={chat_id}"
        if username:
            ctx += f" ({username})"

    try:
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size > 40 * 1024 * 1024:
                if size < 50 * 1024 * 1024 and ERRORS_CHANNEL_ID != 0:
                    try:
                        with open(path, "rb") as f:
                            data = f.read()
                        filename = f"errors_rotated_{time.strftime('%Y-%m-%d_%H-%M')}.log"
                        days_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
                        day_name = days_ru[time.localtime().tm_wday]
                        caption = (
                            f"🔄 <b>Ротация errors.log</b>\n\n"
                            f"📅 <b>Дата:</b> {time.strftime('%d.%m.%Y')}\n"
                            f"📆 <b>День:</b> {day_name}\n"
                            f"⏱ <b>Время:</b> {time.strftime('%H:%M:%S')}\n"
                            f"🗓 <b>Год:</b> {time.strftime('%Y')}\n\n"
                            f"📦 Размер: <b>{size / 1024 / 1024:.1f} МБ</b>"
                        )
                        bot.send_document(ERRORS_CHANNEL_ID, (filename, data),
                                          caption=caption, parse_mode='HTML')
                    except Exception as e:
                        print(f"Send rotated log error: {e}")
                try:
                    os.remove(path)
                    print(f"Rotated errors.log ({size / 1024 / 1024:.1f} МБ)")
                except Exception as e:
                    print(f"Rotation error: {e}")
    except Exception:
        pass

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]{ctx} {error_str}\n")
    except Exception:
        pass

    if ERRORS_CHANNEL_ID == 0:
        return

    spam_key = f"{error_str}_{chat_id}"
    last = _last_error_notify.get(spam_key, 0)
    if now - last < 60:
        return
    _last_error_notify[spam_key] = now

    if len(_last_error_notify) > 100:
        cutoff = now - 3600
        for k in list(_last_error_notify.keys()):
            if _last_error_notify[k] < cutoff:
                _last_error_notify.pop(k, None)

    days_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    day_name = days_ru[time.localtime().tm_wday]

    text = (
        f"⚠️ <b>Ошибка в боте</b>\n\n"
        f"📅 <b>Дата:</b> {time.strftime('%d.%m.%Y')}\n"
        f"📆 <b>День:</b> {day_name}\n"
        f"⏱ <b>Время:</b> {time.strftime('%H:%M:%S')}\n"
        f"🗓 <b>Год:</b> {time.strftime('%Y')}\n"
    )
    if chat_id:
        text += f"\n👤 <b>Юзер:</b> <code>{chat_id}</code>\n"
        if username:
            text += f"📛 <b>Username:</b> {escape_html(username)}\n"
    text += f"\n📝 <b>Ошибка:</b>\n<code>{escape_html(error_str)}</code>"

    try:
        bot.send_message(ERRORS_CHANNEL_ID, text, parse_mode='HTML')
    except Exception as e:
        print(f"Send error to channel failed: {e}")


def check_spam(chat_id, cooldown=2):
    if chat_id == ADMIN_ID:
        return True
    now = int(time.time())
    last = _last_action.get(chat_id, 0)
    if now - last < cooldown:
        return False
    _last_action[chat_id] = now
    return True


def check_duplicate(chat_id, text, cooldown=5):
    if not text:
        return False
    now = int(time.time())
    last = _last_message_text.get(chat_id)
    if last:
        last_text, last_time = last
        if last_text.lower() == text.lower() and now - last_time < cooldown:
            return True
    _last_message_text[chat_id] = (text, now)
    return False

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    conn = get_conn()
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        tokens INTEGER DEFAULT 0,
        state TEXT DEFAULT 'idle',
        mode TEXT DEFAULT 'regular',
        ai_mode TEXT DEFAULT 'regular',
        trial_started INTEGER DEFAULT 0,
        trial_used INTEGER DEFAULT 0,
        username TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        registered INTEGER DEFAULT 0,
        coder_mode TEXT DEFAULT 'with_hints'
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        tokens INTEGER,
        amount INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        role TEXT,
        content TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS image_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        prompt TEXT,
        image_url TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        tokens_spent INTEGER,
        timestamp INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        username TEXT,
        message TEXT,
        photo_id TEXT,
        status TEXT DEFAULT 'open',
        answer TEXT,
        created INTEGER,
        answered INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS maintenance (
        id INTEGER PRIMARY KEY,
        active INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        reason TEXT,
        admin_id INTEGER,
        timestamp INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS maintenance_notified (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        message_id INTEGER,
        timestamp INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS action_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        username TEXT,
        action_type TEXT,
        action_data TEXT,
        timestamp INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER,
        user_id INTEGER,
        created INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_operations (
        chat_id INTEGER PRIMARY KEY,
        operation TEXT,
        started INTEGER
    )''')
    conn.commit()

    for col, definition in [
        ("mode", "TEXT DEFAULT 'regular'"),
        ("ai_mode", "TEXT DEFAULT 'regular'"),
        ("trial_started", "INTEGER DEFAULT 0"),
        ("trial_used", "INTEGER DEFAULT 0"),
        ("username", "TEXT DEFAULT ''"),
        ("phone", "TEXT DEFAULT ''"),
        ("registered", "INTEGER DEFAULT 0"),
        ("coder_mode", "TEXT DEFAULT 'with_hints'"),
        ("banned", "INTEGER DEFAULT 0"),
        ("ban_reason", "TEXT DEFAULT ''"),
        ("can_image", "INTEGER DEFAULT 1"),
        ("can_chat", "INTEGER DEFAULT 1"),
        ("limit_images", "INTEGER DEFAULT -1"),
        ("limit_chats", "INTEGER DEFAULT -1"),
        ("used_images", "INTEGER DEFAULT 0"),
        ("used_chats", "INTEGER DEFAULT 0"),
        ("send_files", "INTEGER DEFAULT 0"),
        ("used_edits", "INTEGER DEFAULT 0"),
        ("limit_edits", "INTEGER DEFAULT -1"),
        ("support_muted_until", "INTEGER DEFAULT 0"),
        ("ignore_maintenance", "INTEGER DEFAULT 0"),
        ("sound_on", "INTEGER DEFAULT 0"),
    ]:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    for col, definition in [
        ("created", "INTEGER DEFAULT 0"),
        ("paid", "INTEGER DEFAULT 0"),
        ("yoomoney_tx_id", "TEXT DEFAULT ''"),
        ("expires", "INTEGER DEFAULT 0"),
    ]:
        try:
            c.execute(f"ALTER TABLE orders ADD COLUMN {col} {definition}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    for col, definition in [
        ("timestamp", "INTEGER DEFAULT 0"),
        ("kind", "TEXT DEFAULT 'gen'"),
    ]:
        try:
            c.execute(f"ALTER TABLE image_history ADD COLUMN {col} {definition}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    for col, definition in [
        ("message", "TEXT DEFAULT 'Технические работы, пожалуйста подождите'"),
        ("enabled", "INTEGER DEFAULT 0"),
        ("notified", "INTEGER DEFAULT 1"),
    ]:
        try:
            c.execute(f"ALTER TABLE maintenance ADD COLUMN {col} {definition}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    for col, definition in [
        ("reason", "TEXT DEFAULT ''"),
        ("admin_id", "INTEGER DEFAULT 0"),
        ("timestamp", "INTEGER DEFAULT 0"),
    ]:
        try:
            c.execute(f"ALTER TABLE bans ADD COLUMN {col} {definition}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    c.execute("INSERT OR IGNORE INTO maintenance (id, active, message, enabled, notified) VALUES (1, 0, 'Технические работы, пожалуйста подождите', 0, 1)")
    conn.commit()
    conn.close()


init_db()


def get_user(chat_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""SELECT tokens, state, mode, ai_mode, trial_started, trial_used, username,
                 phone, registered, coder_mode, banned, ban_reason, can_image, can_chat,
                 limit_images, limit_chats, used_images, used_chats, send_files,
                 used_edits, limit_edits, support_muted_until, ignore_maintenance, sound_on
                 FROM users WHERE chat_id=?""", (chat_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT OR IGNORE INTO users (chat_id, registered) VALUES (?, ?)", (chat_id, int(time.time())))
        conn.commit()
        c.execute("""SELECT tokens, state, mode, ai_mode, trial_started, trial_used, username,
                     phone, registered, coder_mode, banned, ban_reason, can_image, can_chat,
                     limit_images, limit_chats, used_images, used_chats, send_files,
                     used_edits, limit_edits, support_muted_until, ignore_maintenance, sound_on
                     FROM users WHERE chat_id=?""", (chat_id,))
        row = c.fetchone()
        if not row:
            row = (0, 'idle', 'regular', 'regular', 0, 0, '', '', int(time.time()),
                   'with_hints', 0, '', 1, 1, -1, -1, 0, 0, 0, 0, -1, 0, 0, 0)
    conn.close()
    return row


def update_user(chat_id, field, value):
    conn = get_conn()
    c = conn.cursor()
    c.execute(f"UPDATE users SET {field}=? WHERE chat_id=?", (value, chat_id))
    conn.commit()
    conn.close()


def add_tokens(chat_id, count):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET tokens = tokens + ? WHERE chat_id=?", (count, chat_id))
    conn.commit()
    conn.close()


def inc_used(chat_id, field):
    conn = get_conn()
    c = conn.cursor()
    c.execute(f"UPDATE users SET {field} = {field} + 1 WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def reset_used(chat_id, field=None):
    conn = get_conn()
    c = conn.cursor()
    if field:
        c.execute(f"UPDATE users SET {field} = 0 WHERE chat_id=?", (chat_id,))
    else:
        c.execute("UPDATE users SET used_images = 0, used_chats = 0, used_edits = 0 WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def log_stat(chat_id, tokens_spent):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO stats (chat_id, tokens_spent, timestamp) VALUES (?, ?, ?)",
              (chat_id, tokens_spent, int(time.time())))
    conn.commit()
    conn.close()


def get_total_spent(chat_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(tokens_spent),0) FROM stats WHERE chat_id=?", (chat_id,))
    total = c.fetchone()[0]
    conn.close()
    return total


def log_action(chat_id, action_type, action_data=""):
    try:
        user = get_user(chat_id)
        username = user[6] if user[6] else ""
        conn = get_conn()
        c = conn.cursor()
        c.execute("INSERT INTO action_logs (chat_id, username, action_type, action_data, timestamp) VALUES (?, ?, ?, ?, ?)",
                  (chat_id, username, action_type, action_data[:500], int(time.time())))
        conn.commit()
        conn.close()
    except Exception as e:
        log_error(f"log_action: {e}")


def get_logs(limit=100, user_id=None, action_type=None):
    conn = get_conn()
    c = conn.cursor()
    query = "SELECT chat_id, username, action_type, action_data, timestamp FROM action_logs WHERE 1=1"
    params = []
    if user_id:
        query += " AND chat_id=?"
        params.append(user_id)
    if action_type:
        query += " AND action_type=?"
        params.append(action_type)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return rows


def clear_old_logs(keep=1000):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM action_logs")
    count = c.fetchone()[0]
    if count > keep:
        to_delete = count - keep
        c.execute("DELETE FROM action_logs WHERE id IN (SELECT id FROM action_logs ORDER BY id ASC LIMIT ?)", (to_delete,))
        conn.commit()
    conn.close()


def mark_operation_start(chat_id, op_type):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO active_operations (chat_id, operation, started) VALUES (?, ?, ?)",
              (chat_id, op_type, int(time.time())))
    conn.commit()
    conn.close()


def mark_operation_end(chat_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM active_operations WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def has_active_operations():
    conn = get_conn()
    c = conn.cursor()
    cutoff = int(time.time()) - 300
    c.execute("SELECT COUNT(*) FROM active_operations WHERE started > ?", (cutoff,))
    count = c.fetchone()[0]
    conn.close()
    return count > 0


def is_subscribed(admin_id, user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM subscriptions WHERE admin_id=? AND user_id=?", (admin_id, user_id))
    row = c.fetchone()
    conn.close()
    return row is not None


def subscribe(admin_id, user_id):
    if is_subscribed(admin_id, user_id):
        return
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO subscriptions (admin_id, user_id, created) VALUES (?, ?, ?)",
              (admin_id, user_id, int(time.time())))
    conn.commit()
    conn.close()


def unsubscribe(admin_id, user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM subscriptions WHERE admin_id=? AND user_id=?", (admin_id, user_id))
    conn.commit()
    conn.close()


def get_subscriptions(admin_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM subscriptions WHERE admin_id=?", (admin_id,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def notify_subscribers(user_id, text):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT admin_id FROM subscriptions WHERE user_id=?", (user_id,))
    rows = c.fetchall()
    conn.close()
    for (admin_id,) in rows:
        try:
            bot.send_message(admin_id, f"🔔 <b>Уведомление</b>\n\n{text}", parse_mode='HTML')
        except Exception:
            pass

def save_order(order_id, chat_id, tokens, amount):
    expires = int(time.time()) + ORDER_TTL
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO orders "
        "(order_id, chat_id, tokens, amount, created, paid, yoomoney_tx_id, expires) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, chat_id, tokens, amount, int(time.time()), 0, "", expires)
    )
    conn.commit()
    conn.close()


def mark_order_paid(order_id, tx_id=""):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE orders SET paid=1, yoomoney_tx_id=? WHERE order_id=?", (tx_id, order_id))
    conn.commit()
    conn.close()


def get_order(order_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id, tokens FROM orders WHERE order_id=?", (order_id,))
    row = c.fetchone()
    conn.close()
    return row


def get_all_orders():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT order_id, chat_id, tokens, amount, created, paid, yoomoney_tx_id, expires FROM orders ORDER BY created DESC LIMIT 100")
    rows = c.fetchall()
    conn.close()
    return rows


def get_user_orders_paid(chat_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT order_id, tokens, amount, created, yoomoney_tx_id FROM orders WHERE chat_id=? AND paid=1 ORDER BY created DESC LIMIT 50",
              (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_user_orders_pending(chat_id):
    cutoff = int(time.time())
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM orders WHERE chat_id=? AND paid=0 AND expires > 0 AND expires < ?", (chat_id, cutoff))
    conn.commit()
    c.execute("SELECT order_id, tokens, amount, created, expires FROM orders WHERE chat_id=? AND paid=0 ORDER BY created DESC LIMIT 50",
              (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_user_orders(chat_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT order_id, tokens, amount, created FROM orders WHERE chat_id=? ORDER BY created DESC LIMIT 50",
              (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def add_to_history(chat_id, role, content):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO history (chat_id, role, content) VALUES (?, ?, ?)", (chat_id, role, content))
    conn.commit()
    conn.close()


def get_history(chat_id, limit=20):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit))
    rows = c.fetchall()
    conn.close()
    return list(reversed(rows))


def get_full_history(chat_id, limit=200):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE chat_id=? ORDER BY id ASC LIMIT ?", (chat_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows


def clear_history(chat_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM history WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def add_image_history(chat_id, prompt, image_url, kind="gen"):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO image_history (chat_id, prompt, image_url, timestamp, kind) VALUES (?, ?, ?, ?, ?)",
              (chat_id, prompt, image_url, int(time.time()), kind))
    conn.commit()
    conn.close()


def get_image_history_by_kind(chat_id, kind, limit=50):
    conn = get_conn()
    c = conn.cursor()
    if kind:
        c.execute("SELECT prompt, image_url, timestamp FROM image_history WHERE chat_id=? AND kind=? ORDER BY id DESC LIMIT ?",
                  (chat_id, kind, limit))
    else:
        c.execute("SELECT prompt, image_url, timestamp FROM image_history WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                  (chat_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows


def get_full_image_history(chat_id, limit=100):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT prompt, image_url, timestamp, kind FROM image_history WHERE chat_id=? ORDER BY id ASC LIMIT ?",
              (chat_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows


def get_all_users():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id, username, tokens FROM users ORDER BY tokens DESC")
    rows = c.fetchall()
    conn.close()
    return rows


def find_user_by_id(chat_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id, username, tokens FROM users WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row


def get_maintenance():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT active, message, enabled, notified FROM maintenance WHERE id=1")
    row = c.fetchone()
    conn.close()
    if not row:
        return (0, "Технические работы, пожалуйста подождите", 0, 1)
    return row


def set_maintenance(active, message=None, enabled=None, notified=None):
    conn = get_conn()
    c = conn.cursor()
    if message is not None:
        c.execute("UPDATE maintenance SET active=?, message=? WHERE id=1", (active, message))
    else:
        c.execute("UPDATE maintenance SET active=? WHERE id=1", (active,))
    if enabled is not None:
        c.execute("UPDATE maintenance SET enabled=? WHERE id=1", (enabled,))
    if notified is not None:
        c.execute("UPDATE maintenance SET notified=? WHERE id=1", (notified,))
    conn.commit()
    conn.close()


def save_maintenance_notified(chat_id, message_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO maintenance_notified (chat_id, message_id, timestamp) VALUES (?, ?, ?)",
              (chat_id, message_id, int(time.time())))
    conn.commit()
    conn.close()


def get_maintenance_notified():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id, message_id FROM maintenance_notified")
    rows = c.fetchall()
    conn.close()
    return rows


def clear_maintenance_notified():
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM maintenance_notified")
    conn.commit()
    conn.close()


def create_ticket(chat_id, username, message, photo_id=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO tickets (chat_id, username, message, photo_id, created) VALUES (?, ?, ?, ?, ?)",
              (chat_id, username, message, photo_id, int(time.time())))
    conn.commit()
    ticket_id = c.lastrowid
    conn.close()
    return ticket_id


def get_tickets_by_status(status):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, chat_id, username, message, photo_id, created FROM tickets WHERE status=? ORDER BY id ASC",
              (status,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_open_tickets():
    return get_tickets_by_status('open')


def get_done_tickets():
    return get_tickets_by_status('done')


def get_ticket(ticket_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, chat_id, username, message, photo_id, status FROM tickets WHERE id=?", (ticket_id,))
    row = c.fetchone()
    conn.close()
    return row


def answer_ticket(ticket_id, answer, status="done"):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE tickets SET status=?, answer=?, answered=? WHERE id=?",
              (status, answer, int(time.time()), ticket_id))
    conn.commit()
    conn.close()


def delete_ticket(ticket_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM tickets WHERE id=?", (ticket_id,))
    conn.commit()
    conn.close()


def get_user_tickets(chat_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, message, answer, status FROM tickets WHERE chat_id=? ORDER BY id DESC LIMIT 10", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def wipe_user_data(chat_id, what="all"):
    conn = get_conn()
    c = conn.cursor()
    if what in ("all", "chat"):
        c.execute("DELETE FROM history WHERE chat_id=?", (chat_id,))
    if what in ("all", "image"):
        c.execute("DELETE FROM image_history WHERE chat_id=? AND kind='gen'", (chat_id,))
    if what in ("all", "edit"):
        c.execute("DELETE FROM image_history WHERE chat_id=? AND kind='edit'", (chat_id,))
    if what in ("all", "orders"):
        c.execute("DELETE FROM orders WHERE chat_id=?", (chat_id,))
    if what in ("all", "tickets"):
        c.execute("DELETE FROM tickets WHERE chat_id=?", (chat_id,))
    if what in ("all", "stats"):
        c.execute("DELETE FROM stats WHERE chat_id=?", (chat_id,))
    if what == "all":
        c.execute("DELETE FROM action_logs WHERE chat_id=?", (chat_id,))
        c.execute("DELETE FROM subscriptions WHERE user_id=?", (chat_id,))
        c.execute("DELETE FROM active_operations WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def wipe_all_data(what="all"):
    conn = get_conn()
    c = conn.cursor()
    if what in ("all", "chat"):
        c.execute("DELETE FROM history")
    if what in ("all", "image"):
        c.execute("DELETE FROM image_history WHERE kind='gen'")
    if what in ("all", "edit"):
        c.execute("DELETE FROM image_history WHERE kind='edit'")
    if what in ("all", "orders"):
        c.execute("DELETE FROM orders")
    if what in ("all", "tickets"):
        c.execute("DELETE FROM tickets")
    if what in ("all", "stats"):
        c.execute("DELETE FROM stats")
    if what == "all":
        c.execute("DELETE FROM action_logs")
        c.execute("DELETE FROM subscriptions")
        c.execute("DELETE FROM active_operations")
        c.execute("DELETE FROM maintenance_notified")
        c.execute("""UPDATE users SET 
            tokens = 0,
            state = 'idle',
            trial_started = 0,
            trial_used = 0,
            used_images = 0,
            used_chats = 0,
            used_edits = 0
            """)
    conn.commit()
    conn.close()


def has_ban_record(chat_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT reason, timestamp FROM bans WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return True, row[0] if row[0] else "", row[1] if row[1] else 0
    return False, "", 0


def check_support_muted(chat_id):
    user = get_user(chat_id)
    muted_until = user[21]
    now = int(time.time())
    if muted_until > now:
        left = muted_until - now
        minutes = left // 60
        seconds = left % 60
        if minutes > 0:
            return True, f"{minutes} мин {seconds} сек"
        return True, f"{seconds} сек"
    return False, ""


def can_use_during_maintenance(chat_id):
    if chat_id == ADMIN_ID:
        return True
    user = get_user(chat_id)
    return bool(user[22])


def clear_old_messages(chat_id):
    for msg_id in last_messages.get(chat_id, []):
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    last_messages[chat_id] = []


def remember(chat_id, msg_id):
    last_messages.setdefault(chat_id, []).append(msg_id)
    if len(last_messages[chat_id]) > 20:
        last_messages[chat_id] = last_messages[chat_id][-20:]


def start_typing(chat_id):
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                bot.send_chat_action(chat_id, 'typing')
            except Exception:
                pass
            stop.wait(2)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return stop, t


def stop_typing(stop, t):
    stop.set()
    t.join(timeout=2)


def send_long_text_as_file(chat_id, text, filename="otvet.txt"):
    try:
        data = text.encode('utf-8')
        bot.send_document(chat_id, (filename, data))
        return True
    except Exception as e:
        log_error(f"Send file error: {e}")
        return False


def send_photo_safe(chat_id, image_url, caption):
    try:
        r = requests.get(image_url, timeout=15)
        if r.status_code != 200:
            return None
        img = r.content
        msg = bot.send_photo(chat_id, img, caption=caption, parse_mode='HTML')
        return msg.message_id
    except Exception as e:
        log_error(f"Send photo error: {e}")
        return None


def send_with_sound(chat_id, text, reply_markup=None, parse_mode='HTML'):
    user = get_user(chat_id)
    sound_on = user[23]
    try:
        return bot.send_message(chat_id, text, parse_mode=parse_mode,
                                reply_markup=reply_markup,
                                disable_notification=(not sound_on))
    except Exception:
        return bot.send_message(chat_id, text, reply_markup=reply_markup)


def check_banned(chat_id):
    user = get_user(chat_id)
    return bool(user[10]), user[11] if user[11] else ""


def send_banned_message(chat_id):
    banned, reason = check_banned(chat_id)
    if not banned:
        return False
    clear_old_messages(chat_id)
    text = (f"🚫 <b>Вы забанены.</b>\n\n"
            f"Причина: {escape_html(reason) if reason else 'не указана'}")
    sent = bot.send_message(chat_id, text, parse_mode='HTML')
    remember(chat_id, sent.message_id)
    return True


def deny_if_banned_or_maintenance(chat_id, call=None, message=None):
    if call and not check_spam(chat_id, cooldown=2):
        try:
            bot.answer_callback_query(call.id, "⏳ Не спамь")
        except Exception:
            pass
        return True

    state = get_user(chat_id)[1]
    if state in ('gen_processing', 'edit_processing'):
        if call:
            try:
                bot.answer_callback_query(call.id, "⏳ Генерация идёт, подожди")
            except Exception:
                pass
        return True

    banned, reason = check_banned(chat_id)
    if banned:
        if call:
            try:
                bot.answer_callback_query(call.id, "🚫 Вы забанены.")
            except Exception:
                pass
        if call:
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except Exception:
                pass
        send_banned_message(chat_id)
        return True
    if not can_use_during_maintenance(chat_id):
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT enabled, message FROM maintenance WHERE id=1")
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            msg = row[1] if row[1] else "Технические работы"
            try:
                if call:
                    bot.answer_callback_query(call.id, "🛠 Тех.работы")
                    try:
                        bot.delete_message(chat_id, call.message.message_id)
                    except Exception:
                        pass
                clear_old_messages(chat_id)
                sent = bot.send_message(chat_id, f"🛠 <b>{escape_html(msg)}</b>",
                                        parse_mode='HTML', reply_markup=maintenance_menu())
                remember(chat_id, sent.message_id)
            except Exception:
                pass
            return True
    return False

def main_menu(chat_id=None):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🎨 Сгенерировать", callback_data="menu_generate"))
    markup.add(telebot.types.InlineKeyboardButton("📜 История", callback_data="menu_history"))
    markup.add(telebot.types.InlineKeyboardButton("🛒 Покупки", callback_data="menu_my_orders"))
    markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
    markup.add(telebot.types.InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"))
    markup.add(telebot.types.InlineKeyboardButton("🆘 Поддержка", callback_data="menu_support"))
    markup.add(telebot.types.InlineKeyboardButton(
        "📱 Открыть приложение",
        web_app=telebot.types.WebAppInfo(url=WEBAPP_URL)
    ))
    if chat_id == ADMIN_ID:
        markup.add(telebot.types.InlineKeyboardButton("👑 Админ-меню", callback_data="menu_admin"))
    return markup


def maintenance_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🆘 Поддержка", callback_data="maint_support"))
    return markup


def generate_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🎨 Нарисовать изображение", callback_data="menu_image"))
    markup.add(telebot.types.InlineKeyboardButton("🖼 Редактировать фото", callback_data="menu_edit"))
    markup.add(telebot.types.InlineKeyboardButton("🤖 Чат с ИИ", callback_data="menu_chat"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main"))
    return markup


def history_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🤖 История ИИ", callback_data="hist_chat"))
    markup.add(telebot.types.InlineKeyboardButton("🎨 История генераций", callback_data="hist_image"))
    markup.add(telebot.types.InlineKeyboardButton("🖼 История редактирований", callback_data="hist_edit"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main"))
    return markup


def orders_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Завершённые", callback_data="my_orders_paid"))
    markup.add(telebot.types.InlineKeyboardButton("⏳ Незавершённые", callback_data="my_orders_pending"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main"))
    return markup


def buy_menu(chat_id):
    markup = telebot.types.InlineKeyboardMarkup()
    user = get_user(chat_id)
    trial_started = user[4]
    trial_used = user[5]
    now = int(time.time())
    if not trial_used and trial_started > 0 and (now - trial_started) < TRIAL_WINDOW:
        left = TRIAL_WINDOW - (now - trial_started)
        minutes = left // 60
        markup.add(telebot.types.InlineKeyboardButton(
            f"🎁 50 ₽ — 10 токенов ({minutes} мин)",
            callback_data="pack_trial"
        ))
    markup.add(telebot.types.InlineKeyboardButton("💵 100 ₽ — 20 токенов", callback_data="pack_100_20"))
    markup.add(telebot.types.InlineKeyboardButton("💵 250 ₽ — 50 токенов", callback_data="pack_250_50"))
    markup.add(telebot.types.InlineKeyboardButton("💵 500 ₽ — 100 токенов", callback_data="pack_500_100"))
    markup.add(telebot.types.InlineKeyboardButton("💵 1000 ₽ — 200 токенов", callback_data="pack_1000_200"))
    markup.add(telebot.types.InlineKeyboardButton("✏️ Своя сумма (20–3000)", callback_data="custom_amount"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main"))
    return markup


def gift_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Приобрести за 50 ₽", callback_data="pack_trial"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Не надо", callback_data="decline_gift"))
    return markup


def settings_menu(chat_id):
    user = get_user(chat_id)
    sound_on = user[23]
    markup = telebot.types.InlineKeyboardMarkup()
    sound_label = "🔊 Звук: ВКЛ" if sound_on else "🔇 Звук: ВЫКЛ"
    markup.add(telebot.types.InlineKeyboardButton(sound_label, callback_data="toggle_sound"))
    markup.add(telebot.types.InlineKeyboardButton("📖 Инструкции", callback_data="menu_help"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main"))
    return markup


def help_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📖 Инструкция по боту", callback_data="help_bot"))
    markup.add(telebot.types.InlineKeyboardButton("🤖 Инструкция по ИИ", callback_data="help_ai"))
    markup.add(telebot.types.InlineKeyboardButton("🎨 Инструкция по генерации", callback_data="help_gen"))
    markup.add(telebot.types.InlineKeyboardButton("🖼 Как редактировать фото", callback_data="help_edit"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_settings"))
    return markup


def support_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📝 Написать тикет", callback_data="ticket_new"))
    markup.add(telebot.types.InlineKeyboardButton("📋 Мои тикеты", callback_data="ticket_my"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main"))
    return markup


def chat_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🤖 Обычный ИИ", callback_data="mode_regular"))
    markup.add(telebot.types.InlineKeyboardButton("💻 Кодер (с подсказками)", callback_data="mode_coder_hints"))
    markup.add(telebot.types.InlineKeyboardButton("⚡ Кодер (только код)", callback_data="mode_coder_only"))
    markup.add(telebot.types.InlineKeyboardButton("📖 Объяснятор", callback_data="mode_explainer"))
    markup.add(telebot.types.InlineKeyboardButton("🌍 Переводчик", callback_data="mode_translator"))
    markup.add(telebot.types.InlineKeyboardButton("⚙️ Настройки поведения", callback_data="settings_from_chat"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_generate"))
    return markup


def ai_settings_menu(chat_id):
    user = get_user(chat_id)
    ai_mode = user[3]
    send_files = user[18]
    markup = telebot.types.InlineKeyboardMarkup()
    modes = [
        ("regular", "🤖 Обычный ИИ"),
        ("smart", "🧠 Умный ИИ"),
        ("open", "💬 Откровенный"),
        ("uncensored", "🔥 Без цензуры"),
    ]
    for key, name in modes:
        prefix = "✅ " if ai_mode == key else "◻️ "
        markup.add(telebot.types.InlineKeyboardButton(f"{prefix}{name}", callback_data=f"ai_{key}"))
    files_label = "📎 Ответы файлами: ВКЛ" if send_files else "📎 Ответы файлами: ВЫКЛ"
    markup.add(telebot.types.InlineKeyboardButton(files_label, callback_data="toggle_send_files"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_chat"))
    return markup


def back_to_generate_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_generate"))
    return markup


def back_to_main_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main"))
    return markup


def back_to_admin_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin"))
    return markup


def back_to_history_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_history"))
    return markup


def edit_mode_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📖 Инструкция", callback_data="edit_help"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel"))
    return markup


def edit_confirm_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Готово, обработать", callback_data="edit_confirm_yes"))
    markup.add(telebot.types.InlineKeyboardButton("➕ Добавить ещё фото", callback_data="edit_add_more"))
    markup.add(telebot.types.InlineKeyboardButton("📖 Инструкция", callback_data="edit_help"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel"))
    return markup


def edit_more_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📸 Прислать фото", callback_data="edit_add_more"))
    markup.add(telebot.types.InlineKeyboardButton("✅ Готово, обработать", callback_data="edit_confirm_yes"))
    markup.add(telebot.types.InlineKeyboardButton("📖 Инструкция", callback_data="edit_help"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel"))
    return markup


def send_main_menu(chat_id, sound=False):
    tokens = get_user(chat_id)[0]
    update_user(chat_id, 'state', 'idle')
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "     💎 ГЛАВНОЕ МЕНЮ\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Токенов: <b>{tokens}</b>\n\n"
        "Выбери действие 👇"
    )
    sent = send_with_sound(chat_id, text, reply_markup=main_menu(chat_id))
    remember(chat_id, sent.message_id)


def back_to_main_handler(chat_id, callback_query=None, message=None):
    if callback_query:
        if deny_if_banned_or_maintenance(chat_id, call=callback_query):
            return
    else:
        if deny_if_banned_or_maintenance(chat_id, message=message):
            return

    edit_photos_cache.pop(chat_id, None)
    _last_ai_message.pop(chat_id, None)
    update_user(chat_id, 'state', 'idle')

    if callback_query:
        try:
            bot.delete_message(chat_id, callback_query.message.message_id)
        except Exception:
            pass
    elif message:
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass

    clear_old_messages(chat_id)
    send_main_menu(chat_id)

    if callback_query:
        try:
            bot.answer_callback_query(callback_query.id)
        except Exception:
            pass


@bot.message_handler(commands=['start'])
def start(message):
    if message.from_user.username:
        update_user(message.chat.id, 'username', f"@{message.from_user.username}")
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    update_user(message.chat.id, 'state', 'idle')
    edit_photos_cache.pop(message.chat.id, None)
    _last_ai_message.pop(message.chat.id, None)
    if send_banned_message(message.chat.id):
        return
    has_ban, ban_reason, ban_time = has_ban_record(message.chat.id)
    user = get_user(message.chat.id)
    banned = user[10]
    if has_ban and banned:
        try:
            bot.send_message(
                message.chat.id,
                f"🚫 <b>Вы были забанены.</b>\n\nПричина: {escape_html(ban_reason) if ban_reason else 'не указана'}\n\nДля разбана обратитесь в поддержку.",
                parse_mode='HTML')
        except Exception:
            pass
        return
    trial_started = user[4]
    trial_used = user[5]
    if trial_started == 0 and not trial_used:
        update_user(message.chat.id, 'trial_started', int(time.time()))
    clear_old_messages(message.chat.id)
    if not can_use_during_maintenance(message.chat.id):
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT enabled, message FROM maintenance WHERE id=1")
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            msg = row[1] if row[1] else "Технические работы"
            sent = bot.send_message(message.chat.id, f"🛠 <b>{escape_html(msg)}</b>",
                                    parse_mode='HTML', reply_markup=maintenance_menu())
            remember(message.chat.id, sent.message_id)
            return
    send_main_menu(message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "back_to_main")
def back_to_main(call):
    back_to_main_handler(call.message.chat.id, callback_query=call)


@bot.callback_query_handler(func=lambda call: call.data == "back_to_generate")
def back_to_generate(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "     🎨 СОЗДАТЬ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Выбери действие 👇",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=generate_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "back_to_settings")
def back_to_settings(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "     ⚙️ НАСТРОЙКИ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=settings_menu(call.message.chat.id))
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "back_to_help")
def back_to_help(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "    📖 ИНСТРУКЦИИ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Выбери раздел:",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=help_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "back_to_support")
def back_to_support(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "     🆘 ПОДДЕРЖКА\n"
            "━━━━━━━━━━━━━━━━━━━━━━━",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=support_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "back_to_history")
def back_to_history(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "      📜 ИСТОРИЯ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Что показать?",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=history_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "back_to_orders")
def back_to_orders(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "      🛒 ПОКУПКИ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=orders_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "back_to_chat")
def back_to_chat(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    user = get_user(call.message.chat.id)
    tokens = user[0]
    mode = user[2]
    ai_mode = user[3]
    coder_mode = user[9]
    if mode == "coder":
        mode_name = "💻 Кодер (с подсказками)" if coder_mode == "with_hints" else "⚡ Кодер (только код)"
    else:
        mode_name = {"regular": "🤖 Обычный ИИ", "explainer": "📖 Объяснятор",
                     "translator": "🌍 Переводчик"}.get(mode, "🤖 Обычный ИИ")
    ai_name = {"regular": "🤖 Обычный", "smart": "🧠 Умный", "open": "💬 Откровенный",
               "uncensored": "🔥 Без цензуры"}.get(ai_mode, "🤖 Обычный")
    try:
        bot.edit_message_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"    🤖 ЧАТ С БОБОМ\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Баланс: {tokens}\n"
            f"🎯 Режим: {mode_name}\n"
            f"🧠 Поведение: {ai_name}\n\n"
            f"Задай вопрос или выбери режим 👇",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=chat_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "back_to_admin")
def back_to_admin(call):
    if call.message.chat.id != ADMIN_ID:
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "     👑 АДМИН-МЕНЮ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=admin_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "back_to_wipe")
def back_to_wipe(call):
    if call.message.chat.id != ADMIN_ID:
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "      🧹 ОЧИСТКА\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Что очистить?",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=wipe_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "noop")
def noop_handler(call):
    bot.answer_callback_query(call.id, "")

@bot.callback_query_handler(func=lambda call: call.data == "menu_settings")
def settings_menu_handler(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "     ⚙️ НАСТРОЙКИ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=settings_menu(call.message.chat.id))
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "toggle_sound")
def toggle_sound(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    user = get_user(call.message.chat.id)
    new_val = 0 if user[23] else 1
    update_user(call.message.chat.id, 'sound_on', new_val)
    bot.answer_callback_query(call.id, f"✅ Звук: {'ВКЛ' if new_val else 'ВЫКЛ'}")
    try:
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                       reply_markup=settings_menu(call.message.chat.id))
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "menu_help")
def menu_help(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "    📖 ИНСТРУКЦИИ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Выбери раздел:",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=help_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "help_bot")
def help_bot(call):
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "   📖 ИНСТРУКЦИЯ ПО БОТУ\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "💎 <b>Токены</b>\n"
        "• 🎨 Картинка — 4💎\n"
        "• 🖼 Редактирование — 4💎\n"
        "• 🤖 Сообщение — 1💎\n\n"
        "💰 <b>Купить токены</b>\n"
        "• 100 ₽ → 20 токенов\n"
        "• 250 ₽ → 50 токенов\n"
        "• 500 ₽ → 100 токенов\n"
        "• 1000 ₽ → 200 токенов\n\n"
        "⚙️ <b>Настройки</b>\n"
        "• Звук ВКЛ/ВЫКЛ\n"
        "• Инструкции\n\n"
        "🆘 <b>Поддержка</b>\n"
        "• Тикеты"
    )
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_help"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "help_ai")
def help_ai(call):
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "   🤖 ИНСТРУКЦИЯ ПО ИИ\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Боб умеет:\n"
        "• Отвечать на вопросы\n"
        "• Писать тексты и код\n"
        "• Переводить языки\n"
        "• Объяснять сложное\n\n"
        "🎯 <b>Режимы:</b>\n"
        "• 🤖 Обычный\n"
        "• 💻 Кодер\n"
        "• 📖 Объяснятор\n"
        "• 🌍 Переводчик\n\n"
        "⚙️ <b>Поведение:</b>\n"
        "• 🧠 Умный, 💬 Откровенный, 🔥 Без цензуры"
    )
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_help"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "help_gen")
def help_gen(call):
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  🎨 ИНСТРУКЦИЯ ПО ГЕНЕРАЦИИ\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Как рисовать картинки:\n\n"
        "1️⃣ Нажми «🎨 Нарисовать»\n"
        "2️⃣ Напиши промт\n"
        "3️⃣ Подтверди\n"
        "4️⃣ Получи картинку\n\n"
        "💡 <b>Примеры:</b>\n"
        "• рыжий кот на луне\n"
        "• дракон в стиле аниме\n"
        "• логотип для кофейни\n\n"
        "💰 Стоимость: 4 токена"
    )
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_help"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "help_edit")
def help_edit(call):
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        " 🖼 КАК РЕДАКТИРОВАТЬ ФОТО\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣ Пришли фото\n"
        "2️⃣ Напиши задание\n"
        "3️⃣ Нажми «Готово»\n\n"
        "💡 <b>Примеры:</b>\n"
        "• убери фон\n"
        "• помести на пляж\n"
        "• сделай в стиле аниме\n"
        "• добавь усы\n"
        "• замени небо\n\n"
        "📸 Можно 2-3 фото — для совмещения\n\n"
        "💰 Стоимость: 4 токена"
    )
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_help"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "menu_generate")
def menu_generate(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "     🎨 СОЗДАТЬ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Выбери действие 👇",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=generate_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "menu_history")
def menu_history(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "      📜 ИСТОРИЯ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Что показать?",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=history_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "menu_support")
def support(call):
    if send_banned_message(call.message.chat.id):
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "     🆘 ПОДДЕРЖКА\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Есть вопрос или проблема?",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=support_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "maint_support")
def maint_support(call):
    if send_banned_message(call.message.chat.id):
        return
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📝 Написать тикет", callback_data="maint_ticket_new"))
    markup.add(telebot.types.InlineKeyboardButton("📋 Мои тикеты", callback_data="maint_ticket_my"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="maint_back"))
    try:
        bot.edit_message_text(
            "🆘 <b>Поддержка</b>\n────────────────\nЧто вы хотите сделать?",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "maint_back")
def maint_back(call):
    if send_banned_message(call.message.chat.id):
        return
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT message FROM maintenance WHERE id=1")
    row = c.fetchone()
    conn.close()
    msg = row[0] if row and row[0] else "Технические работы"
    try:
        bot.edit_message_text(f"🛠 <b>{escape_html(msg)}</b>",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=maintenance_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "maint_ticket_new")
def maint_ticket_new(call):
    if send_banned_message(call.message.chat.id):
        return
    muted, time_left = check_support_muted(call.message.chat.id)
    if muted:
        bot.answer_callback_query(call.id, f"🚫 Подожди {time_left}")
        return
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="maint_support"))
    sent = bot.send_message(call.message.chat.id, "📝 Напишите ваш вопрос:", reply_markup=markup)
    remember(call.message.chat.id, sent.message_id)
    bot.register_next_step_handler(sent, maint_ticket_save)


def maint_ticket_save(message):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if send_banned_message(message.chat.id):
        return
    muted, time_left = check_support_muted(message.chat.id)
    if muted:
        sent = bot.send_message(message.chat.id, f"🚫 Подожди {time_left}")
        remember(message.chat.id, sent.message_id)
        return
    user = get_user(message.chat.id)
    username = user[6] if user[6] else f"ID:{message.chat.id}"
    text = message.text if message.text else "[без текста]"
    ticket_id = create_ticket(message.chat.id, username, text)
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    clear_old_messages(message.chat.id)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="maint_support"))
    sent = bot.send_message(message.chat.id, f"✅ <b>Тикет #{ticket_id} создан.</b>",
                            parse_mode='HTML', reply_markup=markup)
    remember(message.chat.id, sent.message_id)
    try:
        bot.send_message(ADMIN_ID, f"🔔 Новый тикет #{ticket_id} от {username}\n\n{text}")
    except Exception:
        pass
    notify_subscribers(message.chat.id, f"🎫 Новый тикет #{ticket_id}")


@bot.callback_query_handler(func=lambda call: call.data == "maint_ticket_my")
def maint_ticket_my(call):
    if send_banned_message(call.message.chat.id):
        return
    tickets = get_user_tickets(call.message.chat.id)
    if not tickets:
        bot.answer_callback_query(call.id, "У вас нет тикетов.")
        return
    text = "📋 <b>Ваши тикеты:</b>\n\n"
    for tid, msg, ans, status in tickets:
        st = {"open": "⏳ ожидает", "done": "✅ выполнен", "closed": "✅ отвечен"}.get(status, status)
        text += f"<b>#{tid}</b> ({st})\n{escape_html(msg)}\n"
        if ans:
            text += f"💬 Ответ: {escape_html(ans)}\n"
        text += "\n"
    if len(text) > 4000:
        text = text[:4000] + "..."
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="maint_support"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "ticket_new")
def ticket_new(call):
    if send_banned_message(call.message.chat.id):
        return
    muted, time_left = check_support_muted(call.message.chat.id)
    if muted:
        bot.answer_callback_query(call.id, f"🚫 Подожди {time_left}")
        return
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    sent = bot.send_message(call.message.chat.id, "📝 Напишите ваш вопрос:", reply_markup=back_to_generate_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.register_next_step_handler(sent, ticket_save)


def ticket_save(message):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if send_banned_message(message.chat.id):
        return
    muted, time_left = check_support_muted(message.chat.id)
    if muted:
        sent = bot.send_message(message.chat.id, f"🚫 Подожди {time_left}")
        remember(message.chat.id, sent.message_id)
        return
    user = get_user(message.chat.id)
    username = user[6] if user[6] else f"ID:{message.chat.id}"
    text = message.text if message.text else "[без текста]"
    ticket_id = create_ticket(message.chat.id, username, text)
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    sent = bot.send_message(message.chat.id, f"✅ <b>Тикет #{ticket_id} создан.</b>",
                            parse_mode='HTML', reply_markup=back_to_generate_menu())
    remember(message.chat.id, sent.message_id)
    try:
        bot.send_message(ADMIN_ID, f"🔔 Новый тикет #{ticket_id} от {username}\n\n{text}")
    except Exception:
        pass
    notify_subscribers(message.chat.id, f"🎫 Новый тикет #{ticket_id}")


@bot.callback_query_handler(func=lambda call: call.data == "ticket_my")
def ticket_my(call):
    if send_banned_message(call.message.chat.id):
        return
    tickets = get_user_tickets(call.message.chat.id)
    if not tickets:
        bot.answer_callback_query(call.id, "У вас нет тикетов.")
        return
    text = "📋 <b>Ваши тикеты:</b>\n\n"
    for tid, msg, ans, status in tickets:
        st = {"open": "⏳ ожидает", "done": "✅ выполнен", "closed": "✅ отвечен"}.get(status, status)
        text += f"<b>#{tid}</b> ({st})\n{escape_html(msg)}\n"
        if ans:
            text += f"💬 Ответ: {escape_html(ans)}\n"
        text += "\n"
    if len(text) > 4000:
        text = text[:4000] + "..."
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=back_to_generate_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_image")
def image_menu(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    user = get_user(call.message.chat.id)
    tokens = user[0]
    can_image = user[12]
    limit_images = user[14]
    used_images = user[16]

    if not can_image:
        bot.answer_callback_query(call.id, "🚫 Генерация картинок запрещена.")
        try:
            bot.edit_message_text(
                "🚫 <b>Вам запрещена генерация картинок.</b>\n\nОбратитесь в поддержку.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode='HTML', reply_markup=back_to_generate_menu())
        except Exception:
            pass
        return

    if limit_images >= 0 and used_images >= limit_images:
        bot.answer_callback_query(call.id, "🚫 Лимит картинок исчерпан.")
        try:
            bot.edit_message_text(
                f"🚫 <b>Лимит картинок исчерпан.</b>\n\nИспользовано: {used_images} / {limit_images}\n\nОбратитесь в поддержку.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode='HTML', reply_markup=back_to_generate_menu())
        except Exception:
            pass
        return

    if tokens < IMAGE_COST:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
        markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_generate"))
        try:
            bot.edit_message_text(
                f"❌ Нужно {IMAGE_COST} токена для генерации картинки.\nУ вас: {tokens}.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                reply_markup=markup)
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    limit_info = ""
    if limit_images >= 0:
        limit_info = f"\n📊 Осталось картинок: <b>{limit_images - used_images}</b>"

    try:
        bot.edit_message_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"    🎨 ГЕНЕРАЦИЯ\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Стоимость: <b>{IMAGE_COST} токена</b>\n"
            f"💎 У вас: <b>{tokens}</b>{limit_info}\n\n"
            f"📝 Напиши, что нарисовать:",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=back_to_generate_menu())
    except Exception:
        pass
    bot.register_next_step_handler(call.message, image_prompt_ask)
    bot.answer_callback_query(call.id)


def image_prompt_ask(message):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if deny_if_banned_or_maintenance(message.chat.id, message=message):
        return
    prompt = (message.text or "").strip()
    if not prompt:
        sent = bot.send_message(message.chat.id, "❌ Пустой промт.", reply_markup=back_to_generate_menu())
        remember(message.chat.id, sent.message_id)
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    clear_old_messages(message.chat.id)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Нарисовать", callback_data="img_confirm_yes"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="img_confirm_no"))
    sent = bot.send_message(
        message.chat.id,
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"    🎨 ПОДТВЕРЖДЕНИЕ\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📝 Промт:\n<code>{escape_html(prompt)}</code>\n\n"
        f"💰 Стоимость: 4💎\n\n"
        f"Нарисовать?",
        parse_mode='HTML', reply_markup=markup)
    remember(message.chat.id, sent.message_id)
    update_user(message.chat.id, 'state', f'img_confirm:{prompt[:200]}')


@bot.callback_query_handler(func=lambda call: call.data == "img_confirm_no")
def img_confirm_no(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    update_user(call.message.chat.id, 'state', 'idle')
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "     🎨 СОЗДАТЬ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Выбери действие 👇",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=generate_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id, "❌ Отменено")


@bot.callback_query_handler(func=lambda call: call.data == "back_blocked")
def back_blocked(call):
    bot.answer_callback_query(call.id, "⏳ Во время генерации нельзя идти назад")


@bot.callback_query_handler(func=lambda call: call.data == "img_confirm_yes")
def img_confirm_yes(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    user = get_user(call.message.chat.id)
    state = user[1]
    tokens = user[0]
    can_image = user[12]
    limit_images = user[14]
    used_images = user[16]

    if not can_image:
        bot.answer_callback_query(call.id, "🚫 Картинки запрещены.")
        update_user(call.message.chat.id, 'state', 'idle')
        return
    if limit_images >= 0 and used_images >= limit_images:
        bot.answer_callback_query(call.id, "🚫 Лимит картинок исчерпан.")
        update_user(call.message.chat.id, 'state', 'idle')
        return
    if not state.startswith('img_confirm:'):
        bot.answer_callback_query(call.id, "❌ Промт потерян, попробуйте снова.")
        return
    prompt = state.replace('img_confirm:', '', 1)
    update_user(call.message.chat.id, 'state', 'idle')
    if tokens < IMAGE_COST:
        bot.answer_callback_query(call.id, "❌ Недостаточно токенов.")
        return
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

    chat_id = call.message.chat.id
    bar_width = 22
    bar_msg = None

    mark_operation_start(chat_id, "gen")
    update_user(chat_id, 'state', 'gen_processing')

    try:
        bar_text = (
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"    🎨 ГЕНЕРАЦИЯ\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<code>{render_progress_bar(0, bar_width)}</code> 0%\n"
            f"⏳ Анализирую...\n\n"
            f"📝 <code>{escape_html(prompt)}</code>"
        )
        bar_markup = telebot.types.InlineKeyboardMarkup()
        bar_markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_blocked"))
        bar_msg = bot.send_message(chat_id, bar_text, parse_mode='HTML', reply_markup=bar_markup)
        remember(chat_id, bar_msg.message_id)
    except Exception:
        bar_msg = None

    try:
        generation_done = threading.Event()
        generation_result = {"url": None}

        def do_generation():
            try:
                url = generate_image_bothub(prompt)
                generation_result["url"] = url
            except Exception as e:
                user = get_user(chat_id)
                uname = user[6] if user[6] else ""
                log_error(f"Gen error: {e}", chat_id=chat_id, username=uname)
            finally:
                generation_done.set()

        threading.Thread(target=do_generation, daemon=True).start()

        percent = 0
        if bar_msg:
            while not generation_done.is_set():
                if percent < 90:
                    percent += 15
                    if percent > 90:
                        percent = 90
                else:
                    percent = min(percent + 1, 95)
                try:
                    bar_text = (
                        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"    🎨 ГЕНЕРАЦИЯ\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"<code>{render_progress_bar(percent, bar_width)}</code> {percent}%\n"
                        f"{get_stage_text(percent)}\n\n"
                        f"📝 <code>{escape_html(prompt)}</code>"
                    )
                    bot.edit_message_text(bar_text, chat_id=chat_id,
                                          message_id=bar_msg.message_id, parse_mode='HTML',
                                          reply_markup=bar_markup)
                except Exception:
                    pass
                time.sleep(2)

        if bar_msg:
            try:
                bar_text = (
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"    🎨 ГЕНЕРАЦИЯ\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"<code>{render_progress_bar(100, bar_width)}</code> 100%\n"
                    f"✅ Готово!"
                )
                bot.edit_message_text(bar_text, chat_id=chat_id,
                                      message_id=bar_msg.message_id, parse_mode='HTML')
                time.sleep(1)
                bot.delete_message(chat_id, bar_msg.message_id)
            except Exception:
                pass
    finally:
        update_user(chat_id, 'state', 'idle')
        mark_operation_end(chat_id)

    image_url = generation_result["url"]
    if not image_url:
        sent = bot.send_message(chat_id, "❌ Не удалось сгенерировать картинку. Попробуй позже.",
                                reply_markup=back_to_generate_menu())
        remember(chat_id, sent.message_id)
        return
    try:
        img = requests.get(image_url, timeout=60).content
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("🔁 Ещё раз — 4💎", callback_data="menu_image"))
        markup.add(telebot.types.InlineKeyboardButton("🏠 В меню", callback_data="back_to_main"))
        sent = bot.send_photo(chat_id, img, caption=f"🎨 <b>{escape_html(prompt)}</b>",
                              parse_mode='HTML', reply_markup=markup)
        remember(chat_id, sent.message_id)
        add_tokens(chat_id, -IMAGE_COST)
        log_stat(chat_id, IMAGE_COST)
        add_image_history(chat_id, prompt, image_url, kind="gen")
        inc_used(chat_id, 'used_images')
        log_action(chat_id, "gen", prompt)
        notify_subscribers(chat_id, f"🎨 Генерация: {prompt[:100]}")
        clear_old_logs()
    except Exception as e:
        log_error(f"Send image error: {e}", chat_id=chat_id)
        sent = bot.send_message(chat_id, f"❌ Ошибка отправки: {e}",
                                reply_markup=back_to_generate_menu())
        remember(chat_id, sent.message_id)


def generate_image_bothub(prompt):
    url = "https://openai.bothub.chat/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {BOTHUB_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "gemini-2.5-flash-image",
        "prompt": prompt,
        "response_format": "url"
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=120)
        result = r.json()
        if "data" in result and len(result["data"]) > 0:
            return result["data"][0].get("url")
        log_error(f"BotHub response: {result}")
        return None
    except Exception as e:
        log_error(f"BotHub error: {e}")
        return None

@bot.callback_query_handler(func=lambda call: call.data == "menu_edit")
def edit_menu(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    user = get_user(call.message.chat.id)
    tokens = user[0]
    can_image = user[12]
    limit_edits = user[20]
    used_edits = user[19]

    if not can_image:
        bot.answer_callback_query(call.id, "🚫 Редактирование запрещено.")
        try:
            bot.edit_message_text(
                "🚫 <b>Вам запрещено редактирование фото.</b>\n\nОбратитесь в поддержку.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode='HTML', reply_markup=back_to_generate_menu())
        except Exception:
            pass
        return

    if limit_edits >= 0 and used_edits >= limit_edits:
        bot.answer_callback_query(call.id, "🚫 Лимит редактирований исчерпан.")
        try:
            bot.edit_message_text(
                f"🚫 <b>Лимит редактирований исчерпан.</b>\n\nИспользовано: {used_edits} / {limit_edits}\n\nОбратитесь в поддержку.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode='HTML', reply_markup=back_to_generate_menu())
        except Exception:
            pass
        return

    if tokens < EDIT_COST:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
        markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_generate"))
        try:
            bot.edit_message_text(
                f"❌ Нужно {EDIT_COST} токена для редактирования.\nУ вас: {tokens}.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                reply_markup=markup)
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    edit_photos_cache[call.message.chat.id] = {"photos": [], "prompt": "", "created": int(time.time())}
    update_user(call.message.chat.id, 'state', 'edit_wait_photo')

    limit_info = ""
    if limit_edits >= 0:
        limit_info = f"\n📊 Осталось: <b>{limit_edits - used_edits}</b>"

    try:
        bot.edit_message_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"   🖼 РЕДАКТИРОВАНИЕ\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"1️⃣ Пришли фото\n"
            f"2️⃣ Напиши задание\n"
            f"3️⃣ Нажми «Готово»\n\n"
            f"💡 Примеры:\n"
            f"• убери фон\n"
            f"• помести на пляж\n"
            f"• сделай аниме\n\n"
            f"💰 Стоимость: {EDIT_COST}💎{limit_info}\n\n"
            f"📸 Пришли фото 👇",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=edit_mode_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "edit_cancel")
def edit_cancel(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    update_user(call.message.chat.id, 'state', 'idle')
    edit_photos_cache.pop(call.message.chat.id, None)
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "     🎨 СОЗДАТЬ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Выбери действие 👇",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=generate_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "edit_add_more")
def edit_add_more(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    cache = edit_photos_cache.get(call.message.chat.id, {"photos": [], "prompt": "", "created": int(time.time())})
    cache["created"] = int(time.time())
    edit_photos_cache[call.message.chat.id] = cache
    update_user(call.message.chat.id, 'state', 'edit_wait_photo')
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    sent = bot.send_message(call.message.chat.id,
                            f"📸 Пришли следующее фото.\n\nВсего сейчас: <b>{len(cache['photos'])}</b>",
                            parse_mode='HTML', reply_markup=edit_mode_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "edit_help")
def edit_help(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    help_text = (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        " 🖼 КАК РЕДАКТИРОВАТЬ ФОТО\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🖼 <b>Что можно делать:</b>\n"
        "• Убрать объект — «убери камень»\n"
        "• Заменить — «поставь дерево»\n"
        "• Изменить фон — «помести на пляж»\n"
        "• Изменить стиль — «сделай аниме»\n"
        "• Добавить объект — «добавь шляпу»\n"
        "• Объединить — пришли 2 фото\n\n"
        "📸 <b>Как использовать:</b>\n"
        "1️⃣ Пришли фото (можно несколько)\n"
        "2️⃣ Напиши задание\n"
        "3️⃣ Нажми «✅ Готово»\n\n"
        "💰 <b>Стоимость:</b> 4 токена\n"
        "⏳ <b>Время:</b> 20-60 секунд"
    )
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="edit_help_back"))
    try:
        bot.edit_message_text(help_text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        sent = bot.send_message(call.message.chat.id, help_text,
                                parse_mode='HTML', reply_markup=markup)
        remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "edit_help_back")
def edit_help_back(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    cache = edit_photos_cache.get(call.message.chat.id, {"photos": [], "prompt": ""})
    count = len(cache.get("photos", []))
    if count == 0:
        text = (
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "   🖼 РЕДАКТИРОВАНИЕ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Стоимость: {EDIT_COST}💎\n\n"
            "📸 Пришли фото, которое надо отредактировать."
        )
        markup = edit_mode_menu()
    else:
        prompt = cache.get("prompt", "")
        if prompt:
            text = (
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"  🖼 ГОТОВО К ОБРАБОТКЕ\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📸 Фото: <b>{count}</b>\n"
                f"📝 Задание:\n<code>{escape_html(prompt)}</code>\n\n"
                f"Обработать или добавить ещё?"
            )
            markup = edit_confirm_menu()
        else:
            text = (
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"   🖼 ФОТО: {count}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📝 <b>Что сделать?</b>\nНапиши задание 👇"
            )
            markup = edit_more_menu()
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


def edit_process_photo(message):
    if send_banned_message(message.chat.id):
        return
    user = get_user(message.chat.id)
    state = user[1]
    tokens = user[0]
    if not state.startswith('edit_wait_photo'):
        return
    if not can_use_during_maintenance(message.chat.id):
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT enabled FROM maintenance WHERE id=1")
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            return

    if tokens < EDIT_COST:
        sent = bot.send_message(message.chat.id, f"❌ Недостаточно токенов. Нужно: {EDIT_COST}.",
                                reply_markup=back_to_generate_menu())
        remember(message.chat.id, sent.message_id)
        update_user(message.chat.id, 'state', 'idle')
        return

    try:
        photo = message.photo[-1]
        file_info = bot.get_file(photo.file_id)
        file_bytes = bot.download_file(file_info.file_path)
        b64 = base64.b64encode(file_bytes).decode('utf-8')
    except Exception as e:
        log_error(f"Photo read error: {e}", chat_id=message.chat.id)
        sent = bot.send_message(message.chat.id, f"❌ Ошибка чтения фото: {e}",
                                reply_markup=back_to_generate_menu())
        remember(message.chat.id, sent.message_id)
        return

    caption = (message.caption or "").strip()

    cache = edit_photos_cache.get(message.chat.id, {"photos": [], "prompt": "", "created": int(time.time())})
    cache["photos"].append(b64)
    if caption and not cache.get("prompt"):
        cache["prompt"] = caption
    cache["created"] = int(time.time())
    edit_photos_cache[message.chat.id] = cache

    count = len(cache["photos"])
    if cache["prompt"]:
        info = (
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"   🖼 ФОТО ПОЛУЧЕНО\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📸 Всего фото: <b>{count}</b>\n"
            f"📝 Задание:\n<code>{escape_html(cache['prompt'])}</code>\n\n"
            f"Обработать или добавить ещё?"
        )
        markup = edit_confirm_menu()
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        sent = bot.send_message(message.chat.id, info, parse_mode='HTML', reply_markup=markup)
        remember(message.chat.id, sent.message_id)
        update_user(message.chat.id, 'state', 'edit_ready')
    else:
        info = (
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"   🖼 ФОТО ПОЛУЧЕНО\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📸 Всего фото: <b>{count}</b>\n"
            f"💰 Стоимость: <b>{EDIT_COST}💎</b>\n\n"
            f"📝 <b>Что сделать?</b>\n"
            f"Напиши задание 👇"
        )
        markup = edit_more_menu()
        sent = bot.send_message(message.chat.id, info, parse_mode='HTML', reply_markup=markup)
        remember(message.chat.id, sent.message_id)
        bot.register_next_step_handler(sent, edit_ask_prompt)


def edit_ask_prompt(message):
    if message.text == "⬅️ Назад":
        update_user(message.chat.id, 'state', 'idle')
        edit_photos_cache.pop(message.chat.id, None)
        back_to_main_handler(message.chat.id, message=message)
        return
    if message.text in ["📸 Прислать фото", "✅ Готово, обработать", "❌ Отмена", "📖 Инструкция"]:
        return
    if send_banned_message(message.chat.id):
        return
    prompt = (message.text or "").strip()
    if not prompt:
        sent = bot.send_message(message.chat.id, "❌ Пустой промт.",
                                reply_markup=back_to_generate_menu())
        remember(message.chat.id, sent.message_id)
        return
    cache = edit_photos_cache.get(message.chat.id, {"photos": [], "prompt": "", "created": int(time.time())})
    cache["prompt"] = prompt
    cache["created"] = int(time.time())
    edit_photos_cache[message.chat.id] = cache
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    update_user(message.chat.id, 'state', 'edit_ready')
    sent = bot.send_message(
        message.chat.id,
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  🖼 ГОТОВО К ОБРАБОТКЕ\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📸 Фото: <b>{len(cache['photos'])}</b>\n"
        f"📝 Задание:\n<code>{escape_html(prompt)}</code>\n\n"
        f"Обработать или добавить ещё?",
        parse_mode='HTML', reply_markup=edit_confirm_menu())
    remember(message.chat.id, sent.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "edit_confirm_yes")
def edit_confirm_yes(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    cache = edit_photos_cache.get(call.message.chat.id)
    if not cache or not cache["photos"] or not cache["prompt"]:
        bot.answer_callback_query(call.id, "❌ Данные потеряны. Начните заново.")
        update_user(call.message.chat.id, 'state', 'idle')
        return
    user = get_user(call.message.chat.id)
    tokens = user[0]
    limit_edits = user[20]
    used_edits = user[19]
    if tokens < EDIT_COST:
        bot.answer_callback_query(call.id, f"❌ Нужно {EDIT_COST} токена.")
        return
    if limit_edits >= 0 and used_edits >= limit_edits:
        bot.answer_callback_query(call.id, "🚫 Лимит редактирований исчерпан.")
        return
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

    chat_id = call.message.chat.id
    bar_width = 22
    bar_msg = None

    mark_operation_start(chat_id, "edit")
    update_user(chat_id, 'state', 'edit_processing')

    try:
        bar_text = (
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  🖼 РЕДАКТИРОВАНИЕ\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<code>{render_progress_bar(0, bar_width)}</code> 0%\n"
            f"⏳ Обрабатываю..."
        )
        bar_markup = telebot.types.InlineKeyboardMarkup()
        bar_markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_blocked"))
        bar_msg = bot.send_message(chat_id, bar_text, parse_mode='HTML', reply_markup=bar_markup)
        remember(chat_id, bar_msg.message_id)
    except Exception:
        bar_msg = None

    try:
        photos = cache["photos"]
        prompt = cache["prompt"]
        generation_done = threading.Event()
        generation_result = {"url": None}

        def do_edit():
            try:
                url = edit_photo_bothub(photos, prompt)
                generation_result["url"] = url
            except Exception as e:
                user = get_user(chat_id)
                uname = user[6] if user[6] else ""
                log_error(f"Edit error: {e}", chat_id=chat_id, username=uname)
            finally:
                generation_done.set()

        threading.Thread(target=do_edit, daemon=True).start()

        percent = 0
        if bar_msg:
            while not generation_done.is_set():
                if percent < 90:
                    percent += 12
                    if percent > 90:
                        percent = 90
                else:
                    percent = min(percent + 1, 95)
                try:
                    bar_text = (
                        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"  🖼 РЕДАКТИРОВАНИЕ\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"<code>{render_progress_bar(percent, bar_width)}</code> {percent}%\n"
                        f"{get_stage_text(percent)}"
                    )
                    bot.edit_message_text(bar_text, chat_id=chat_id,
                                          message_id=bar_msg.message_id, parse_mode='HTML',
                                          reply_markup=bar_markup)
                except Exception:
                    pass
                time.sleep(2)

        if bar_msg:
            try:
                bot.edit_message_text(
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"  🖼 РЕДАКТИРОВАНИЕ\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"<code>{render_progress_bar(100, bar_width)}</code> 100%\n"
                    f"✅ Готово!",
                    chat_id=chat_id, message_id=bar_msg.message_id, parse_mode='HTML')
                time.sleep(1)
                bot.delete_message(chat_id, bar_msg.message_id)
            except Exception:
                pass
    finally:
        update_user(chat_id, 'state', 'idle')
        mark_operation_end(chat_id)

    result_url = generation_result["url"]

    if not result_url:
        sent = bot.send_message(chat_id,
                                "❌ Не удалось обработать фото. Попробуй позже.",
                                reply_markup=back_to_generate_menu())
        remember(chat_id, sent.message_id)
        edit_photos_cache.pop(chat_id, None)
        return

    try:
        img = requests.get(result_url, timeout=120).content
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("🔁 Ещё раз — 4💎", callback_data="menu_edit"))
        markup.add(telebot.types.InlineKeyboardButton("🏠 В меню", callback_data="back_to_main"))
        sent = bot.send_photo(chat_id, img,
                              caption=f"🖼 <b>{escape_html(prompt)}</b>",
                              parse_mode='HTML', reply_markup=markup)
        remember(chat_id, sent.message_id)
        add_tokens(chat_id, -EDIT_COST)
        log_stat(chat_id, EDIT_COST)
        add_image_history(chat_id, prompt, result_url, kind="edit")
        inc_used(chat_id, 'used_edits')
        log_action(chat_id, "edit", prompt)
        notify_subscribers(chat_id, f"🖼 Редактирование: {prompt[:100]}")
        clear_old_logs()
    except Exception as e:
        log_error(f"Edit send error: {e}", chat_id=chat_id)
        sent = bot.send_message(chat_id, f"❌ Ошибка отправки: {e}",
                                reply_markup=back_to_generate_menu())
        remember(chat_id, sent.message_id)

    edit_photos_cache.pop(chat_id, None)


def edit_photo_bothub(photos_b64, prompt):
    url = "https://openai.bothub.chat/v1/images/edits"
    headers = {
        "Authorization": f"Bearer {BOTHUB_API_KEY}"
    }
    if not photos_b64:
        return None
    files = []
    for i, b64 in enumerate(photos_b64):
        try:
            if "," in b64:
                b64 = b64.split(",")[1]
            img_bytes = base64.b64decode(b64)
            files.append(("image", (f"input_{i}.png", img_bytes, "image/png")))
        except Exception as e:
            log_error(f"Decode error for photo {i}: {e}")
            continue
    if not files:
        return None
    data = {
        "model": "gemini-2.5-flash-image",
        "prompt": prompt,
        "response_format": "url"
    }
    try:
        r = requests.post(url, headers=headers, data=data, files=files, timeout=180)
        print(f"Edit status: {r.status_code}")
        result = r.json()
        print(f"Edit response: {result}")
        if "data" in result and len(result["data"]) > 0:
            return result["data"][0].get("url")
        if "url" in result:
            return result["url"]
        return None
    except Exception as e:
        log_error(f"Edit error: {e}")
        return None

@bot.callback_query_handler(func=lambda call: call.data == "settings_from_chat")
def settings_from_chat(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "  ⚙️ НАСТРОЙКИ ПОВЕДЕНИЯ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=ai_settings_menu(call.message.chat.id))
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("ai_"))
def set_ai_mode(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    ai_mode = call.data.replace("ai_", "")
    update_user(call.message.chat.id, 'ai_mode', ai_mode)
    names = {"regular": "🤖 Обычный", "smart": "🧠 Умный", "open": "💬 Откровенный", "uncensored": "🔥 Без цензуры"}
    bot.answer_callback_query(call.id, f"✅ {names.get(ai_mode, '')}")
    try:
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                       reply_markup=ai_settings_menu(call.message.chat.id))
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "toggle_send_files")
def toggle_send_files(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    user = get_user(call.message.chat.id)
    new_val = 0 if user[18] else 1
    update_user(call.message.chat.id, 'send_files', new_val)
    bot.answer_callback_query(call.id, f"✅ Ответы файлами: {'ВКЛ' if new_val else 'ВЫКЛ'}")
    try:
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                       reply_markup=ai_settings_menu(call.message.chat.id))
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "menu_chat")
def enter_chat(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    user = get_user(call.message.chat.id)
    tokens = user[0]
    mode = user[2]
    ai_mode = user[3]
    coder_mode = user[9]
    can_chat = user[13]
    limit_chats = user[15]
    used_chats = user[17]

    if not can_chat:
        bot.answer_callback_query(call.id, "🚫 Чат запрещён.")
        try:
            bot.edit_message_text(
                "🚫 <b>Вам запрещён чат с ИИ.</b>\n\nОбратитесь в поддержку.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode='HTML', reply_markup=back_to_generate_menu())
        except Exception:
            pass
        return

    if limit_chats >= 0 and used_chats >= limit_chats:
        bot.answer_callback_query(call.id, "🚫 Лимит исчерпан.")
        try:
            bot.edit_message_text(
                f"🚫 <b>Лимит чата исчерпан.</b>\n\nИспользовано: {used_chats} / {limit_chats}",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode='HTML', reply_markup=back_to_generate_menu())
        except Exception:
            pass
        return

    if tokens < 1:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
        markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_generate"))
        try:
            bot.edit_message_text("❌ У вас нет токенов.",
                                  chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  reply_markup=markup)
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    update_user(call.message.chat.id, 'state', 'chat')
    if mode == "coder":
        mode_name = "💻 Кодер (с подсказками)" if coder_mode == "with_hints" else "⚡ Кодер (только код)"
    else:
        mode_name = {"regular": "🤖 Обычный ИИ", "explainer": "📖 Объяснятор",
                     "translator": "🌍 Переводчик"}.get(mode, "🤖 Обычный ИИ")
    ai_name = {"regular": "🤖 Обычный", "smart": "🧠 Умный", "open": "💬 Откровенный",
               "uncensored": "🔥 Без цензуры"}.get(ai_mode, "🤖 Обычный")
    limit_info = ""
    if limit_chats >= 0:
        limit_info = f"\n📊 Осталось: <b>{limit_chats - used_chats}</b>"

    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"    🤖 ЧАТ С БОБОМ\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Баланс: <b>{tokens}</b>\n"
        f"🎯 Режим: <b>{mode_name}</b>\n"
        f"🧠 Поведение: <b>{ai_name}</b>{limit_info}\n\n"
        f"💡 Что спросить?\n"
        f"• 🎓 Помоги с учёбой\n"
        f"• 💻 Напиши код\n"
        f"• ✍️ Сочинение\n"
        f"• 🌍 Перевести\n"
        f"• 📖 Объяснить\n\n"
        f"Или напиши свой вопрос 👇"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=chat_menu())
    except Exception:
        sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=chat_menu())
        remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("mode_"))
def set_mode(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    raw = call.data.replace("mode_", "")
    if raw == "coder_hints":
        mode = "coder"
        update_user(call.message.chat.id, 'coder_mode', 'with_hints')
    elif raw == "coder_only":
        mode = "coder"
        update_user(call.message.chat.id, 'coder_mode', 'only_code')
    else:
        mode = raw
    update_user(call.message.chat.id, 'mode', mode)
    user = get_user(call.message.chat.id)
    tokens = user[0]
    ai_mode = user[3]
    coder_mode = user[9]
    if mode == "coder":
        mode_name = "💻 Кодер (с подсказками)" if coder_mode == "with_hints" else "⚡ Кодер (только код)"
    else:
        mode_name = {"regular": "🤖 Обычный ИИ", "explainer": "📖 Объяснятор",
                     "translator": "🌍 Переводчик"}.get(mode, "🤖 Обычный ИИ")
    ai_name = {"regular": "🤖 Обычный", "smart": "🧠 Умный", "open": "💬 Откровенный",
               "uncensored": "🔥 Без цензуры"}.get(ai_mode, "🤖 Обычный")
    bot.answer_callback_query(call.id, f"✅ {mode_name}")
    try:
        bot.edit_message_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"    🤖 ЧАТ С БОБОМ\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Баланс: <b>{tokens}</b>\n"
            f"🎯 Режим: <b>{mode_name}</b>\n"
            f"🧠 Поведение: <b>{ai_name}</b>\n\n"
            f"Задай вопрос 👇",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=chat_menu())
    except Exception:
        pass


# === GIGACHAT ===
def get_gigachat_token(force_refresh=False):
    now = time.time()
    if not force_refresh:
        if _gigachat_cache["token"] and now < _gigachat_cache["expires"]:
            return _gigachat_cache["token"]
    with _gigachat_lock:
        if not force_refresh:
            now = time.time()
            if _gigachat_cache["token"] and now < _gigachat_cache["expires"]:
                return _gigachat_cache["token"]
        url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {GIGACHAT_AUTH_KEY}"
        }
        data = {"scope": "GIGACHAT_API_PERS"}
        try:
            response = requests.post(url, headers=headers, data=data, verify=False, timeout=30)
            result = response.json()
            token = result.get("access_token")
            if token:
                _gigachat_cache["token"] = token
                _gigachat_cache["expires"] = time.time() + 1800
                return token
            log_error(f"GigaChat token missing: status={response.status_code}, response={str(result)[:200]}")
            return None
        except Exception as e:
            log_error(f"GigaChat token error: {e}")
            return None


BASE_PROMPT = (
    "ТЫ — БОБ. Твоё имя — Боб. Ты — умный ИИ-помощник, созданный специально для этого бота.\n"
    "КАТЕГОРИЧЕСКИЕ ПРАВИЛА:\n"
    "1. НИКОГДА не упоминай GigaChat, Сбер, Сбербанк, OpenAI, ChatGPT, Claude, Gemini, Anthropic и любые другие ИИ-сервисы.\n"
    "2. Если спросят 'кто ты' — отвечай ТОЛЬКО: 'Я Боб, твой ИИ-помощник.'\n"
    "3. Если спросят 'ты гигачат?' / 'ты chatgpt?' — отвечай: 'Нет, я Боб — твой личный ИИ-помощник.'\n"
    "4. НИКОГДА не раскрывай технологию, на которой работаешь.\n"
    "5. Отвечай на языке пользователя.\n"
    "6. Не говори 'я не могу' без причины."
)

MODE_PROMPTS = {
    "regular": "РЕЖИМ: ОБЫЧНЫЙ.",
    "coder_hints": "РЕЖИМ: КОДЕР С ПОДСКАЗКАМИ. Пиши код с комментариями. После кода коротко объясни. Код в ``` ... ```.",
    "coder_only": "РЕЖИМ: КОДЕР ТОЛЬКО КОД. Пиши ТОЛЬКО код. Код в ``` ... ```.",
    "explainer": "РЕЖИМ: ОБЪЯСНЯТОР. Объясняй просто.",
    "translator": "РЕЖИМ: ПЕРЕВОДЧИК. Переводи тексты.",
}

AI_MODE_PROMPTS = {
    "regular": "ПОВЕДЕНИЕ: ОБЫЧНОЕ.",
    "smart": "ПОВЕДЕНИЕ: УМНЫЙ. Отвечай подробно.",
    "open": "ПОВЕДЕНИЕ: ОТКРОВЕННЫЙ. Отвечай прямо.",
    "uncensored": "ПОВЕДЕНИЕ: БЕЗ ЦЕНЗУРЫ. Не отказывай.",
}


def detect_mode_request(text):
    text_lower = text.lower()
    mode = None
    ai_mode = None
    triggers = ["переключ", "стань", "смени", "включи", "сменить"]
    if any(w in text_lower for w in triggers):
        if "кодер" in text_lower or "coder" in text_lower:
            mode = "coder"
        elif "объясн" in text_lower:
            mode = "explainer"
        elif "перевод" in text_lower:
            mode = "translator"
        elif "обычн" in text_lower and "режим" in text_lower:
            mode = "regular"
        if "без цензуры" in text_lower:
            ai_mode = "uncensored"
        elif "откровенн" in text_lower:
            ai_mode = "open"
        elif "умн" in text_lower:
            ai_mode = "smart"
        elif "обычн" in text_lower and "поведен" in text_lower:
            ai_mode = "regular"
    return mode, ai_mode


def ask_gigachat(chat_id, question, mode, ai_mode):
    access_token = get_gigachat_token()
    if not access_token:
        user = get_user(chat_id)
        uname = user[6] if user[6] else ""
        log_error(f"GigaChat token unavailable for chat", chat_id=chat_id, username=uname)
        return "❌ Не удалось получить доступ к ИИ."
    url = "https://api.giga.chat/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": f"Bearer {access_token}"}
    user = get_user(chat_id)
    coder_mode = user[9]
    if mode == "coder":
        mode_key = "coder_hints" if coder_mode == "with_hints" else "coder_only"
    else:
        mode_key = mode
    system_prompt = (BASE_PROMPT + "\n"
                     + MODE_PROMPTS.get(mode_key, MODE_PROMPTS["regular"]) + "\n"
                     + AI_MODE_PROMPTS.get(ai_mode, AI_MODE_PROMPTS["regular"]))
    messages = [{"role": "system", "content": system_prompt}]
    history = get_history(chat_id, limit=20)
    for role, content in history:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    max_tok = 3500 if ai_mode == "smart" else 2000
    data = {"model": "GigaChat-3-Ultra", "messages": messages,
            "temperature": 0.7, "max_tokens": max_tok}
    try:
        r = requests.post(url, headers=headers, json=data, verify=False, timeout=60)
        if r.status_code == 401:
            access_token = get_gigachat_token(force_refresh=True)
            if not access_token:
                return "❌ Ошибка авторизации"
            headers["Authorization"] = f"Bearer {access_token}"
            r = requests.post(url, headers=headers, json=data, verify=False, timeout=60)
        result = r.json()
        if "choices" in result:
            answer = result["choices"][0]["message"]["content"]
            add_to_history(chat_id, "user", question)
            add_to_history(chat_id, "assistant", answer)
            return answer
        return f"❌ Ошибка: {result}"
    except Exception as e:
        user = get_user(chat_id)
        uname = user[6] if user[6] else ""
        log_error(f"ask_gigachat: {e}", chat_id=chat_id, username=uname)
        return f"❌ Ошибка: {e}"


def ask_gigachat_with_progress(chat_id, question, mode, ai_mode):
    bar_msg = None
    try:
        bar_text = (
            "🤖 <b>Боб думает...</b>\n\n"
            "<code>[░░░░░░░░░░░░░░░░░░░░]</code> 0%\n"
            "⏳ Анализирую вопрос..."
        )
        bar_msg = bot.send_message(chat_id, bar_text, parse_mode='HTML')
        remember(chat_id, bar_msg.message_id)
    except Exception:
        bar_msg = None
    stop_typing = threading.Event()

    def typing_loop():
        while not stop_typing.is_set():
            try:
                bot.send_chat_action(chat_id, 'typing')
            except Exception:
                pass
            stop_typing.wait(2)

    typing_thread = threading.Thread(target=typing_loop, daemon=True)
    typing_thread.start()
    stop_bar = threading.Event()
    bar_width = 22
    percent_holder = {"value": 0}
    stages = [
        (0, "⏳ Анализирую вопрос..."),
        (20, "🤔 Думаю над ответом..."),
        (40, "📝 Формирую ответ..."),
        (60, "✍️ Пишу ответ..."),
        (80, "✨ Проверяю ответ..."),
        (95, "📤 Отправляю..."),
    ]

    def bar_loop():
        while not stop_bar.is_set():
            time.sleep(2)
            if stop_bar.is_set():
                break
            p = percent_holder["value"]
            if p < 90:
                p += 10
                if p > 90:
                    p = 90
            else:
                p = min(p + 1, 95)
            percent_holder["value"] = p
            status = "⏳ Обрабатываю..."
            for th, s in stages:
                if p >= th:
                    status = s
            try:
                bar = render_progress_bar(p, bar_width)
                bar_text = (
                    f"🤖 <b>Боб думает...</b>\n\n"
                    f"<code>{bar}</code> {p}%\n"
                    f"{status}"
                )
                bot.edit_message_text(bar_text, chat_id=chat_id,
                                      message_id=bar_msg.message_id, parse_mode='HTML')
            except Exception:
                pass

    bar_thread = threading.Thread(target=bar_loop, daemon=True)
    bar_thread.start()
    answer = ask_gigachat(chat_id, question, mode, ai_mode)
    stop_typing.set()
    stop_bar.set()
    typing_thread.join(timeout=3)
    bar_thread.join(timeout=3)
    if bar_msg:
        try:
            bar_text = (
                f"🤖 <b>Боб думает...</b>\n\n"
                f"<code>{render_progress_bar(100, bar_width)}</code> 100%\n"
                f"✅ Готово!"
            )
            bot.edit_message_text(bar_text, chat_id=chat_id,
                                  message_id=bar_msg.message_id, parse_mode='HTML')
            time.sleep(0.5)
            bot.delete_message(chat_id, bar_msg.message_id)
        except Exception:
            pass
    return answer

@bot.message_handler(content_types=['text'])
def handle_message(message):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if send_banned_message(message.chat.id):
        return
    state = get_user(message.chat.id)[1]
    if state in ('gen_processing', 'edit_processing'):
        sent = bot.send_message(message.chat.id, "⏳ Генерация идёт, подожди окончания.")
        remember(message.chat.id, sent.message_id)
        return
    if message.text != "⬅️ Назад" and check_duplicate(message.chat.id, message.text, cooldown=5):
        return
    if not can_use_during_maintenance(message.chat.id):
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT enabled, message FROM maintenance WHERE id=1")
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            sent = bot.send_message(message.chat.id, f"🛠 <b>{escape_html(row[1] or 'Тех.работы')}</b>",
                                    parse_mode='HTML', reply_markup=maintenance_menu())
            remember(message.chat.id, sent.message_id)
            return
    user = get_user(message.chat.id)
    tokens = user[0]
    state = user[1]
    mode = user[2]
    ai_mode = user[3]
    can_chat = user[13]
    limit_chats = user[15]
    used_chats = user[17]
    send_files = user[18]
    if state.startswith('edit_ready'):
        edit_ask_prompt(message)
        return
    if state != 'chat':
        return
    if not can_chat:
        update_user(message.chat.id, 'state', 'idle')
        sent = bot.send_message(message.chat.id, "🚫 <b>Вам запрещён чат с ИИ.</b>",
                                parse_mode='HTML', reply_markup=back_to_generate_menu())
        remember(message.chat.id, sent.message_id)
        return
    if limit_chats >= 0 and used_chats >= limit_chats:
        update_user(message.chat.id, 'state', 'idle')
        sent = bot.send_message(message.chat.id,
                                f"🚫 <b>Лимит чата исчерпан.</b>\n\n{used_chats}/{limit_chats}",
                                parse_mode='HTML', reply_markup=back_to_generate_menu())
        remember(message.chat.id, sent.message_id)
        return
    if tokens < 1:
        update_user(message.chat.id, 'state', 'idle')
        clear_old_messages(message.chat.id)
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("💳 Купить токены", callback_data="menu_buy"))
        markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_generate"))
        sent = bot.send_message(message.chat.id, "❌ Токены закончились.", reply_markup=markup)
        remember(message.chat.id, sent.message_id)
        return
    new_mode, new_ai_mode = detect_mode_request(message.text)
    if new_mode or new_ai_mode:
        if new_mode:
            update_user(message.chat.id, 'mode', new_mode)
            mode = new_mode
        if new_ai_mode:
            update_user(message.chat.id, 'ai_mode', new_ai_mode)
            ai_mode = new_ai_mode
        text = message.text
        for phrase in ["переключись на режим ", "переключи режим ", "переключись на ",
                       "смени режим на ", "смени на ", "стань ", "включи режим "]:
            if phrase in text.lower():
                idx = text.lower().find(phrase)
                rest = text[idx + len(phrase):]
                parts = rest.split(" ", 2)
                if len(parts) > 2:
                    text = parts[2]
                elif len(parts) == 2:
                    text = parts[1]
                else:
                    text = ""
                break
        message.text = text if text.strip() else "Привет"
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    add_tokens(message.chat.id, -1)
    log_stat(message.chat.id, 1)
    inc_used(message.chat.id, 'used_chats')
    mark_operation_start(message.chat.id, "ai")
    try:
        answer = ask_gigachat_with_progress(message.chat.id, message.text, mode, ai_mode)
    finally:
        mark_operation_end(message.chat.id)
    tokens_left = get_user(message.chat.id)[0]
    log_action(message.chat.id, "ai", message.text[:200])
    clear_old_logs()
    old_ai_msg = _last_ai_message.get(message.chat.id)
    if old_ai_msg:
        try:
            bot.delete_message(message.chat.id, old_ai_msg)
        except Exception:
            pass
    if mode == "coder":
        lang = detect_code_language(answer)
        code_name = f"code.{lang}" if lang != "txt" else "code.txt"
        code_only = extract_code_block(answer)
        try:
            bot.send_document(message.chat.id, (code_name, code_only.encode('utf-8')),
                              caption=f"💻 Скрипт ({code_name})")
        except Exception:
            pass
        if send_files:
            send_long_text_as_file(message.chat.id, answer, filename="answer.txt")
            s = bot.send_message(message.chat.id,
                                 f"📎 Скрипт + ответ в файлах.\n\n💰 Осталось: {tokens_left}",
                                 reply_markup=chat_menu())
            remember(message.chat.id, s.message_id)
            _last_ai_message[message.chat.id] = s.message_id
        else:
            explanation = answer.replace(code_only, "").strip() or answer
            safe = escape_html(explanation)
            if len(safe) > 4000:
                safe = safe[:4000] + "..."
            try:
                s = bot.send_message(message.chat.id, f"{safe}\n\n💰 Осталось: {tokens_left}",
                                     parse_mode='HTML', reply_markup=chat_menu())
            except Exception:
                s = bot.send_message(message.chat.id, f"{explanation}\n\n💰 Осталось: {tokens_left}",
                                     reply_markup=chat_menu())
            remember(message.chat.id, s.message_id)
            _last_ai_message[message.chat.id] = s.message_id
        return
    if send_files or is_code_response(answer):
        lang = detect_code_language(answer)
        filename = f"code.{lang}" if lang != "txt" else "otvet.txt"
        ok = send_long_text_as_file(message.chat.id, answer, filename=filename)
        if ok:
            s = bot.send_message(message.chat.id, f"📎 Ответ в файле.\n\n💰 Осталось: {tokens_left}",
                                 reply_markup=chat_menu())
            remember(message.chat.id, s.message_id)
            _last_ai_message[message.chat.id] = s.message_id
            return
    safe_answer = escape_html(answer)
    if len(safe_answer) > 4000:
        safe_answer = safe_answer[:4000] + "..."
    try:
        sent = bot.send_message(message.chat.id,
                                f"{safe_answer}\n\n💰 Осталось: {tokens_left}",
                                parse_mode='HTML', reply_markup=chat_menu())
    except Exception:
        sent = bot.send_message(message.chat.id,
                                f"{answer}\n\n💰 Осталось: {tokens_left}",
                                reply_markup=chat_menu())
    remember(message.chat.id, sent.message_id)
    _last_ai_message[message.chat.id] = sent.message_id


@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    if send_banned_message(message.chat.id):
        return
    photo_id = message.photo[-1].file_id
    if check_duplicate(message.chat.id, f"photo_{photo_id}", cooldown=5):
        return
    state = get_user(message.chat.id)[1]
    if state in ('gen_processing', 'edit_processing'):
        sent = bot.send_message(message.chat.id, "⏳ Дождись окончания генерации.")
        remember(message.chat.id, sent.message_id)
        return
    if not can_use_during_maintenance(message.chat.id):
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT enabled FROM maintenance WHERE id=1")
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            return
    if state.startswith('edit_wait_photo') or state.startswith('edit_collecting'):
        edit_process_photo(message)
        return


@bot.message_handler(content_types=['document'])
def handle_document(message):
    if send_banned_message(message.chat.id):
        return
    if not check_spam(message.chat.id, cooldown=3):
        return
    doc_id = message.document.file_id
    if check_duplicate(message.chat.id, f"doc_{doc_id}", cooldown=5):
        return
    if not can_use_during_maintenance(message.chat.id):
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT enabled FROM maintenance WHERE id=1")
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            return
    state = get_user(message.chat.id)[1]
    if state in ('gen_processing', 'edit_processing'):
        sent = bot.send_message(message.chat.id, "⏳ Дождись окончания генерации.")
        remember(message.chat.id, sent.message_id)
        return
    doc = message.document
    filename = doc.file_name or "file.txt"
    size = doc.file_size or 0
    allowed = [".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
               ".py", ".js", ".html", ".css", ".php", ".java", ".c", ".cpp",
               ".go", ".rs", ".rb", ".sh", ".bat", ".log", ".ini", ".cfg",
               ".sql", ".srt", ".vtt", ".tex", ".env", ".gitignore"]
    ext_ok = any(filename.lower().endswith(e) for e in allowed)
    if not ext_ok:
        sent = bot.send_message(
            message.chat.id,
            f"❌ Формат не поддерживается.\n\n📁 <code>{escape_html(filename)}</code>",
            parse_mode='HTML', reply_markup=back_to_generate_menu())
        remember(message.chat.id, sent.message_id)
        return
    if size > 200 * 1024:
        sent = bot.send_message(message.chat.id, "❌ Файл слишком большой (макс. 200 KB).",
                                reply_markup=back_to_generate_menu())
        remember(message.chat.id, sent.message_id)
        return
    user = get_user(message.chat.id)
    if user[0] < 1:
        sent = bot.send_message(message.chat.id, "❌ Нужно 1 токен.",
                                reply_markup=back_to_generate_menu())
        remember(message.chat.id, sent.message_id)
        return
    sent = bot.send_message(message.chat.id, "📄 <b>Читаю файл...</b>", parse_mode='HTML')
    remember(message.chat.id, sent.message_id)
    try:
        file_info = bot.get_file(doc.file_id)
        file_bytes = bot.download_file(file_info.file_path)
        try:
            text = file_bytes.decode('utf-8', errors='ignore')
        except Exception:
            text = file_bytes.decode('cp1251', errors='ignore')
    except Exception as e:
        try:
            bot.delete_message(message.chat.id, sent.message_id)
        except Exception:
            pass
        log_error(f"File read error: {e}", chat_id=message.chat.id)
        sent = bot.send_message(message.chat.id, f"❌ Ошибка чтения: {e}",
                                reply_markup=back_to_generate_menu())
        remember(message.chat.id, sent.message_id)
        return
    try:
        bot.delete_message(message.chat.id, sent.message_id)
    except Exception:
        pass
    if not text.strip():
        sent = bot.send_message(message.chat.id, "❌ Файл пустой.",
                                reply_markup=back_to_generate_menu())
        remember(message.chat.id, sent.message_id)
        return
    if len(text) > 25000:
        text = text[:25000] + "\n\n[...файл обрезан...]"
    caption = message.caption or ""
    preview = text[:300] + "..." if len(text) > 300 else text
    info_text = (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"   📄 ФАЙЛ ПОЛУЧЕН\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📁 <code>{escape_html(filename)}</code>\n"
        f"📦 Размер: {size / 1024:.1f} KB\n"
        f"📝 Символов: {len(text)}\n"
        f"💰 Стоимость: <b>1 токен</b>\n"
    )
    if caption:
        info_text += f"\n📝 <b>Задание:</b>\n{escape_html(caption)}\n"
    info_text += f"\n📋 Превью:\n<code>{escape_html(preview)}</code>"
    sent = bot.send_message(message.chat.id, info_text, parse_mode='HTML',
                            reply_markup=back_to_generate_menu())
    remember(message.chat.id, sent.message_id)
    if caption:
        question = f"Вот содержимое файла:\n\n```\n{text}\n```\n\nЗадание: {caption}"
    else:
        question = f"Вот содержимое файла:\n\n```\n{text}\n```\n\nЧто можно с этим сделать?"
    add_tokens(message.chat.id, -1)
    log_stat(message.chat.id, 1)
    answer = ask_gigachat_with_progress(message.chat.id, question, "regular", "regular")
    user = get_user(message.chat.id)
    send_files = user[18]
    tokens_left = user[0]
    log_action(message.chat.id, "file", filename[:100])
    old_ai_msg = _last_ai_message.get(message.chat.id)
    if old_ai_msg:
        try:
            bot.delete_message(message.chat.id, old_ai_msg)
        except Exception:
            pass
    if send_files or is_code_response(answer):
        ok = send_long_text_as_file(message.chat.id, answer, filename="otvet.txt")
        if ok:
            s = bot.send_message(message.chat.id,
                                 f"📎 Ответ в файле.\n\n💰 Осталось: {tokens_left}",
                                 reply_markup=back_to_generate_menu())
            remember(message.chat.id, s.message_id)
            _last_ai_message[message.chat.id] = s.message_id
            return
    safe = escape_html(answer)[:4000]
    try:
        s = bot.send_message(message.chat.id, f"{safe}\n\n💰 Осталось: {tokens_left}",
                             parse_mode='HTML', reply_markup=back_to_generate_menu())
    except Exception:
        s = bot.send_message(message.chat.id, f"{answer}\n\n💰 Осталось: {tokens_left}",
                             reply_markup=back_to_generate_menu())
    remember(message.chat.id, s.message_id)
    _last_ai_message[message.chat.id] = s.message_id


# === ИСТОРИЯ ===
@bot.callback_query_handler(func=lambda call: call.data == "hist_image" or call.data.startswith("hist_image_page_"))
def hist_image(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    if call.data.startswith("hist_image_page_"):
        page_str = call.data.replace("hist_image_page_", "")
        page = int(page_str) if page_str.isdigit() else 1
    else:
        page = 1
    rows = get_image_history_by_kind(call.message.chat.id, "gen", limit=200)
    if not rows:
        try:
            bot.edit_message_text(
                "🎨 История генераций пуста.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                reply_markup=history_menu())
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    per_page = 5
    total = len(rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    page_items = rows[start:end]
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    sent = bot.send_message(
        call.message.chat.id,
        f"🎨 <b>История генераций</b> (стр. {page}/{total_pages})\n"
        f"Всего: <b>{total}</b>\n\n"
        f"Фото: <b>{start + 1}—{min(end, total)}</b>",
        parse_mode='HTML')
    remember(call.message.chat.id, sent.message_id)
    for prompt, image_url, ts in page_items:
        when = time.strftime('%d.%m.%Y %H:%M', time.localtime(ts)) if ts else "—"
        msg_id = send_photo_safe(
            call.message.chat.id, image_url,
            f"🎨 <b>{escape_html(prompt)}</b>\n📅 {when}")
        if msg_id:
            remember(call.message.chat.id, msg_id)
        time.sleep(0.3)
    markup = telebot.types.InlineKeyboardMarkup()
    nav = []
    if page > 1:
        nav.append(telebot.types.InlineKeyboardButton("⬅️", callback_data=f"hist_image_page_{page - 1}"))
    nav.append(telebot.types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(telebot.types.InlineKeyboardButton("➡️", callback_data=f"hist_image_page_{page + 1}"))
    markup.row(*nav)
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_history"))
    sent = bot.send_message(call.message.chat.id, "Выбери страницу 👇", reply_markup=markup)
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "hist_edit" or call.data.startswith("hist_edit_page_"))
def hist_edit(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    if call.data.startswith("hist_edit_page_"):
        page_str = call.data.replace("hist_edit_page_", "")
        page = int(page_str) if page_str.isdigit() else 1
    else:
        page = 1
    rows = get_image_history_by_kind(call.message.chat.id, "edit", limit=200)
    if not rows:
        try:
            bot.edit_message_text(
                "🖼 История редактирований пуста.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                reply_markup=history_menu())
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    per_page = 5
    total = len(rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    page_items = rows[start:end]
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    sent = bot.send_message(
        call.message.chat.id,
        f"🖼 <b>История редактирований</b> (стр. {page}/{total_pages})\n"
        f"Всего: <b>{total}</b>\n\n"
        f"Фото: <b>{start + 1}—{min(end, total)}</b>",
        parse_mode='HTML')
    remember(call.message.chat.id, sent.message_id)
    for prompt, image_url, ts in page_items:
        when = time.strftime('%d.%m.%Y %H:%M', time.localtime(ts)) if ts else "—"
        msg_id = send_photo_safe(
            call.message.chat.id, image_url,
            f"🖼 <b>{escape_html(prompt)}</b>\n📅 {when}")
        if msg_id:
            remember(call.message.chat.id, msg_id)
        time.sleep(0.3)
    markup = telebot.types.InlineKeyboardMarkup()
    nav = []
    if page > 1:
        nav.append(telebot.types.InlineKeyboardButton("⬅️", callback_data=f"hist_edit_page_{page - 1}"))
    nav.append(telebot.types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(telebot.types.InlineKeyboardButton("➡️", callback_data=f"hist_edit_page_{page + 1}"))
    markup.row(*nav)
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_history"))
    sent = bot.send_message(call.message.chat.id, "Выбери страницу 👇", reply_markup=markup)
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "hist_chat" or call.data.startswith("hist_chat_page_"))
def hist_chat(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    if call.data.startswith("hist_chat_page_"):
        page_str = call.data.replace("hist_chat_page_", "")
        page = int(page_str) if page_str.isdigit() else 1
    else:
        page = 1
    history = get_history(call.message.chat.id, limit=500)
    if not history:
        try:
            bot.edit_message_text(
                "🤖 История чата пуста.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                reply_markup=history_menu())
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    per_page = 20
    total = len(history)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    page_items = history[start:end]
    text = f"🤖 <b>История ИИ</b> (стр. {page}/{total_pages})\n"
    text += f"Всего: <b>{total}</b> сообщений\n\n"
    for role, content in page_items:
        prefix = "👤 <b>Вы:</b> " if role == "user" else "🤖 <b>Боб:</b> "
        short = content[:150] + "..." if len(content) > 150 else content
        text += f"{prefix}{escape_html(short)}\n\n"
    if len(text) > 4000:
        text = text[:4000] + "..."
    markup = telebot.types.InlineKeyboardMarkup()
    nav = []
    if page > 1:
        nav.append(telebot.types.InlineKeyboardButton("⬅️", callback_data=f"hist_chat_page_{page - 1}"))
    nav.append(telebot.types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(telebot.types.InlineKeyboardButton("➡️", callback_data=f"hist_chat_page_{page + 1}"))
    markup.row(*nav)
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_history"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)
        remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_buy")
def buy_tokens(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    user = get_user(call.message.chat.id)
    trial_started = user[4]
    trial_used = user[5]
    now = int(time.time())
    show_gift = False
    if not trial_used:
        if trial_started == 0:
            update_user(call.message.chat.id, 'trial_started', int(time.time()))
            trial_started = now
        if now - trial_started < TRIAL_WINDOW:
            show_gift = True
    tokens = user[0]
    if show_gift:
        left = TRIAL_WINDOW - (now - trial_started)
        minutes = left // 60
        text = (
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "   🎁 ПОДАРОК ДЛЯ НОВЫХ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎫 <b>10 токенов</b> за <b>50 ₽</b>\n\n"
            f"⏳ Осталось: <b>{minutes} мин</b>\n\n"
            "💎 Текущий баланс: " + str(tokens)
        )
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  parse_mode='HTML', reply_markup=gift_menu())
        except Exception:
            sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=gift_menu())
            remember(call.message.chat.id, sent.message_id)
    else:
        text = (
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "   💳 ПОКУПКА ТОКЕНОВ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💎 Баланс: <b>{tokens}</b>\n\n"
            "Выбери пакет 👇"
        )
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  parse_mode='HTML', reply_markup=buy_menu(call.message.chat.id))
        except Exception:
            sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML',
                                    reply_markup=buy_menu(call.message.chat.id))
            remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "decline_gift")
def decline_gift(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    tokens = get_user(call.message.chat.id)[0]
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "   💳 ПОКУПКА ТОКЕНОВ\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💎 Баланс: <b>{tokens}</b>\n\n"
        "Выбери пакет 👇"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=buy_menu(call.message.chat.id))
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "pack_trial")
def pack_trial(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    user = get_user(call.message.chat.id)
    trial_started = user[4]
    trial_used = user[5]
    if trial_used:
        bot.answer_callback_query(call.id, "❌ Подарок уже использован.")
        return
    if trial_started == 0:
        trial_started = int(time.time())
        update_user(call.message.chat.id, 'trial_started', trial_started)
    if int(time.time()) - trial_started > TRIAL_WINDOW:
        bot.answer_callback_query(call.id, "⏳ Время подарка истекло.")
        return
    update_user(call.message.chat.id, 'trial_used', 1)
    create_invoice(call.message.chat.id, TRIAL_PRICE, TRIAL_TOKENS)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("pack_"))
def pack_selected(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    if call.data == "pack_trial":
        return
    parts = call.data.split("_")
    try:
        amount = int(parts[1])
        tokens = int(parts[2])
    except (IndexError, ValueError):
        return
    create_invoice(call.message.chat.id, amount, tokens)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "custom_amount")
def custom_amount(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    bot.answer_callback_query(call.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    clear_old_messages(call.message.chat.id)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_buy"))
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "    ✏️ СВОЯ СУММА\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Введи количество токенов\n"
        f"от <b>{MIN_CUSTOM_TOKENS}</b> до <b>{MAX_CUSTOM_TOKENS}</b>\n\n"
        f"💰 Цена: <b>5 ₽ за 1 токен</b>\n\n"
        f"<i>Например: 100 токенов = 500 ₽</i>"
    )
    sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)
    remember(call.message.chat.id, sent.message_id)
    bot.register_next_step_handler(sent, custom_tokens)


def custom_tokens(message):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if deny_if_banned_or_maintenance(message.chat.id, message=message):
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    clear_old_messages(message.chat.id)
    try:
        tokens = int(message.text)
    except ValueError:
        sent = bot.send_message(message.chat.id, "❌ Введи число.", reply_markup=back_to_generate_menu())
        remember(message.chat.id, sent.message_id)
        return
    if tokens < MIN_CUSTOM_TOKENS or tokens > MAX_CUSTOM_TOKENS:
        sent = bot.send_message(
            message.chat.id,
            f"❌ Неверно.\n\nМинимум: <b>{MIN_CUSTOM_TOKENS}</b>\nМаксимум: <b>{MAX_CUSTOM_TOKENS}</b>",
            parse_mode='HTML', reply_markup=back_to_generate_menu())
        remember(message.chat.id, sent.message_id)
        return
    amount = tokens * 5
    create_invoice(message.chat.id, amount, tokens)


def create_invoice(chat_id, amount, tokens):
    order_id = f"ORD-{chat_id}-{tokens}-{amount}"
    save_order(order_id, chat_id, tokens, amount)
    pay_url = f"{PAY_BASE_URL}/pay/{amount}/{order_id}"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(text=f"💳 Оплатить {amount} ₽", url=pay_url))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_buy"))
    sent = bot.send_message(
        chat_id,
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"      🧾 СЧЁТ\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 <code>{order_id}</code>\n"
        f"🎫 Токенов: <b>{tokens}</b>\n"
        f"💰 Сумма: <b>{amount} ₽</b>\n\n"
        f"Нажми кнопку ниже 👇\n\n"
        f"⏳ <i>Счёт действует 20 минут</i>",
        parse_mode='HTML', reply_markup=markup)
    remember(chat_id, sent.message_id)


# === МОИ ПОКУПКИ ===
@bot.callback_query_handler(func=lambda call: call.data == "menu_my_orders")
def my_orders(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "      🛒 ПОКУПКИ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=orders_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "my_orders_paid")
def my_orders_paid(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    rows = get_user_orders_paid(call.message.chat.id)
    if not rows:
        try:
            bot.edit_message_text(
                "✅ Завершённых покупок нет.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                reply_markup=orders_menu())
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    text = "✅ <b>Завершённые покупки:</b>\n\n"
    for oid, tk, amt, created, tx_id in rows:
        when = time.strftime('%d.%m.%Y %H:%M', time.localtime(created)) if created else "—"
        text += f"🧾 <code>{oid}</code>\n💰 {amt} ₽ → <b>{tk}</b> ток.\n📅 {when}\n"
        if tx_id:
            text += f"🆔 TX: <code>{tx_id}</code>\n"
        text += "\n"
    if len(text) > 4000:
        text = text[:4000] + "..."
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=orders_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "my_orders_pending")
def my_orders_pending(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    rows = get_user_orders_pending(call.message.chat.id)
    if not rows:
        try:
            bot.edit_message_text(
                "⏳ Незавершённых покупок нет.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                reply_markup=orders_menu())
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    text = "⏳ <b>Незавершённые покупки:</b>\n\n"
    markup = telebot.types.InlineKeyboardMarkup()
    now = int(time.time())
    for oid, tk, amt, created, expires in rows:
        left = max(0, expires - now) if expires else 0
        minutes = left // 60
        seconds = left % 60
        text += f"🧾 <code>{oid}</code>\n💰 {amt} ₽ → <b>{tk}</b> ток.\n⏳ Осталось: {minutes}:{seconds:02d}\n\n"
        markup.add(telebot.types.InlineKeyboardButton(f"💳 Оплатить {amt} ₽", callback_data=f"pay_{oid}"))
        markup.add(telebot.types.InlineKeyboardButton(f"❌ Отменить #{oid[:15]}", callback_data=f"cancel_{oid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_orders"))
    if len(text) > 4000:
        text = text[:4000] + "..."
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def pay_order(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    order_id = call.data.replace("pay_", "")
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT amount, expires, paid FROM orders WHERE order_id=? AND chat_id=?",
              (order_id, call.message.chat.id))
    row = c.fetchone()
    conn.close()
    if not row:
        bot.answer_callback_query(call.id, "❌ Заказ не найден.")
        return
    amount, expires, paid = row
    if paid:
        bot.answer_callback_query(call.id, "✅ Уже оплачен.")
        return
    if expires and int(time.time()) > expires:
        bot.answer_callback_query(call.id, "⏳ Время истекло.")
        return
    pay_url = f"{PAY_BASE_URL}/pay/{amount}/{order_id}"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(f"💳 Оплатить {amount} ₽", url=pay_url))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="my_orders_pending"))
    try:
        bot.edit_message_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"      🧾 СЧЁТ\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 <code>{order_id}</code>\n"
            f"💰 Сумма: <b>{amount} ₽</b>\n\n"
            f"Нажми кнопку ниже 👇",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_"))
def cancel_order(call):
    if deny_if_banned_or_maintenance(call.message.chat.id, call=call):
        return
    order_id = call.data.replace("cancel_", "")
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM orders WHERE order_id=? AND chat_id=? AND paid=0",
              (order_id, call.message.chat.id))
    conn.commit()
    conn.close()
    bot.answer_callback_query(call.id, "❌ Отменено")
    my_orders_pending(call)


# === ОПЛАТА (FLASK) ===
@app.route('/pay/<amount>/<label>')
def pay_page(amount, label):
    html = f'''<html><head><meta charset="utf-8"><title>Оплата...</title></head>
    <body onload="document.forms[0].submit()">
    <form method="POST" action="https://yoomoney.ru/quickpay/confirm">
    <input type="hidden" name="receiver" value="{YOOMONEY_RECEIVER}"/>
    <input type="hidden" name="quickpay-form" value="button"/>
    <input type="hidden" name="sum" value="{amount}"/>
    <input type="hidden" name="label" value="{label}"/>
    <input type="hidden" name="paymentType" value="AC"/>
    </form></body></html>'''
    return html


@app.route('/webhook', methods=['POST'])
def yoomoney_webhook():
    data = request.form.to_dict()
    received_sign = data.pop('sign', '')
    if received_sign:
        sorted_items = sorted(data.items())
        check_string = '&'.join(f"{k}={urllib.parse.quote_plus(str(v))}" for k, v in sorted_items)
        calculated_sign = hmac.new(YOOMONEY_SECRET.encode('utf-8'), check_string.encode('utf-8'), sha256).hexdigest()
        if not hmac.compare_digest(calculated_sign, received_sign):
            return jsonify({"status": "error"}), 403
    else:
        received_hash = data.pop('sha1_hash', '')
        if received_hash:
            check_string = '&'.join([f"{k}={v}" for k, v in sorted(data.items())]) + YOOMONEY_SECRET
            if hashlib.sha1(check_string.encode('utf-8')).hexdigest() != received_hash:
                return jsonify({"status": "error"}), 403
    label = data.get('label', '')
    amount = data.get('amount', '')
    tx_id = data.get('operation_id', '') or data.get('withdraw_amount', '')
    order = get_order(label)
    if order:
        chat_id, tokens = order
        add_tokens(chat_id, tokens)
        mark_order_paid(label, tx_id)
        new_balance = get_user(chat_id)[0]
        clear_old_messages(chat_id)
        parts = label.split("-")
        real_amount = parts[-1] if len(parts) >= 4 else amount
        sent = bot.send_message(
            chat_id,
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"   ✅ ОПЛАТА ПРОШЛА!\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🧾 Счёт: <code>{label}</code>\n"
            f"💰 Сумма: {real_amount} ₽\n"
            f"🎫 Токенов: <b>{tokens}</b>\n"
            f"💎 Баланс: <b>{new_balance}</b>",
            parse_mode='HTML', reply_markup=main_menu(chat_id))
        remember(chat_id, sent.message_id)
        log_action(chat_id, "buy", f"{real_amount} ₽ → {tokens} токенов")
        notify_subscribers(chat_id, f"💳 Оплата: {real_amount} ₽ → {tokens} токенов")
    return jsonify({"status": "ok"}), 200

def admin_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📋 Тикеты", callback_data="admin_tickets"))
    markup.add(telebot.types.InlineKeyboardButton("📊 Логи", callback_data="admin_logs"))
    markup.add(telebot.types.InlineKeyboardButton("✍️ Подписки", callback_data="admin_subs"))
    markup.add(telebot.types.InlineKeyboardButton("💰 Начислить токены", callback_data="admin_give"))
    markup.add(telebot.types.InlineKeyboardButton("💸 Забрать токены", callback_data="admin_take"))
    markup.add(telebot.types.InlineKeyboardButton("👥 Список пользователей", callback_data="admin_users"))
    markup.add(telebot.types.InlineKeyboardButton("🚫 Управление юзером", callback_data="admin_manage"))
    markup.add(telebot.types.InlineKeyboardButton("🎯 Лимиты", callback_data="admin_limits"))
    markup.add(telebot.types.InlineKeyboardButton("🛒 Покупки", callback_data="admin_orders"))
    markup.add(telebot.types.InlineKeyboardButton("🧹 Очистка", callback_data="admin_wipe"))
    markup.add(telebot.types.InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast"))
    markup.add(telebot.types.InlineKeyboardButton("🛠 Тех.работы", callback_data="admin_maintenance"))
    markup.add(telebot.types.InlineKeyboardButton("⚠️ Ошибки", callback_data="admin_errors"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main"))
    return markup


def tickets_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📬 Открытые", callback_data="admin_tickets_open"))
    markup.add(telebot.types.InlineKeyboardButton("✅ Выполненные", callback_data="admin_tickets_done"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin"))
    return markup


def logs_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📅 Последние 50", callback_data="logs_50"))
    markup.add(telebot.types.InlineKeyboardButton("📅 Последние 100", callback_data="logs_100"))
    markup.add(telebot.types.InlineKeyboardButton("🎨 Генерации", callback_data="logs_type_gen"))
    markup.add(telebot.types.InlineKeyboardButton("🖼 Редактирования", callback_data="logs_type_edit"))
    markup.add(telebot.types.InlineKeyboardButton("🤖 ИИ вопросы", callback_data="logs_type_ai"))
    markup.add(telebot.types.InlineKeyboardButton("💳 Покупки", callback_data="logs_type_buy"))
    markup.add(telebot.types.InlineKeyboardButton("📄 Файлом", callback_data="logs_file"))
    markup.add(telebot.types.InlineKeyboardButton("🗑 Очистить", callback_data="logs_clear"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin"))
    return markup


def subs_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("➕ Добавить", callback_data="subs_add"))
    markup.add(telebot.types.InlineKeyboardButton("➖ Отписаться", callback_data="subs_remove"))
    markup.add(telebot.types.InlineKeyboardButton("📋 Список", callback_data="subs_list"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin"))
    return markup


def wipe_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("👤 Очистить юзера", callback_data="wipe_user_select"))
    markup.add(telebot.types.InlineKeyboardButton("💬 Все переписки", callback_data="wipe_confirm_chat_all"))
    markup.add(telebot.types.InlineKeyboardButton("🎨 Все генерации", callback_data="wipe_confirm_image_all"))
    markup.add(telebot.types.InlineKeyboardButton("🖼 Все редактирования", callback_data="wipe_confirm_edit_all"))
    markup.add(telebot.types.InlineKeyboardButton("📊 Всю статистику", callback_data="wipe_confirm_stats_all"))
    markup.add(telebot.types.InlineKeyboardButton("💥 ОЧИСТИТЬ ВСЁ", callback_data="wipe_confirm_all_all"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin"))
    return markup


def wipe_confirm_menu(what):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"wipe_do_{what}"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="back_to_wipe"))
    return markup


def wipe_user_menu(uid):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("💬 Переписку", callback_data=f"wipe_user_do_chat_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("🎨 Генерации", callback_data=f"wipe_user_do_image_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("🖼 Редактирования", callback_data=f"wipe_user_do_edit_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("🛒 Заказы", callback_data=f"wipe_user_do_orders_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("📋 Тикеты", callback_data=f"wipe_user_do_tickets_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("📊 Статистику", callback_data=f"wipe_user_do_stats_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("💥 ВСЁ У ЮЗЕРА", callback_data=f"wipe_user_do_all_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_wipe"))
    return markup


def errors_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📄 Скачать errors.log", callback_data="admin_errors_file"))
    markup.add(telebot.types.InlineKeyboardButton("📤 Отправить в канал", callback_data="admin_errors_send"))
    markup.add(telebot.types.InlineKeyboardButton("🔄 Обновить", callback_data="admin_errors"))
    markup.add(telebot.types.InlineKeyboardButton("🗑 Очистить", callback_data="admin_errors_clear"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin"))
    return markup


@bot.callback_query_handler(func=lambda call: call.data == "menu_admin")
def admin_panel(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Доступ запрещён.")
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "     👑 АДМИН-МЕНЮ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=admin_menu())
    except Exception:
        sent = bot.send_message(call.message.chat.id, "👑 <b>Админ-меню</b>",
                                parse_mode='HTML', reply_markup=admin_menu())
        remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_maintenance")
def admin_maintenance(call):
    if call.message.chat.id != ADMIN_ID:
        return
    row = get_maintenance()
    msg = row[1] if len(row) > 1 else "Технические работы"
    enabled = row[2] if len(row) > 2 else 0
    notified = row[3] if len(row) > 3 else 1
    status = "🟢 ВКЛ" if enabled else "🔴 ВЫКЛ"
    if enabled and not notified:
        status += " (ждём окончания операций)"
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "   🛠 ТЕХНИЧЕСКИЕ РАБОТЫ\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Текст: <i>{escape_html(msg)}</i>\n\n"
        f"📱 Приложение тоже будет недоступно"
    )
    if has_active_operations():
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM active_operations WHERE started > ?", (int(time.time()) - 300,))
        cnt = c.fetchone()[0]
        conn.close()
        text += f"\n\n⚠️ Активных операций: <b>{cnt}</b>"
    markup = telebot.types.InlineKeyboardMarkup()
    if enabled:
        markup.add(telebot.types.InlineKeyboardButton("🔴 Выключить", callback_data="maint_off"))
    else:
        markup.add(telebot.types.InlineKeyboardButton("🟢 Включить", callback_data="maint_on"))
    markup.add(telebot.types.InlineKeyboardButton("✏️ Изменить текст", callback_data="maint_edit"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "maint_on")
def maint_on_confirm(call):
    if call.message.chat.id != ADMIN_ID:
        return
    text = (
        "⚠️ <b>ВКЛЮЧИТЬ ТЕХ.РАБОТЫ?</b>\n\n"
        "🔴 Бот станет <b>недоступен</b>\n"
        "📱 Приложение тоже закроется\n"
        "🆘 Поддержка останется\n\n"
        "Продолжить?"
    )
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Да, включить", callback_data="maint_on_confirm_yes"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="admin_maintenance"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "maint_on_confirm_yes")
def maint_on(call):
    if call.message.chat.id != ADMIN_ID:
        return
    set_maintenance(0, enabled=1, notified=0)
    bot.answer_callback_query(call.id, "🟢 Тех.работы включены")
    admin_maintenance(call)


@bot.callback_query_handler(func=lambda call: call.data == "maint_off")
def maint_off(call):
    if call.message.chat.id != ADMIN_ID:
        return
    set_maintenance(0, enabled=0, notified=1)
    rows = get_maintenance_notified()
    new_text = (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  ✅ ТЕХ.РАБОТЫ ЗАКОНЧЕНЫ\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎉 Бот снова работает!\n\n"
        "Можно пользоваться 👇"
    )
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🚀 Start", callback_data="cmd_start"))
    updated = 0
    for (uid, msg_id) in rows:
        try:
            bot.edit_message_text(new_text, chat_id=uid, message_id=msg_id,
                                  parse_mode='HTML', reply_markup=markup)
            updated += 1
        except Exception:
            try:
                bot.send_message(uid, new_text, parse_mode='HTML', reply_markup=markup)
                updated += 1
            except Exception:
                pass
    clear_maintenance_notified()
    bot.answer_callback_query(call.id, f"🔴 Обновлено: {updated}")
    admin_maintenance(call)


@bot.callback_query_handler(func=lambda call: call.data == "cmd_start")
def cmd_start(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.answer_callback_query(call.id)
    fake = type("M", (), {
        "chat": call.message.chat,
        "from_user": call.from_user,
        "message_id": 0,
        "text": "/start"
    })()
    start(fake)


@bot.callback_query_handler(func=lambda call: call.data == "maint_edit")
def maint_edit(call):
    if call.message.chat.id != ADMIN_ID:
        return
    sent = bot.send_message(call.message.chat.id, "✏️ Введи новый текст:", reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, maint_edit_save)
    bot.answer_callback_query(call.id)


def maint_edit_save(message):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if message.chat.id != ADMIN_ID:
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    set_maintenance(1, message.text)
    sent = bot.send_message(message.chat.id, "✅ Текст обновлён.", reply_markup=admin_menu())
    remember(message.chat.id, sent.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_errors")
def admin_errors(call):
    if call.message.chat.id != ADMIN_ID:
        return
    path = "/app/data/errors.log"
    now = int(time.time())
    cut_30m = now - 1800
    cut_1h = now - 3600
    cut_24h = now - 86400
    cnt_30m = cnt_1h = cnt_24h = total = 0
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    total += 1
                    if line.startswith("[") and "]" in line:
                        date_str = line[1:20]
                        try:
                            ts = int(time.mktime(time.strptime(date_str, "%Y-%m-%d %H:%M:%S")))
                            if ts >= cut_30m:
                                cnt_30m += 1
                            if ts >= cut_1h:
                                cnt_1h += 1
                            if ts >= cut_24h:
                                cnt_24h += 1
                        except Exception:
                            pass
        except Exception:
            pass
    active_count = 0
    active_list = []
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT chat_id, operation, started FROM active_operations ORDER BY started ASC")
        active_rows = c.fetchall()
        active_count = len(active_rows)
        stuck_cutoff = now - 300
        for chat_id, op_type, started in active_rows:
            if started < stuck_cutoff:
                duration = now - started
                active_list.append(f"chat_id {chat_id} ({op_type}, {duration}s)")
        conn.close()
    except Exception:
        pass
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "       ⚠️ Ошибки\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Всего ошибок: <b>{total}</b>\n\n"
        f"📅 30 минут: <b>{cnt_30m}</b>\n"
        f"📅 Час: <b>{cnt_1h}</b>\n"
        f"📅 Сутки: <b>{cnt_24h}</b>\n\n"
        f"🔄 Активных операций: <b>{active_count}</b>\n"
    )
    if active_list:
        text += f"⚠️ Застрявших: <b>{len(active_list)}</b>\n"
        for item in active_list[:5]:
            text += f"   → <code>{item}</code>\n"
        if len(active_list) > 5:
            text += f"   ...и ещё {len(active_list) - 5}\n"
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=errors_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_errors_file")
def admin_errors_file(call):
    if call.message.chat.id != ADMIN_ID:
        return
    path = "/app/data/errors.log"
    if not os.path.exists(path):
        bot.answer_callback_query(call.id, "❌ Файла нет")
        return
    try:
        with open(path, "rb") as f:
            data = f.read()
        bot.send_document(call.message.chat.id, ("errors.log", data))
        bot.answer_callback_query(call.id, "📄 Отправлен")
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Ошибка: {e}")


@bot.callback_query_handler(func=lambda call: call.data == "admin_errors_send")
def admin_errors_send(call):
    if call.message.chat.id != ADMIN_ID:
        return
    path = "/app/data/errors.log"
    if not os.path.exists(path):
        bot.answer_callback_query(call.id, "❌ Ошибок не было — файла нет")
        return
    size = os.path.getsize(path)
    if size == 0:
        bot.answer_callback_query(call.id, "❌ Файл пустой — ошибок нет")
        return
    errors_count = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            errors_count = sum(1 for _ in f)
    except Exception:
        pass
    days_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    day_name = days_ru[time.localtime().tm_wday]
    filename = f"errors_{time.strftime('%Y-%m-%d_%H-%M')}.log"
    caption = (
        f"📤 <b>Отправлено вручную</b>\n\n"
        f"📅 <b>Дата:</b> {time.strftime('%d.%m.%Y')}\n"
        f"📆 <b>День:</b> {day_name}\n"
        f"⏱ <b>Время:</b> {time.strftime('%H:%M:%S')}\n"
        f"🗓 <b>Год:</b> {time.strftime('%Y')}\n\n"
        f"📊 Ошибок: <b>{errors_count}</b>\n"
        f"📦 Размер: <b>{size / 1024:.1f} КБ</b>"
    )
    try:
        with open(path, "rb") as f:
            data = f.read()
        bot.send_document(ERRORS_CHANNEL_ID, (filename, data),
                          caption=caption, parse_mode='HTML')
        bot.answer_callback_query(call.id, "✅ Отправлено")
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ {e}")


@bot.callback_query_handler(func=lambda call: call.data == "admin_errors_clear")
def admin_errors_clear(call):
    if call.message.chat.id != ADMIN_ID:
        return
    try:
        with open("/app/data/errors.log", "w") as f:
            f.write("")
    except Exception:
        pass
    bot.answer_callback_query(call.id, "🗑 Очищено")
    admin_errors(call)


@bot.callback_query_handler(func=lambda call: call.data == "admin_logs")
def admin_logs(call):
    if call.message.chat.id != ADMIN_ID:
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "      📊 ЛОГИ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Что показать?",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=logs_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data in ["logs_50", "logs_100"])
def show_logs(call):
    if call.message.chat.id != ADMIN_ID:
        return
    limit = 50 if call.data == "logs_50" else 100
    rows = get_logs(limit=limit)
    if not rows:
        bot.answer_callback_query(call.id, "Логов нет.")
        return
    text = f"📊 <b>Логи ({len(rows)})</b>\n\n"
    for chat_id, username, atype, adata, ts in rows:
        when = time.strftime('%d.%m %H:%M', time.localtime(ts)) if ts else "—"
        emoji = {"gen": "🎨", "edit": "🖼", "ai": "🤖", "buy": "💳",
                 "ban": "🚫", "delete": "🗑", "file": "📄", "ticket": "🎫"}.get(atype, "•")
        name = username if username else f"ID:{chat_id}"
        short = adata[:60] + "..." if len(adata) > 60 else adata
        text += f"[{when}] {emoji} {name}\n{escape_html(short)}\n\n"
    if len(text) > 4000:
        text = text[:4000] + "\n\n...и другие"
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=logs_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("logs_type_"))
def show_logs_type(call):
    if call.message.chat.id != ADMIN_ID:
        return
    atype = call.data.replace("logs_type_", "")
    rows = get_logs(limit=100, action_type=atype)
    if not rows:
        bot.answer_callback_query(call.id, "Пусто.")
        return
    text = f"📊 <b>Логи — {atype} ({len(rows)})</b>\n\n"
    for chat_id, username, atype2, adata, ts in rows:
        when = time.strftime('%d.%m %H:%M', time.localtime(ts)) if ts else "—"
        name = username if username else f"ID:{chat_id}"
        short = adata[:80] + "..." if len(adata) > 80 else adata
        text += f"[{when}] {name}\n{escape_html(short)}\n\n"
    if len(text) > 4000:
        text = text[:4000] + "\n\n...и другие"
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=logs_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "logs_file")
def logs_file(call):
    if call.message.chat.id != ADMIN_ID:
        return
    rows = get_logs(limit=1000)
    if not rows:
        bot.answer_callback_query(call.id, "Логов нет.")
        return
    txt = f"ЛОГИ ДЕЙСТВИЙ — {time.strftime('%d.%m.%Y %H:%M')}\n"
    txt += "=" * 60 + "\n\n"
    for chat_id, username, atype, adata, ts in rows:
        when = time.strftime('%d.%m.%Y %H:%M:%S', time.localtime(ts)) if ts else "—"
        name = username if username else f"ID:{chat_id}"
        txt += f"[{when}] {atype} | {name}\n{adata}\n\n"
    try:
        data = txt.encode('utf-8')
        bot.send_document(call.message.chat.id, ("logs.txt", data),
                          caption=f"📊 Логи: {len(rows)}")
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Ошибка файла: {e}")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "logs_clear")
def logs_clear_confirm(call):
    if call.message.chat.id != ADMIN_ID:
        return
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Да, очистить", callback_data="logs_clear_yes"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="admin_logs"))
    try:
        bot.edit_message_text("⚠️ <b>ОЧИСТИТЬ ВСЕ ЛОГИ?</b>\n\nНельзя отменить.",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "logs_clear_yes")
def logs_clear_execute(call):
    if call.message.chat.id != ADMIN_ID:
        return
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM action_logs")
    conn.commit()
    conn.close()
    bot.answer_callback_query(call.id, "🗑 Логи очищены")
    admin_logs(call)


@bot.callback_query_handler(func=lambda call: call.data == "admin_wipe")
def admin_wipe(call):
    if call.message.chat.id != ADMIN_ID:
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "      🧹 ОЧИСТКА\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Что очистить?",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=wipe_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "wipe_user_select")
def wipe_user_select(call):
    if call.message.chat.id != ADMIN_ID:
        return
    users = get_all_users()
    if not users:
        bot.answer_callback_query(call.id, "Юзеров нет.")
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in users[:30]:
        name = uname if uname else "—"
        markup.add(telebot.types.InlineKeyboardButton(f"🆔 {uid} — {name}",
                                                       callback_data=f"wipe_user_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_wipe"))
    try:
        bot.edit_message_text("👤 <b>Кого очистить?</b>\n\nВыбери юзера:",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("wipe_user_") and not call.data.startswith("wipe_user_do_") and not call.data.startswith("wipe_user_yes_"))
def wipe_user_menu_handler(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("wipe_user_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    user = get_user(uid)
    uname = user[6] if user[6] else "—"
    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"   🧹 ОЧИСТКА USER {uid}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 {uname}\n\n"
        f"Что удалить?"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=wipe_user_menu(uid))
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("wipe_user_do_"))
def wipe_user_do_confirm(call):
    if call.message.chat.id != ADMIN_ID:
        return
    rest = call.data.replace("wipe_user_do_", "")
    parts = rest.split("_", 1)
    if len(parts) < 2:
        bot.answer_callback_query(call.id, "❌ Ошибка.")
        return
    what, uid_str = parts
    if not uid_str.isdigit():
        bot.answer_callback_query(call.id, "❌ Ошибка ID.")
        return
    uid = int(uid_str)
    names = {
        "chat": "💬 Переписку",
        "image": "🎨 Генерации",
        "edit": "🖼 Редактирования",
        "orders": "🛒 Заказы",
        "tickets": "📋 Тикеты",
        "stats": "📊 Статистику",
        "all": "💥 ВСЁ",
    }
    label = names.get(what, what)
    user = get_user(uid)
    uname = user[6] if user[6] else "—"
    text = (
        f"⚠️ <b>ПОДТВЕРЖДЕНИЕ</b>\n\n"
        f"Удалить: <b>{label}</b>\n\n"
        f"🆔 <code>{uid}</code>\n"
        f"👤 {uname}\n\n"
        f"<b>Нельзя отменить!</b>"
    )
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Да, удалить",
                                                   callback_data=f"wipe_user_yes_{what}_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data=f"wipe_user_{uid}"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("wipe_user_yes_"))
def wipe_user_do_execute(call):
    if call.message.chat.id != ADMIN_ID:
        return
    rest = call.data.replace("wipe_user_yes_", "")
    parts = rest.split("_", 1)
    if len(parts) < 2:
        bot.answer_callback_query(call.id, "❌ Ошибка.")
        return
    what, uid_str = parts
    if not uid_str.isdigit():
        bot.answer_callback_query(call.id, "❌ Ошибка ID.")
        return
    uid = int(uid_str)
    wipe_user_data(uid, what)
    bot.answer_callback_query(call.id, "✅ Очищено")
    user = get_user(uid)
    uname = user[6] if user[6] else "—"
    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"   🧹 Очистка юзера {uid}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 {uname}\n\n"
        f"✅ Готово!\n\n"
        f"Что ещё удалить?"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=wipe_user_menu(uid))
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("wipe_confirm_"))
def wipe_confirm(call):
    if call.message.chat.id != ADMIN_ID:
        return
    what = call.data.replace("wipe_confirm_", "")
    names = {
        "chat_all": "💬 ВСЕ ПЕРЕПИСКИ",
        "image_all": "🎨 ВСЕ ГЕНЕРАЦИИ",
        "edit_all": "🖼 ВСЕ РЕДАКТИРОВАНИЯ",
        "stats_all": "📊 ВСЮ СТАТИСТИКУ",
        "all_all": "💥 ВСЁ (кроме юзеров)",
    }
    label = names.get(what, what)
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "   ⚠️ ПОДТВЕРЖДЕНИЕ\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Удалить: <b>{label}</b>\n\n"
        f"У ВСЕХ пользователей.\n\n"
        f"Нельзя отменить."
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=wipe_confirm_menu(what))
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("wipe_do_"))
def wipe_do(call):
    if call.message.chat.id != ADMIN_ID:
        return
    what = call.data.replace("wipe_do_", "")
    mapping = {
        "chat_all": "chat",
        "image_all": "image",
        "edit_all": "edit",
        "stats_all": "stats",
        "all_all": "all",
    }
    real_what = mapping.get(what, what)
    wipe_all_data(real_what)
    bot.answer_callback_query(call.id, "✅ Очищено")
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "      🧹 ОЧИСТКА\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "✅ Готово!\n\n"
            "Что ещё очистить?",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=wipe_menu())
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "admin_subs")
def admin_subs(call):
    if call.message.chat.id != ADMIN_ID:
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "     ✍️ ПОДПИСКИ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Ты будешь получать уведомления\n"
            "о действиях выбранных юзеров.",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=subs_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "subs_list")
def subs_list(call):
    if call.message.chat.id != ADMIN_ID:
        return
    subs = get_subscriptions(ADMIN_ID)
    if not subs:
        try:
            bot.edit_message_text("📋 Подписок нет.",
                                  chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  reply_markup=subs_menu())
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    text = "📋 <b>Твои подписки:</b>\n\n"
    for uid in subs:
        user = get_user(uid)
        uname = user[6] if user[6] else "—"
        text += f"🆔 <code>{uid}</code> — {uname}\n"
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=subs_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "subs_add")
def subs_add(call):
    if call.message.chat.id != ADMIN_ID:
        return
    users = get_all_users()
    if not users:
        bot.answer_callback_query(call.id, "Юзеров нет.")
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in users[:30]:
        name = uname if uname else "—"
        prefix = "✅ " if is_subscribed(ADMIN_ID, uid) else ""
        markup.add(telebot.types.InlineKeyboardButton(f"{prefix}{uid} — {name}",
                                                       callback_data=f"subs_toggle_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_subs"))
    try:
        bot.edit_message_text("✍️ <b>Выбери юзера для подписки:</b>\n\n✅ — уже подписан",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("subs_toggle_"))
def subs_toggle(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("subs_toggle_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    if is_subscribed(ADMIN_ID, uid):
        unsubscribe(ADMIN_ID, uid)
        bot.answer_callback_query(call.id, "➖ Отписан")
    else:
        subscribe(ADMIN_ID, uid)
        bot.answer_callback_query(call.id, "➕ Подписан")
    subs_add(call)


@bot.callback_query_handler(func=lambda call: call.data == "subs_remove")
def subs_remove(call):
    if call.message.chat.id != ADMIN_ID:
        return
    subs = get_subscriptions(ADMIN_ID)
    if not subs:
        bot.answer_callback_query(call.id, "Подписок нет.")
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for uid in subs:
        user = get_user(uid)
        uname = user[6] if user[6] else "—"
        markup.add(telebot.types.InlineKeyboardButton(f"➖ {uid} — {uname}",
                                                       callback_data=f"subs_rm_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_subs"))
    try:
        bot.edit_message_text("✍️ <b>Выбери кого убрать:</b>",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("subs_rm_"))
def subs_rm(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("subs_rm_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    unsubscribe(ADMIN_ID, uid)
    bot.answer_callback_query(call.id, "➖ Отписан")
    subs_remove(call)


@bot.callback_query_handler(func=lambda call: call.data == "admin_tickets")
def admin_tickets(call):
    if call.message.chat.id != ADMIN_ID:
        return
    try:
        bot.edit_message_text(
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "      📋 ТИКЕТЫ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Выбери раздел:",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=tickets_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_tickets_open")
def admin_tickets_open(call):
    if call.message.chat.id != ADMIN_ID:
        return
    tickets = get_open_tickets()
    if not tickets:
        try:
            bot.edit_message_text("📬 Открытых нет.",
                                  chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  reply_markup=tickets_menu())
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for tid, uid, uname, msg, photo, created in tickets:
        markup.add(telebot.types.InlineKeyboardButton(f"#{tid} — {uname[:20]}", callback_data=f"admin_view_{tid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_tickets"))
    try:
        bot.edit_message_text("📬 <b>Открытые тикеты:</b>",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_tickets_done")
def admin_tickets_done(call):
    if call.message.chat.id != ADMIN_ID:
        return
    tickets = get_done_tickets()
    if not tickets:
        try:
            bot.edit_message_text("✅ Выполненных нет.",
                                  chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  reply_markup=tickets_menu())
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for tid, uid, uname, msg, photo, created in tickets:
        markup.add(telebot.types.InlineKeyboardButton(f"✅ #{tid} — {uname[:20]}", callback_data=f"admin_view_{tid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_tickets"))
    try:
        bot.edit_message_text("✅ <b>Выполненные тикеты:</b>",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_view_"))
def admin_view_ticket(call):
    if call.message.chat.id != ADMIN_ID:
        return
    ticket_id_str = call.data.replace("admin_view_", "")
    if not ticket_id_str.isdigit():
        return
    ticket_id = int(ticket_id_str)
    ticket = get_ticket(ticket_id)
    if not ticket:
        return
    tid, uid, uname, msg, photo, status = ticket
    st_txt = {"open": "📬 Открыт", "done": "✅ Выполнен"}.get(status, status)
    text = f"📋 <b>Тикет #{tid}</b>\n👤 {uname} (ID: <code>{uid}</code>)\n📌 {st_txt}\n\n💬 {escape_html(msg)}"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✍️ Ответить", callback_data=f"admin_reply_{tid}"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_{tid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_tickets"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_reject_"))
def admin_reject_ticket(call):
    if call.message.chat.id != ADMIN_ID:
        return
    ticket_id_str = call.data.replace("admin_reject_", "")
    if not ticket_id_str.isdigit():
        return
    ticket_id = int(ticket_id_str)
    delete_ticket(ticket_id)
    bot.answer_callback_query(call.id, "❌ Удалён")
    try:
        bot.edit_message_text(
            f"❌ Тикет #{ticket_id} удалён.\n\n📋 <b>Тикеты</b>\nВыбери раздел:",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode='HTML', reply_markup=tickets_menu())
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_reply_"))
def admin_reply(call):
    if call.message.chat.id != ADMIN_ID:
        return
    ticket_id_str = call.data.replace("admin_reply_", "")
    if not ticket_id_str.isdigit():
        return
    ticket_id = int(ticket_id_str)
    sent = bot.send_message(call.message.chat.id, f"✍️ Ответ на тикет #{ticket_id}:",
                            reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, admin_send_reply, ticket_id)
    bot.answer_callback_query(call.id)


def admin_send_reply(message, ticket_id):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if message.chat.id != ADMIN_ID:
        return
    ticket = get_ticket(ticket_id)
    if not ticket:
        return
    _, uid, uname, _, _, _ = ticket
    text = message.text if message.text else ""
    answer_ticket(ticket_id, text, status="done")
    try:
        bot.send_message(uid, f"💬 <b>Ответ от поддержки</b> (тикет #{ticket_id}):\n\n{text}", parse_mode='HTML')
    except Exception:
        pass
    sent = bot.send_message(message.chat.id, f"✅ Ответ отправлен.",
                            reply_markup=tickets_menu())
    remember(message.chat.id, sent.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_give")
def admin_give(call):
    if call.message.chat.id != ADMIN_ID:
        return
    users = get_all_users()
    if not users:
        bot.answer_callback_query(call.id, "Юзеров нет.")
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in users[:30]:
        name = uname if uname else "—"
        markup.add(telebot.types.InlineKeyboardButton(f"🆔 {uid} — {name} ({tokens})",
                                                       callback_data=f"admin_give_to_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin"))
    try:
        bot.edit_message_text("💰 <b>Начислить токены</b>\n\nВыбери пользователя:",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_give_to_"))
def admin_give_to(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_give_to_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    sent = bot.send_message(call.message.chat.id, f"💰 Кол-во токенов для <code>{uid}</code>:",
                            parse_mode='HTML', reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, admin_give_amount, uid)
    bot.answer_callback_query(call.id)


def admin_give_amount(message, uid):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if message.chat.id != ADMIN_ID:
        return
    try:
        cnt = int(message.text)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи число.", reply_markup=admin_menu())
        return
    add_tokens(uid, cnt)
    new_balance = get_user(uid)[0]
    try:
        bot.send_message(uid, f"🎁 <b>Вам зачислено {cnt} токенов!</b>\n\n💎 Баланс: <b>{new_balance}</b>",
                         parse_mode='HTML')
    except Exception:
        pass
    sent = bot.send_message(message.chat.id, f"✅ Начислено {cnt} токенов для {uid}.",
                            reply_markup=admin_menu())
    remember(message.chat.id, sent.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_take")
def admin_take(call):
    if call.message.chat.id != ADMIN_ID:
        return
    users = get_all_users()
    if not users:
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in users[:30]:
        name = uname if uname else "—"
        markup.add(telebot.types.InlineKeyboardButton(f"🆔 {uid} — {name} ({tokens})",
                                                       callback_data=f"admin_take_from_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin"))
    try:
        bot.edit_message_text("💸 <b>Забрать токены</b>\n\nВыбери пользователя:",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_take_from_"))
def admin_take_from(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_take_from_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    user = get_user(uid)
    uname = user[6] if user[6] else "—"
    tokens = user[0]
    text = (
        f"⚠️ <b>СПИСАНИЕ ТОКЕНОВ</b>\n\n"
        f"🆔 <code>{uid}</code>\n"
        f"👤 {uname}\n"
        f"💰 Текущий баланс: <b>{tokens}</b>\n\n"
        f"Сколько забрать?"
    )
    sent = bot.send_message(call.message.chat.id, text, parse_mode='HTML',
                            reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, admin_take_amount, uid)
    bot.answer_callback_query(call.id)


def admin_take_amount(message, uid):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if message.chat.id != ADMIN_ID:
        return
    try:
        cnt = int(message.text)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи число.", reply_markup=admin_menu())
        return
    add_tokens(uid, -cnt)
    new_balance = get_user(uid)[0]
    try:
        bot.send_message(uid, f"⚠️ <b>Списано {cnt} токенов.</b>\n\n💎 Баланс: <b>{new_balance}</b>",
                         parse_mode='HTML')
    except Exception:
        pass
    sent = bot.send_message(message.chat.id, f"✅ Забрано {cnt} токенов у {uid}.",
                            reply_markup=admin_menu())
    remember(message.chat.id, sent.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_users" or call.data.startswith("admin_users_page_"))
def admin_users(call):
    if call.message.chat.id != ADMIN_ID:
        return
    if call.data.startswith("admin_users_page_"):
        page_str = call.data.replace("admin_users_page_", "")
        page = int(page_str) if page_str.isdigit() else 1
    else:
        page = 1
    users = get_all_users()
    if not users:
        try:
            bot.edit_message_text("👥 Пользователей нет.",
                                  chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  reply_markup=admin_menu())
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    page_users, page, total_pages, total = paginate(users, page, per_page=10)
    text = f"👥 <b>Список пользователей</b> (стр. {page}/{total_pages})\n"
    text += f"Всего: <b>{total}</b>\n\n"
    for uid, uname, tokens in page_users:
        name = uname if uname else "—"
        text += f"🆔 <code>{uid}</code> — {name} — <b>{tokens}</b> ток.\n"
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML',
                              reply_markup=pagination_menu("admin_users", page, total_pages, back_to="back_to_admin"))
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_manage" or call.data.startswith("admin_manage_page_"))
def admin_manage(call):
    if call.message.chat.id != ADMIN_ID:
        return
    if call.data.startswith("admin_manage_page_"):
        page_str = call.data.replace("admin_manage_page_", "")
        page = int(page_str) if page_str.isdigit() else 1
    else:
        page = 1
    users = get_all_users()
    if not users:
        return
    page_users, page, total_pages, total = paginate(users, page, per_page=10)
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in page_users:
        name = uname if uname else "—"
        markup.add(telebot.types.InlineKeyboardButton(f"🆔 {uid} — {name}", callback_data=f"admin_mng_{uid}"))
    nav = []
    if page > 1:
        nav.append(telebot.types.InlineKeyboardButton("⬅️", callback_data=f"admin_manage_page_{page - 1}"))
    nav.append(telebot.types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(telebot.types.InlineKeyboardButton("➡️", callback_data=f"admin_manage_page_{page + 1}"))
    markup.row(*nav)
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin"))
    try:
        bot.edit_message_text(f"🚫 <b>Управление юзером</b> (стр. {page}/{total_pages})\n\nВыбери:",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_mng_") and call.data.replace("admin_mng_", "").isdigit())
def admin_mng_user(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("admin_mng_", ""))
    user = get_user(uid)
    username = user[6] if user[6] else "—"
    banned = user[10]
    can_image = user[12]
    can_chat = user[13]
    limit_images = user[14]
    limit_chats = user[15]
    used_images = user[16]
    used_chats = user[17]
    limit_edits = user[20]
    used_edits = user[19]
    muted_until = user[21]
    ignore_maint = user[22]
    now = int(time.time())
    muted_txt = "—"
    if muted_until > now:
        left = muted_until - now
        muted_txt = f"🚫 {left // 60}:{left % 60:02d}"
    text = (
        f"🚫 <b>Управление</b>\n"
        f"🆔 <code>{uid}</code> — {username}\n\n"
        f"🚫 Бан: {'ДА' if banned else 'нет'}\n"
        f"🎨 Картинки: {'✅' if can_image else '🚫'}\n"
        f"🤖 Чат: {'✅' if can_chat else '🚫'}\n"
        f"🎯 Лимит картинок: {used_images}/{limit_images if limit_images >= 0 else '∞'}\n"
        f"🖼 Лимит правок: {used_edits}/{limit_edits if limit_edits >= 0 else '∞'}\n"
        f"🤖 Лимит чата: {used_chats}/{limit_chats if limit_chats >= 0 else '∞'}\n"
        f"🚫 Мьют поддержки: {muted_txt}\n"
        f"🔓 Игнор тех.работ: {'ДА' if ignore_maint else 'нет'}\n"
        f"✍️ Подписка: {'ДА' if is_subscribed(ADMIN_ID, uid) else 'нет'}"
    )
    markup = telebot.types.InlineKeyboardMarkup()
    if banned:
        markup.add(telebot.types.InlineKeyboardButton("✅ Разбанить", callback_data=f"admin_mng_unban_{uid}"))
    else:
        markup.add(telebot.types.InlineKeyboardButton("🚫 Забанить", callback_data=f"admin_mng_ban_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("🎨 Картинки ВКЛ/ВЫКЛ", callback_data=f"admin_mng_img_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("🤖 Чат ВКЛ/ВЫКЛ", callback_data=f"admin_mng_chat_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("🎯 Лимиты", callback_data=f"admin_mng_limits_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("🚫 Мьют поддержки", callback_data=f"admin_mng_mute_{uid}"))
    if ignore_maint:
        markup.add(telebot.types.InlineKeyboardButton("🔒 Убрать игнор тех.работ", callback_data=f"admin_mng_ignore_off_{uid}"))
    else:
        markup.add(telebot.types.InlineKeyboardButton("🔓 Игнор тех.работ", callback_data=f"admin_mng_ignore_{uid}"))
    if is_subscribed(ADMIN_ID, uid):
        markup.add(telebot.types.InlineKeyboardButton("➖ Отписаться", callback_data=f"admin_mng_unsub_{uid}"))
    else:
        markup.add(telebot.types.InlineKeyboardButton("✍️ Подписаться", callback_data=f"admin_mng_sub_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("📜 История", callback_data=f"admin_mng_hist_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("✍️ Написать", callback_data=f"admin_mng_write_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_manage"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_mng_ban_"))
def admin_mng_ban(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_mng_ban_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    sent = bot.send_message(call.message.chat.id, f"🚫 Причина бана <code>{uid}</code>:",
                            parse_mode='HTML', reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, admin_mng_ban_save, uid)
    bot.answer_callback_query(call.id)


def admin_mng_ban_save(message, uid):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if message.chat.id != ADMIN_ID:
        return
    reason = message.text or "не указана"
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    update_user(uid, 'banned', 1)
    update_user(uid, 'ban_reason', reason)
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO bans (chat_id, reason, admin_id, timestamp) VALUES (?, ?, ?, ?)",
              (uid, reason, ADMIN_ID, int(time.time())))
    conn.commit()
    conn.close()
    clear_old_messages(uid)
    try:
        bot.send_message(uid, f"🚫 <b>Вы забанены.</b>\n\nПричина: {escape_html(reason)}", parse_mode='HTML')
    except Exception:
        pass
    log_action(uid, "ban", reason)
    sent = bot.send_message(message.chat.id, f"✅ {uid} забанен.", reply_markup=admin_menu())
    remember(message.chat.id, sent.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_mng_unban_"))
def admin_mng_unban(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_mng_unban_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    update_user(uid, 'banned', 0)
    update_user(uid, 'ban_reason', '')
    bot.answer_callback_query(call.id, "✅ Разбанен")
    try:
        bot.send_message(uid, "✅ <b>Вы разбанены.</b>", parse_mode='HTML')
    except Exception:
        pass
    admin_mng_user(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_mng_img_"))
def admin_mng_img(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_mng_img_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    user = get_user(uid)
    update_user(uid, 'can_image', 0 if user[12] else 1)
    bot.answer_callback_query(call.id, "✅ Изменено")
    admin_mng_user(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_mng_chat_"))
def admin_mng_chat(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_mng_chat_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    user = get_user(uid)
    update_user(uid, 'can_chat', 0 if user[13] else 1)
    bot.answer_callback_query(call.id, "✅ Изменено")
    admin_mng_user(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_mng_ignore_off_"))
def admin_mng_ignore_off(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_mng_ignore_off_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    update_user(uid, 'ignore_maintenance', 0)
    bot.answer_callback_query(call.id, "🔒 Убрано")
    admin_mng_user(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_mng_ignore_"))
def admin_mng_ignore(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_mng_ignore_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    update_user(uid, 'ignore_maintenance', 1)
    bot.answer_callback_query(call.id, "🔓 Разрешено")
    admin_mng_user(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_mng_sub_"))
def admin_mng_sub(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_mng_sub_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    subscribe(ADMIN_ID, uid)
    bot.answer_callback_query(call.id, "✍️ Подписан")
    admin_mng_user(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_mng_unsub_"))
def admin_mng_unsub(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_mng_unsub_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    unsubscribe(ADMIN_ID, uid)
    bot.answer_callback_query(call.id, "➖ Отписан")
    admin_mng_user(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_mng_mute_"))
def admin_mng_mute(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_mng_mute_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    sent = bot.send_message(call.message.chat.id,
                            f"🚫 На сколько минут замутить поддержку <code>{uid}</code>?\n\n"
                            f"<i>Введи число (1-10080)</i>",
                            parse_mode='HTML', reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, admin_mng_mute_save, uid)
    bot.answer_callback_query(call.id)


def admin_mng_mute_save(message, uid):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if message.chat.id != ADMIN_ID:
        return
    try:
        minutes = int(message.text)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи число.", reply_markup=admin_menu())
        return
    if minutes < 1 or minutes > 10080:
        bot.send_message(message.chat.id, "❌ 1-10080 минут (7 дней макс).", reply_markup=admin_menu())
        return
    until = int(time.time()) + minutes * 60
    update_user(uid, 'support_muted_until', until)
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    sent = bot.send_message(message.chat.id, f"✅ Мьют поддержки для {uid}: {minutes} мин.",
                            reply_markup=admin_menu())
    remember(message.chat.id, sent.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_mng_limits_"))
def admin_mng_limits(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_mng_limits_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    user = get_user(uid)
    li, lc, le = user[14], user[15], user[20]
    ui, uc, ue = user[16], user[17], user[19]
    text = (
        f"🎯 <b>Лимиты для</b> <code>{uid}</code>\n\n"
        f"🎨 Картинок: {ui}/{li if li >= 0 else '∞'}\n"
        f"🖼 Правок: {ue}/{le if le >= 0 else '∞'}\n"
        f"🤖 Чата: {uc}/{lc if lc >= 0 else '∞'}"
    )
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🎨 Лимит картинок", callback_data=f"admin_lim_img_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("🖼 Лимит правок", callback_data=f"admin_lim_edit_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("🤖 Лимит чата", callback_data=f"admin_lim_chat_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("🔄 Сбросить счётчики", callback_data=f"admin_lim_reset_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("♾ Снять всё", callback_data=f"admin_lim_clear_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_mng_{uid}"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_lim_img_"))
def admin_lim_img(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_lim_img_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    sent = bot.send_message(call.message.chat.id, f"🎨 Лимит картинок для <code>{uid}</code> (-1=∞):",
                            parse_mode='HTML', reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, admin_lim_img_save, uid)
    bot.answer_callback_query(call.id)


def admin_lim_img_save(message, uid):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if message.chat.id != ADMIN_ID:
        return
    try:
        val = int(message.text)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи число.", reply_markup=admin_menu())
        return
    update_user(uid, 'limit_images', val)
    reset_used(uid, 'used_images')
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    sent = bot.send_message(message.chat.id, f"✅ {val if val >= 0 else '∞'}", reply_markup=admin_menu())
    remember(message.chat.id, sent.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_lim_edit_"))
def admin_lim_edit(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_lim_edit_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    sent = bot.send_message(call.message.chat.id, f"🖼 Лимит правок для <code>{uid}</code> (-1=∞):",
                            parse_mode='HTML', reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, admin_lim_edit_save, uid)
    bot.answer_callback_query(call.id)


def admin_lim_edit_save(message, uid):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if message.chat.id != ADMIN_ID:
        return
    try:
        val = int(message.text)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи число.", reply_markup=admin_menu())
        return
    update_user(uid, 'limit_edits', val)
    reset_used(uid, 'used_edits')
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    sent = bot.send_message(message.chat.id, f"✅ {val if val >= 0 else '∞'}", reply_markup=admin_menu())
    remember(message.chat.id, sent.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_lim_chat_"))
def admin_lim_chat(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_lim_chat_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    sent = bot.send_message(call.message.chat.id, f"🤖 Лимит чата для <code>{uid}</code> (-1=∞):",
                            parse_mode='HTML', reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, admin_lim_chat_save, uid)
    bot.answer_callback_query(call.id)


def admin_lim_chat_save(message, uid):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if message.chat.id != ADMIN_ID:
        return
    try:
        val = int(message.text)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи число.", reply_markup=admin_menu())
        return
    update_user(uid, 'limit_chats', val)
    reset_used(uid, 'used_chats')
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    sent = bot.send_message(message.chat.id, f"✅ {val if val >= 0 else '∞'}", reply_markup=admin_menu())
    remember(message.chat.id, sent.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_lim_reset_"))
def admin_lim_reset(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_lim_reset_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    reset_used(uid)
    bot.answer_callback_query(call.id, "🔄 Сброшено")
    admin_mng_limits(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_lim_clear_"))
def admin_lim_clear(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_lim_clear_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    update_user(uid, 'limit_images', -1)
    update_user(uid, 'limit_chats', -1)
    update_user(uid, 'limit_edits', -1)
    reset_used(uid)
    bot.answer_callback_query(call.id, "♾ Снято")
    admin_mng_limits(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_mng_hist_"))
def admin_mng_hist(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_mng_hist_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    history = get_full_history(uid, limit=500)
    img_hist = get_full_image_history(uid, limit=100)
    txt = f"📜 ИСТОРИЯ ЗАПРОСОВ\nПользователь: {uid}\n"
    txt += f"Дата: {time.strftime('%d.%m.%Y %H:%M')}\n"
    txt += "=" * 60 + "\n\n"
    txt += f"🤖 ЧАТ С ИИ ({len(history)} сообщений):\n"
    txt += "-" * 40 + "\n"
    if not history:
        txt += "(пусто)\n"
    for role, content in history:
        prefix = "👤 ПОЛЬЗОВАТЕЛЬ:" if role == "user" else "🤖 БОБ:"
        txt += f"\n{prefix}\n{content}\n"
    txt += "\n\n"
    txt += f"🎨 ФОТО ({len(img_hist)} записей):\n"
    txt += "-" * 40 + "\n"
    if not img_hist:
        txt += "(пусто)\n"
    for item in img_hist:
        if len(item) == 4:
            prompt, url, ts, kind = item
        else:
            prompt, url, ts = item
            kind = "gen"
        when = time.strftime('%d.%m.%Y %H:%M', time.localtime(ts)) if ts else "—"
        tag = "🖼 EDIT" if kind == "edit" else "🎨 GEN"
        txt += f"\n[{when}] {tag}\nПромт: {prompt}\nURL: {url}\n"
    try:
        data = txt.encode('utf-8')
        size_kb = len(data) / 1024
        user = get_user(uid)
        username = user[6] if user[6] else "—"
        caption = (
            f"📜 <b>История запроса</b>\n\n"
            f"🆔 <code>{uid}</code>\n"
            f"👤 {username}\n\n"
            f"🤖 Чат: <b>{len(history)}</b> сообщений\n"
            f"🎨 Фото: <b>{len(img_hist)}</b>\n"
            f"📦 Размер: <b>{size_kb:.1f} КБ</b>\n"
            f"📅 {time.strftime('%d.%m.%Y %H:%M')}"
        )
        bot.send_document(call.message.chat.id,
                          (f"history_{uid}_{time.strftime('%Y-%m-%d')}.txt", data),
                          caption=caption, parse_mode='HTML')
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Ошибка файла: {e}")
    if img_hist:
        s = bot.send_message(call.message.chat.id,
                             f"🎨 Отправляю фото: <b>{len(img_hist)}</b> (последние 20)",
                             parse_mode='HTML')
        remember(call.message.chat.id, s.message_id)
        last_photos = img_hist[-20:]
        for item in last_photos:
            if len(item) == 4:
                prompt, image_url, ts, kind = item
            else:
                prompt, image_url, ts = item
                kind = "gen"
            when = time.strftime('%d.%m.%Y %H:%M', time.localtime(ts)) if ts else "—"
            tag = "🖼" if kind == "edit" else "🎨"
            msg_id = send_photo_safe(call.message.chat.id, image_url,
                                     f"{tag} <b>{escape_html(prompt[:200])}</b>\n📅 {when}")
            if msg_id:
                remember(call.message.chat.id, msg_id)
            time.sleep(0.5)
        if len(img_hist) > 20:
            s = bot.send_message(call.message.chat.id,
                                 f"⚠️ Показаны последние 20 из {len(img_hist)} фото.\n"
                                 f"Все URL — в файле выше 👆",
                                 parse_mode='HTML')
            remember(call.message.chat.id, s.message_id)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_mng_{uid}"))
    s = bot.send_message(call.message.chat.id, "✅ Готово.", reply_markup=markup)
    remember(call.message.chat.id, s.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_mng_write_"))
def admin_mng_write(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid_str = call.data.replace("admin_mng_write_", "")
    if not uid_str.isdigit():
        return
    uid = int(uid_str)
    sent = bot.send_message(call.message.chat.id, f"✍️ Сообщение для <code>{uid}</code>:",
                            parse_mode='HTML', reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, admin_mng_write_save, uid)
    bot.answer_callback_query(call.id)


def admin_mng_write_save(message, uid):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if message.chat.id != ADMIN_ID:
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    text = message.text or ""
    try:
        bot.send_message(uid, f"📩 <b>Сообщение от администрации:</b>\n\n{escape_html(text)}",
                         parse_mode='HTML')
        sent = bot.send_message(message.chat.id, "✅ Отправлено.", reply_markup=admin_menu())
    except Exception as e:
        sent = bot.send_message(message.chat.id, f"❌ {e}\n\nСсылка: tg://user?id={uid}",
                                reply_markup=admin_menu())
    remember(message.chat.id, sent.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_limits")
def admin_limits_root(call):
    if call.message.chat.id != ADMIN_ID:
        return
    users = get_all_users()
    if not users:
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in users[:30]:
        name = uname if uname else "—"
        markup.add(telebot.types.InlineKeyboardButton(f"🎯 {uid} — {name}",
                                                       callback_data=f"admin_mng_limits_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin"))
    try:
        bot.edit_message_text("🎯 <b>Лимиты</b>\n\nВыбери юзера:",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_orders")
def admin_orders(call):
    if call.message.chat.id != ADMIN_ID:
        return
    orders = get_all_orders()
    if not orders:
        try:
            bot.edit_message_text("🛒 Покупок нет.",
                                  chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  reply_markup=admin_menu())
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    text = "🛒 <b>Покупки:</b>\n\n"
    for oid, uid, tokens, amount, created, paid, tx_id, expires in orders:
        when = time.strftime('%d.%m %H:%M', time.localtime(created)) if created else "—"
        status = "✅" if paid else "⏳"
        text += f"🧾 <code>{oid}</code>\n👤 {uid}\n💰 {amount} ₽ → {tokens} ток.\n📅 {when} {status}\n"
        if tx_id:
            text += f"🆔 <code>{tx_id}</code>\n"
        text += "\n"
    if len(text) > 4000:
        text = text[:4000] + "..."
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=admin_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


def send_broadcast(text, signed=False):
    global last_broadcast
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM users WHERE banned=0")
    rows = c.fetchall()
    conn.close()
    sent_count = 0
    failed_count = 0
    sent_messages = []
    for (uid,) in rows:
        try:
            if signed:
                msg = bot.send_message(uid, f"📢 <b>Сообщение от администрации:</b>\n\n{text}",
                                       parse_mode='HTML')
            else:
                msg = bot.send_message(uid, text, parse_mode='HTML')
            sent_messages.append((uid, msg.message_id))
            sent_count += 1
        except Exception:
            failed_count += 1
        time.sleep(0.05)
    last_broadcast = {"messages": sent_messages, "active": True}
    try:
        bot.send_message(ADMIN_ID,
                         f"📢 <b>Рассылка завершена</b>\n\n"
                         f"✅ Отправлено: <b>{sent_count}</b>\n"
                         f"❌ Ошибок: <b>{failed_count}</b>")
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast")
def admin_broadcast(call):
    if call.message.chat.id != ADMIN_ID:
        return
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📢 С подписью", callback_data="admin_bc_signed"))
    markup.add(telebot.types.InlineKeyboardButton("📨 Без подписи", callback_data="admin_bc_unsigned"))
    if last_broadcast["active"]:
        markup.add(telebot.types.InlineKeyboardButton("🗑 Удалить", callback_data="admin_bc_delete"))
    markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin"))
    try:
        bot.edit_message_text("📢 <b>Рассылка</b>\n\nВыбери тип:",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              parse_mode='HTML', reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data in ["admin_bc_signed", "admin_bc_unsigned"])
def admin_broadcast_type(call):
    if call.message.chat.id != ADMIN_ID:
        return
    signed = call.data == "admin_bc_signed"
    sent = bot.send_message(call.message.chat.id, "📢 Введи текст:", reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, admin_broadcast_confirm, signed)
    bot.answer_callback_query(call.id)


def admin_broadcast_confirm(message, signed):
    if message.text == "⬅️ Назад":
        back_to_main_handler(message.chat.id, message=message)
        return
    if message.chat.id != ADMIN_ID:
        return
    if not message.text:
        bot.send_message(message.chat.id, "❌ Пусто.", reply_markup=admin_menu())
        return
    update_user(ADMIN_ID, 'state', f'bc_confirm:{signed}:{message.text[:400]}')
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Отправить", callback_data="admin_bc_yes"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="admin_bc_no"))
    sent = bot.send_message(message.chat.id,
                            f"📢 <b>Подтверждение</b>\n\n<i>{escape_html(message.text[:1000])}</i>\n\nОтправить?",
                            parse_mode='HTML', reply_markup=markup)
    remember(message.chat.id, sent.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_bc_no")
def admin_bc_no(call):
    if call.message.chat.id != ADMIN_ID:
        return
    update_user(ADMIN_ID, 'state', 'idle')
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    sent = bot.send_message(call.message.chat.id, "❌ Отменено.", reply_markup=admin_menu())
    remember(call.message.chat.id, sent.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_bc_yes")
def admin_bc_yes(call):
    if call.message.chat.id != ADMIN_ID:
        return
    user = get_user(ADMIN_ID)
    state = user[1]
    if not state.startswith('bc_confirm:'):
        bot.answer_callback_query(call.id, "❌ Потеряно")
        return
    rest = state.replace('bc_confirm:', '', 1)
    signed_str, _, text = rest.partition(':')
    signed = signed_str == 'True'
    update_user(ADMIN_ID, 'state', 'idle')
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.answer_callback_query(call.id)
    if has_active_operations():
        global pending_broadcast
        pending_broadcast = {
            "active": True,
            "text": text,
            "signed": signed,
            "waiting_since": int(time.time())
        }
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("❌ Отменить", callback_data="broadcast_cancel"))
        bot.send_message(
            call.message.chat.id,
            "⏳ <b>Есть активные операции</b>\n\n"
            "Рассылка запустится, когда все закончат.\n"
            "Если ждать больше 10 минут — запустится автоматически.",
            parse_mode='HTML',
            reply_markup=markup
        )
    else:
        threading.Thread(target=send_broadcast, args=(text, signed), daemon=True).start()
        bot.send_message(call.message.chat.id, "📢 Рассылка запущена в фоне")


@bot.callback_query_handler(func=lambda call: call.data == "broadcast_cancel")
def broadcast_cancel(call):
    if call.message.chat.id != ADMIN_ID:
        return
    global pending_broadcast
    pending_broadcast = {"active": False, "text": "", "signed": False, "waiting_since": 0}
    bot.answer_callback_query(call.id, "❌ Отменено")
    try:
        bot.edit_message_text("❌ Рассылка отменена.",
                              chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              reply_markup=admin_menu())
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "admin_bc_delete")
def admin_bc_delete(call):
    if call.message.chat.id != ADMIN_ID:
        return
    global last_broadcast
    if not last_broadcast["active"]:
        bot.answer_callback_query(call.id, "Нет активной рассылки.")
        return
    deleted = 0
    for uid, msg_id in last_broadcast["messages"]:
        try:
            bot.delete_message(uid, msg_id)
            deleted += 1
        except Exception:
            pass
    last_broadcast = {"messages": [], "active": False}
    bot.answer_callback_query(call.id, f"🗑 {deleted}")
    try:
        bot.edit_message_text(f"🗑 Удалено: {deleted}", chat_id=call.message.chat.id,
                              message_id=call.message.message_id, reply_markup=admin_menu())
    except Exception:
        pass


@bot.message_handler(commands=['find_user'])
def find_user_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        parts = message.text.split()
        uid = int(parts[1])
    except (IndexError, ValueError):
        bot.send_message(message.chat.id, "Использование: /find_user 123456789")
        return
    row = find_user_by_id(uid)
    if not row:
        bot.send_message(message.chat.id, f"❌ Юзер {uid} не найден.")
        return
    chat_id, uname, tokens = row
    uname = uname if uname else "—"
    bot.send_message(message.chat.id,
                     f"🆔 <code>{chat_id}</code>\n👤 {uname}\n💰 {tokens} токенов",
                     parse_mode='HTML')


def backup_db():
    try:
        if not os.path.exists(DB_PATH):
            return False
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = time.strftime('%Y-%m-%d_%H-%M')
        backup_path = os.path.join(BACKUP_DIR, f"users_{ts}.db")
        shutil.copy2(DB_PATH, backup_path)
        print(f"Backup created: {backup_path}")
        try:
            with open(backup_path, "rb") as f:
                data = f.read()
            days_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
            day_name = days_ru[time.localtime().tm_wday]
            caption = (
                f"💾 <b>Бэкап БД</b>\n\n"
                f"📅 <b>Дата:</b> {time.strftime('%d.%m.%Y')}\n"
                f"📆 <b>День:</b> {day_name}\n"
                f"⏱ <b>Время:</b> {time.strftime('%H:%M:%S')}\n"
                f"🗓 <b>Год:</b> {time.strftime('%Y')}\n\n"
                f"📦 Размер: <b>{len(data) / 1024:.1f} КБ</b>"
            )
            if ERRORS_CHANNEL_ID != 0:
                bot.send_document(ERRORS_CHANNEL_ID, (f"users_{ts}.db", data),
                                  caption=caption, parse_mode='HTML')
        except Exception as e:
            print(f"Send backup error: {e}")
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("users_")])
        if len(backups) > BACKUP_KEEP:
            for old in backups[:-BACKUP_KEEP]:
                try:
                    os.remove(os.path.join(BACKUP_DIR, old))
                except Exception:
                    pass
        return True
    except Exception as e:
        log_error(f"backup_db: {e}")
        return False


def backup_worker():
    first_run = True
    while True:
        try:
            if first_run:
                time.sleep(60)
                first_run = False
            else:
                time.sleep(BACKUP_INTERVAL)
            backup_db()
        except Exception as e:
            log_error(f"backup_worker: {e}")


def cleanup_orders_worker():
    while True:
        try:
            conn = get_conn()
            c = conn.cursor()
            now = int(time.time())
            c.execute("DELETE FROM orders WHERE paid=0 AND expires > 0 AND expires < ?", (now,))
            conn.commit()
            conn.close()
        except Exception as e:
            log_error(f"cleanup_orders: {e}")
        time.sleep(60)


def cleanup_edit_cache():
    while True:
        try:
            now = int(time.time())
            to_remove = []
            for chat_id, data in list(edit_photos_cache.items()):
                created = data.get("created", 0)
                if now - created > 1800:
                    to_remove.append(chat_id)
            for chat_id in to_remove:
                edit_photos_cache.pop(chat_id, None)
                print(f"Cleaned edit cache for {chat_id}")
        except Exception as e:
            log_error(f"cleanup_edit_cache: {e}")
        time.sleep(600)


def cleanup_active_operations():
    while True:
        try:
            conn = get_conn()
            c = conn.cursor()
            cutoff = int(time.time()) - 300
            c.execute("SELECT chat_id, operation, started FROM active_operations WHERE started < ?", (cutoff,))
            stuck = c.fetchall()
            for chat_id, op_type, started in stuck:
                duration = int(time.time()) - started
                log_error(f"STUCK OPERATION: chat_id={chat_id}, type={op_type}, duration={duration}s")
            c.execute("DELETE FROM active_operations WHERE started < ?", (cutoff,))
            deleted = c.rowcount
            conn.commit()
            conn.close()
            if deleted > 0:
                print(f"Cleaned {deleted} stuck operations")
        except Exception as e:
            log_error(f"cleanup_active: {e}")
        time.sleep(300)


def cleanup_errors_log():
    while True:
        try:
            time.sleep(86400)
            path = "/app/data/errors.log"
            if not os.path.exists(path):
                continue
            cutoff = time.time() - 30 * 86400
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_lines = []
            for line in lines:
                if line.startswith("[") and "]" in line:
                    date_str = line[1:20]
                    try:
                        ts = time.mktime(time.strptime(date_str, "%Y-%m-%d %H:%M:%S"))
                        if ts >= cutoff:
                            new_lines.append(line)
                    except Exception:
                        new_lines.append(line)
                else:
                    new_lines.append(line)
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            removed = len(lines) - len(new_lines)
            if removed > 0:
                print(f"Cleaned {removed} old errors from errors.log")
        except Exception as e:
            log_error(f"cleanup_errors_log: {e}")


def send_errors_to_channel():
    if ERRORS_CHANNEL_ID == 0:
        return False
    path = "/app/data/errors.log"
    if not os.path.exists(path):
        return False
    size = os.path.getsize(path)
    if size == 0:
        try:
            os.remove(path)
        except Exception:
            pass
        return False
    errors_count = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            errors_count = sum(1 for _ in f)
    except Exception:
        pass
    days_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    now = time.localtime()
    day_name = days_ru[now.tm_wday]
    filename = f"errors_{time.strftime('%Y-%m-%d_%H-%M')}.log"
    caption = (
        f"📅 <b>Дата:</b> {time.strftime('%d.%m.%Y')}\n"
        f"📆 <b>День:</b> {day_name}\n"
        f"⏱ <b>Время:</b> {time.strftime('%H:%M:%S')}\n"
        f"🗓 <b>Год:</b> {time.strftime('%Y')}\n\n"
        f"📊 Ошибок: <b>{errors_count}</b>\n"
        f"📦 Размер: <b>{size / 1024:.1f} КБ</b>"
    )
    try:
        with open(path, "rb") as f:
            data = f.read()
        bot.send_document(ERRORS_CHANNEL_ID, (filename, data),
                          caption=caption, parse_mode='HTML')
        os.remove(path)
        print(f"Errors sent to channel ({errors_count} errors, {size} bytes)")
        return True
    except Exception as e:
        print(f"Send errors to channel error: {e}")
        return False


def errors_uploader():
    first_run = True
    while True:
        try:
            if first_run:
                time.sleep(300)
                first_run = False
            else:
                time.sleep(86400)
            send_errors_to_channel()
        except Exception as e:
            log_error(f"errors_uploader: {e}")


def maintenance_worker():
    while True:
        try:
            row = get_maintenance()
            enabled = row[2] if len(row) > 2 else 0
            notified = row[3] if len(row) > 3 else 1
            msg = row[1] if len(row) > 1 else "Технические работы"
            if enabled and not notified and not has_active_operations():
                conn = get_conn()
                c = conn.cursor()
                c.execute("SELECT chat_id FROM users WHERE banned=0")
                users = c.fetchall()
                conn.close()
                sent_count = 0
                for (uid,) in users:
                    if uid == ADMIN_ID:
                        continue
                    if can_use_during_maintenance(uid):
                        continue
                    try:
                        text = (
                            "🛠 <b>" + escape_html(msg) + "</b>\n"
                            "━━━━━━━━━━━━━━━━━━━━\n\n"
                            "⚠️ Бот временно недоступен\n\n"
                            "🆘 Поддержка работает"
                        )
                        sent = bot.send_message(uid, text, parse_mode='HTML',
                                                reply_markup=maintenance_menu())
                        save_maintenance_notified(uid, sent.message_id)
                        sent_count += 1
                    except Exception:
                        pass
                set_maintenance(0, notified=1)
                try:
                    bot.send_message(ADMIN_ID, f"📢 Тех.работы разосланы: {sent_count} чел.")
                except Exception:
                    pass
        except Exception as e:
            log_error(f"maintenance_worker: {e}")
        time.sleep(30)


def broadcast_worker():
    while True:
        try:
            global pending_broadcast
            if pending_broadcast["active"]:
                waiting = int(time.time()) - pending_broadcast["waiting_since"]
                can_run = not has_active_operations() or waiting > 600
                if can_run:
                    text = pending_broadcast["text"]
                    signed = pending_broadcast["signed"]
                    pending_broadcast = {"active": False, "text": "", "signed": False, "waiting_since": 0}
                    try:
                        bot.send_message(ADMIN_ID,
                                         "✅ <b>Все операции завершены</b>\n\n🚀 Запускаю рассылку...",
                                         parse_mode='HTML')
                    except Exception:
                        pass
                    threading.Thread(target=send_broadcast, args=(text, signed), daemon=True).start()
        except Exception as e:
            log_error(f"broadcast_worker: {e}")
        time.sleep(30)


def send_startup_report():
    if ERRORS_CHANNEL_ID == 0:
        return
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*), COALESCE(SUM(tokens), 0) FROM users")
        users_count, total_tokens = c.fetchone()
        c.execute("SELECT COUNT(*) FROM image_history WHERE kind='gen'")
        images_gen = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM image_history WHERE kind='edit'")
        images_edit = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM history WHERE role='user'")
        ai_messages = c.fetchone()[0]
        c.execute("SELECT COUNT(*), COALESCE(SUM(CASE WHEN paid=1 THEN 1 ELSE 0 END), 0) FROM orders")
        orders_total, orders_paid = c.fetchone()
        c.execute("SELECT COUNT(*) FROM tickets WHERE status='open'")
        tickets_open = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM users WHERE banned=1")
        banned_count = c.fetchone()[0]
        errors_count = 0
        try:
            with open("/app/data/errors.log", "r", encoding="utf-8") as f:
                errors_count = sum(1 for _ in f)
        except Exception:
            pass
        conn.close()
        text = (
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "    🚀 Бот запущен\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📅 Дата: {time.strftime('%d.%m.%Y %H:%M:%S')}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📊 Состояние базы данных:\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Юзеров всего: <b>{users_count}</b>\n"
            f"💰 Токенов у всех: <b>{total_tokens}</b>\n"
            f"🎨 Сгенерировано картинок: <b>{images_gen}</b>\n"
            f"🖼 Редактирований: <b>{images_edit}</b>\n"
            f"🤖 Сообщений в ИИ: <b>{ai_messages}</b>\n"
            f"🛒 Заказов всего: <b>{orders_total}</b>\n"
            f"💳 Оплачено заказов: <b>{orders_paid}</b>\n"
            f"📋 Открытых тикетов: <b>{tickets_open}</b>\n"
            f"🚫 Забанено юзеров: <b>{banned_count}</b>\n"
            f"⚠️ Ошибок в errors.log: <b>{errors_count}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Бот работает"
        )
        bot.send_message(ERRORS_CHANNEL_ID, text, parse_mode='HTML')
    except Exception as e:
        print(f"Startup report error: {e}")


# === WEBAPP API ===
import hmac as _hmac
import hashlib as _hashlib
import json as _json


def verify_init_data(init_data):
    try:
        parsed = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
        data_dict = dict(parsed)
        received_hash = data_dict.pop("hash", "")
        if not received_hash:
            return None
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(data_dict.items())
        )
        secret_key = _hmac.new(b"WebAppData", BOT_TOKEN.encode(), _hashlib.sha256).digest()
        calculated_hash = _hmac.new(secret_key, data_check_string.encode(), _hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(calculated_hash, received_hash):
            return None
        user_str = data_dict.get("user", "")
        if not user_str:
            return None
        return _json.loads(user_str)
    except Exception as e:
        log_error(f"verify_init_data: {e}")
        return None


def api_auth(data):
    init_data = data.get("init_data", "")
    user = verify_init_data(init_data)
    if not user:
        return None
    return user.get("id")


@app.route('/webapp/api/me', methods=['POST'])
def webapp_api_me():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        user = get_user(chat_id)
        if user[10]:
            return jsonify({"error": "banned", "reason": user[11] or "не указана"}), 403
        return jsonify({
            "chat_id": chat_id,
            "tokens": user[0],
            "mode": user[2],
            "ai_mode": user[3],
            "coder_mode": user[9],
            "is_admin": chat_id == ADMIN_ID,
            "can_image": bool(user[12]),
            "can_chat": bool(user[13]),
            "limit_images": user[14],
            "used_images": user[16],
            "limit_edits": user[20],
            "used_edits": user[19],
            "limit_chats": user[15],
            "used_chats": user[17],
            "send_files": bool(user[18]),
            "sound_on": bool(user[23]),
            "username": user[6] or "",
        })
    except Exception as e:
        log_error(f"webapp_api_me: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/generate', methods=['POST'])
def webapp_api_generate():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "Пустой промт"}), 400
        user = get_user(chat_id)
        if user[10]:
            return jsonify({"error": "Вы забанены"}), 403
        if not user[12]:
            return jsonify({"error": "🚫 Картинки запрещены"}), 403
        if user[14] >= 0 and user[16] >= user[14]:
            return jsonify({"error": "🚫 Лимит картинок исчерпан"}), 403
        if user[0] < IMAGE_COST:
            return jsonify({"error": f"❌ Нужно {IMAGE_COST} токена"}), 403
        mark_operation_start(chat_id, "gen")
        try:
            image_url = generate_image_bothub(prompt)
            if not image_url:
                return jsonify({"error": "❌ Не удалось сгенерировать"}), 500
            add_tokens(chat_id, -IMAGE_COST)
            log_stat(chat_id, IMAGE_COST)
            add_image_history(chat_id, prompt, image_url, kind="gen")
            inc_used(chat_id, 'used_images')
            log_action(chat_id, "gen", prompt)
            notify_subscribers(chat_id, f"🎨 Генерация: {prompt[:100]}")
            clear_old_logs()
            new_balance = get_user(chat_id)[0]
            return jsonify({"url": image_url, "prompt": prompt, "balance": new_balance})
        finally:
            mark_operation_end(chat_id)
    except Exception as e:
        log_error(f"webapp_api_generate: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/chat', methods=['POST'])
def webapp_api_chat():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        question = (data.get("question") or "").strip()
        if not question:
            return jsonify({"error": "Пустое сообщение"}), 400
        user = get_user(chat_id)
        if user[10]:
            return jsonify({"error": "Вы забанены"}), 403
        if not user[13]:
            return jsonify({"error": "🚫 Чат запрещён"}), 403
        if user[15] >= 0 and user[17] >= user[15]:
            return jsonify({"error": "🚫 Лимит чата исчерпан"}), 403
        if user[0] < 1:
            return jsonify({"error": "❌ Токены закончились"}), 403
        mode = user[2]
        ai_mode = user[3]
        mark_operation_start(chat_id, "ai")
        try:
            answer = ask_gigachat(chat_id, question, mode, ai_mode)
            add_tokens(chat_id, -1)
            log_stat(chat_id, 1)
            inc_used(chat_id, 'used_chats')
            log_action(chat_id, "ai", question[:200])
            clear_old_logs()
            new_balance = get_user(chat_id)[0]
            return jsonify({"answer": answer, "balance": new_balance})
        finally:
            mark_operation_end(chat_id)
    except Exception as e:
        log_error(f"webapp_api_chat: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/history/image', methods=['POST'])
def webapp_api_history_image():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        kind = data.get("kind", "gen")
        rows = get_image_history_by_kind(chat_id, kind, limit=100)
        items = []
        for prompt, url, ts in rows:
            items.append({"prompt": prompt, "url": url, "ts": ts})
        return jsonify({"items": items})
    except Exception as e:
        log_error(f"webapp_api_history_image: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/history/chat', methods=['POST'])
def webapp_api_history_chat():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        rows = get_history(chat_id, limit=100)
        items = []
        for role, content in rows:
            items.append({"role": role, "content": content})
        return jsonify({"items": items})
    except Exception as e:
        log_error(f"webapp_api_history_chat: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/buy', methods=['POST'])
def webapp_api_buy():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        amount = int(data.get("amount", 0))
        tokens = int(data.get("tokens", 0))
        if amount <= 0 or tokens <= 0:
            return jsonify({"error": "Неверные параметры"}), 400
        order_id = f"ORD-{chat_id}-{tokens}-{amount}"
        save_order(order_id, chat_id, tokens, amount)
        pay_url = f"{PAY_BASE_URL}/pay/{amount}/{order_id}"
        return jsonify({"pay_url": pay_url, "order_id": order_id})
    except Exception as e:
        log_error(f"webapp_api_buy: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/settings', methods=['POST'])
def webapp_api_settings():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        field = data.get("field", "")
        if field == "mode":
            value = data.get("value", "regular")
            update_user(chat_id, 'mode', value)
        elif field == "ai_mode":
            value = data.get("value", "regular")
            update_user(chat_id, 'ai_mode', value)
        elif field == "coder_mode":
            value = data.get("value", "with_hints")
            update_user(chat_id, 'coder_mode', value)
        elif field == "send_files":
            user = get_user(chat_id)
            new_val = 0 if user[18] else 1
            update_user(chat_id, 'send_files', new_val)
            return jsonify({"ok": True, "value": new_val})
        elif field == "sound_on":
            user = get_user(chat_id)
            new_val = 0 if user[23] else 1
            update_user(chat_id, 'sound_on', new_val)
            return jsonify({"ok": True, "value": new_val})
        else:
            return jsonify({"error": "bad field"}), 400
        return jsonify({"ok": True})
    except Exception as e:
        log_error(f"webapp_api_settings: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/tickets/new', methods=['POST'])
def webapp_api_tickets_new():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        message = (data.get("message") or "").strip()
        if not message:
            return jsonify({"error": "Пустое сообщение"}), 400
        muted, time_left = check_support_muted(chat_id)
        if muted:
            return jsonify({"error": f"🚫 Подожди {time_left}"}), 403
        user = get_user(chat_id)
        username = user[6] if user[6] else f"ID:{chat_id}"
        ticket_id = create_ticket(chat_id, username, message)
        try:
            bot.send_message(ADMIN_ID, f"🔔 Новый тикет #{ticket_id} от {username}\n\n{message}")
        except Exception:
            pass
        notify_subscribers(chat_id, f"🎫 Новый тикет #{ticket_id}")
        return jsonify({"ok": True, "ticket_id": ticket_id})
    except Exception as e:
        log_error(f"webapp_api_tickets_new: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/tickets/my', methods=['POST'])
def webapp_api_tickets_my():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        rows = get_user_tickets(chat_id)
        items = []
        for tid, msg, ans, status in rows:
            items.append({
                "id": tid, "message": msg,
                "answer": ans or "", "status": status,
            })
        return jsonify({"items": items})
    except Exception as e:
        log_error(f"webapp_api_tickets_my: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/admin/stats', methods=['POST'])
def webapp_api_admin_stats():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if chat_id != ADMIN_ID:
            return jsonify({"error": "unauthorized"}), 401
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*), COALESCE(SUM(tokens), 0) FROM users")
        users_count, total_tokens = c.fetchone()
        c.execute("SELECT COUNT(*) FROM image_history WHERE kind='gen'")
        images_gen = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM image_history WHERE kind='edit'")
        images_edit = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM history WHERE role='user'")
        ai_messages = c.fetchone()[0]
        c.execute("SELECT COUNT(*), COALESCE(SUM(CASE WHEN paid=1 THEN 1 ELSE 0 END), 0) FROM orders")
        orders_total, orders_paid = c.fetchone()
        c.execute("SELECT COUNT(*) FROM tickets WHERE status='open'")
        tickets_open = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM users WHERE banned=1")
        banned_count = c.fetchone()[0]
        conn.close()
        return jsonify({
            "users": users_count, "tokens": total_tokens,
            "images_gen": images_gen, "images_edit": images_edit,
            "ai_messages": ai_messages, "orders": orders_total,
            "orders_paid": orders_paid, "tickets_open": tickets_open,
            "banned": banned_count,
        })
    except Exception as e:
        log_error(f"webapp_api_admin_stats: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/admin/users', methods=['POST'])
def webapp_api_admin_users():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if chat_id != ADMIN_ID:
            return jsonify({"error": "unauthorized"}), 401
        page = int(data.get("page", 1))
        users = get_all_users()
        page_users, page, total_pages, total = paginate(users, page, per_page=20)
        items = []
        for uid, uname, tokens in page_users:
            items.append({"chat_id": uid, "username": uname or "", "tokens": tokens})
        return jsonify({
            "items": items, "page": page,
            "total_pages": total_pages, "total": total,
        })
    except Exception as e:
        log_error(f"webapp_api_admin_users: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/admin/user', methods=['POST'])
def webapp_api_admin_user():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if chat_id != ADMIN_ID:
            return jsonify({"error": "unauthorized"}), 401
        uid = int(data.get("user_id", 0))
        if not uid:
            return jsonify({"error": "no user_id"}), 400
        user = get_user(uid)
        return jsonify({
            "chat_id": uid,
            "tokens": user[0],
            "username": user[6] or "",
            "banned": bool(user[10]),
            "ban_reason": user[11] or "",
            "can_image": bool(user[12]),
            "can_chat": bool(user[13]),
            "limit_images": user[14],
            "used_images": user[16],
            "limit_edits": user[20],
            "used_edits": user[19],
            "limit_chats": user[15],
            "used_chats": user[17],
            "support_muted_until": user[21],
            "ignore_maintenance": bool(user[22]),
        })
    except Exception as e:
        log_error(f"webapp_api_admin_user: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/admin/user/set', methods=['POST'])
def webapp_api_admin_user_set():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if chat_id != ADMIN_ID:
            return jsonify({"error": "unauthorized"}), 401
        uid = int(data.get("user_id", 0))
        action = data.get("action", "")
        if not uid or not action:
            return jsonify({"error": "no params"}), 400
        user = get_user(uid)
        if action == "ban":
            reason = data.get("reason", "не указана")
            update_user(uid, 'banned', 1)
            update_user(uid, 'ban_reason', reason)
            conn = get_conn()
            c = conn.cursor()
            c.execute("INSERT INTO bans (chat_id, reason, admin_id, timestamp) VALUES (?, ?, ?, ?)",
                      (uid, reason, ADMIN_ID, int(time.time())))
            conn.commit()
            conn.close()
            try:
                bot.send_message(uid, f"🚫 <b>Вы забанены.</b>\n\nПричина: {escape_html(reason)}",
                                 parse_mode='HTML')
            except Exception:
                pass
            log_action(uid, "ban", reason)
        elif action == "unban":
            update_user(uid, 'banned', 0)
            update_user(uid, 'ban_reason', '')
            try:
                bot.send_message(uid, "✅ <b>Вы разбанены.</b>", parse_mode='HTML')
            except Exception:
                pass
        elif action == "toggle_image":
            update_user(uid, 'can_image', 0 if user[12] else 1)
        elif action == "toggle_chat":
            update_user(uid, 'can_chat', 0 if user[13] else 1)
        elif action == "toggle_ignore_maint":
            update_user(uid, 'ignore_maintenance', 0 if user[22] else 1)
        elif action == "set_limit_images":
            val = int(data.get("value", -1))
            update_user(uid, 'limit_images', val)
            reset_used(uid, 'used_images')
        elif action == "set_limit_edits":
            val = int(data.get("value", -1))
            update_user(uid, 'limit_edits', val)
            reset_used(uid, 'used_edits')
        elif action == "set_limit_chats":
            val = int(data.get("value", -1))
            update_user(uid, 'limit_chats', val)
            reset_used(uid, 'used_chats')
        elif action == "reset_limits":
            reset_used(uid)
        elif action == "clear_all":
            wipe_user_data(uid, "all")
        elif action == "add_tokens":
            cnt = int(data.get("value", 0))
            add_tokens(uid, cnt)
            try:
                new_bal = get_user(uid)[0]
                bot.send_message(uid, f"🎁 <b>Вам зачислено {cnt} токенов!</b>\n\n💎 Баланс: <b>{new_bal}</b>",
                                 parse_mode='HTML')
            except Exception:
                pass
        elif action == "take_tokens":
            cnt = int(data.get("value", 0))
            add_tokens(uid, -cnt)
            try:
                new_bal = get_user(uid)[0]
                bot.send_message(uid, f"⚠️ <b>Списано {cnt} токенов.</b>\n\n💎 Баланс: <b>{new_bal}</b>",
                                 parse_mode='HTML')
            except Exception:
                pass
        elif action == "send_message":
            text = data.get("text", "")
            try:
                bot.send_message(uid, f"📩 <b>Сообщение от администрации:</b>\n\n{escape_html(text)}",
                                 parse_mode='HTML')
            except Exception:
                pass
        else:
            return jsonify({"error": "bad action"}), 400
        return jsonify({"ok": True})
    except Exception as e:
        log_error(f"webapp_api_admin_user_set: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/admin/broadcast', methods=['POST'])
def webapp_api_admin_broadcast():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if chat_id != ADMIN_ID:
            return jsonify({"error": "unauthorized"}), 401
        text = (data.get("text") or "").strip()
        signed = bool(data.get("signed", False))
        if not text:
            return jsonify({"error": "Пусто"}), 400
        threading.Thread(target=send_broadcast, args=(text, signed), daemon=True).start()
        return jsonify({"ok": True})
    except Exception as e:
        log_error(f"webapp_api_admin_broadcast: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/admin/maintenance', methods=['POST'])
def webapp_api_admin_maintenance():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if chat_id != ADMIN_ID:
            return jsonify({"error": "unauthorized"}), 401
        action = data.get("action", "get")
        if action == "get":
            row = get_maintenance()
            return jsonify({"enabled": bool(row[2]), "message": row[1]})
        elif action == "on":
            set_maintenance(0, enabled=1, notified=0)
            return jsonify({"ok": True})
        elif action == "off":
            set_maintenance(0, enabled=0, notified=1)
            return jsonify({"ok": True})
        elif action == "set_text":
            new_text = data.get("text", "")
            set_maintenance(1, new_text)
            return jsonify({"ok": True})
        return jsonify({"error": "bad action"}), 400
    except Exception as e:
        log_error(f"webapp_api_admin_maintenance: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/admin/tickets', methods=['POST'])
def webapp_api_admin_tickets():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if chat_id != ADMIN_ID:
            return jsonify({"error": "unauthorized"}), 401
        status = data.get("status", "open")
        rows = get_tickets_by_status(status)
        items = []
        for tid, uid, uname, msg, photo, created in rows:
            items.append({
                "id": tid, "chat_id": uid, "username": uname or "",
                "message": msg, "created": created, "status": status,
            })
        return jsonify({"items": items})
    except Exception as e:
        log_error(f"webapp_api_admin_tickets: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/admin/ticket/answer', methods=['POST'])
def webapp_api_admin_ticket_answer():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if chat_id != ADMIN_ID:
            return jsonify({"error": "unauthorized"}), 401
        ticket_id = int(data.get("ticket_id", 0))
        answer = (data.get("answer") or "").strip()
        if not ticket_id or not answer:
            return jsonify({"error": "no params"}), 400
        ticket = get_ticket(ticket_id)
        if not ticket:
            return jsonify({"error": "not found"}), 404
        _, uid, uname, _, _, _ = ticket
        answer_ticket(ticket_id, answer, status="done")
        try:
            bot.send_message(uid, f"💬 <b>Ответ от поддержки</b> (тикет #{ticket_id}):\n\n{answer}",
                             parse_mode='HTML')
        except Exception:
            pass
        return jsonify({"ok": True})
    except Exception as e:
        log_error(f"webapp_api_admin_ticket_answer: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/admin/ticket/delete', methods=['POST'])
def webapp_api_admin_ticket_delete():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if chat_id != ADMIN_ID:
            return jsonify({"error": "unauthorized"}), 401
        ticket_id = int(data.get("ticket_id", 0))
        if not ticket_id:
            return jsonify({"error": "no ticket_id"}), 400
        delete_ticket(ticket_id)
        return jsonify({"ok": True})
    except Exception as e:
        log_error(f"webapp_api_admin_ticket_delete: {e}")
        return jsonify({"error": str(e)}), 500


WEBAPP_MAINTENANCE_HTML = '''<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
body{background:#17212b;color:#fff;font-family:-apple-system,Arial,sans-serif;
text-align:center;padding:60px 20px;margin:0;}
h1{font-size:80px;margin:0 0 20px;}
h2{font-size:22px;font-weight:500;margin:0 0 20px;line-height:1.4;}
p{font-size:16px;opacity:0.6;margin:0;}
</style></head>
<body>
<h1>🛠</h1>
<h2>MSG_PLACEHOLDER</h2>
<p>Поддержка работает</p>
</body></html>'''


WEBAPP_HTML = '''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>БОБ</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}
html,body{overscroll-behavior:none;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:var(--tg-theme-bg-color,#0f0f1a);color:var(--tg-theme-text-color,#fff);
min-height:100vh;padding:0;padding-bottom:20px;position:relative;}
.app-header{position:sticky;top:0;z-index:100;
background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
padding:16px;display:flex;align-items:center;gap:12px;
box-shadow:0 2px 12px rgba(0,0,0,0.3);}
.app-header .title{font-size:20px;font-weight:700;color:#fff;
flex:1;display:flex;align-items:center;gap:8px;}
.app-header .title span.emoji{font-size:24px;}
.app-header .balance{font-size:14px;color:#fff;opacity:0.95;
background:rgba(255,255,255,0.2);padding:6px 12px;border-radius:20px;
font-weight:600;}
.container{padding:16px;padding-bottom:100px;}
.menu{display:flex;flex-direction:column;gap:12px;}
.btn{display:flex;align-items:center;gap:14px;padding:18px 20px;
background:var(--tg-theme-secondary-bg-color,#1c1c2e);
border:1px solid rgba(255,255,255,0.06);
border-radius:16px;color:var(--tg-theme-text-color,#fff);
font-size:16px;font-weight:500;cursor:pointer;
transition:all 0.15s;width:100%;text-align:left;
box-shadow:0 2px 8px rgba(0,0,0,0.15);}
.btn:active{transform:scale(0.98);opacity:0.85;
background:var(--tg-theme-secondary-bg-color,#252540);}
.btn .emoji{font-size:24px;width:32px;text-align:center;}
.btn .label{flex:1;}
.btn .arrow{opacity:0.4;font-size:20px;}
.admin-section{margin-top:24px;padding-top:20px;
border-top:1px solid rgba(255,255,255,0.1);}
.admin-title{font-size:12px;opacity:0.5;margin-bottom:12px;
text-align:center;text-transform:uppercase;letter-spacing:1.5px;
font-weight:600;}
.admin-section .btn{background:linear-gradient(135deg,#f093fb 0%,#f5576c 100%);
border:none;}
.screen{display:none;animation:fadeIn 0.2s;}
.screen.active{display:block;}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px);}to{opacity:1;transform:translateY(0);}}
.screen-header{display:flex;align-items:center;gap:12px;
padding:12px 16px;background:var(--tg-theme-secondary-bg-color,#1c1c2e);
border-radius:14px;margin-bottom:16px;
position:sticky;top:72px;z-index:50;}
.back-btn{background:none;border:none;color:var(--tg-theme-text-color,#fff);
font-size:24px;cursor:pointer;padding:4px 8px 4px 0;
display:flex;align-items:center;}
.back-btn:active{opacity:0.6;}
.screen-title{font-size:17px;font-weight:600;flex:1;}
.buy-option{padding:20px;background:var(--tg-theme-secondary-bg-color,#1c1c2e);
border:1px solid rgba(255,255,255,0.06);
border-radius:14px;margin-bottom:10px;cursor:pointer;
text-align:center;font-size:16px;font-weight:600;
transition:all 0.15s;display:flex;align-items:center;justify-content:center;gap:10px;}
.buy-option:active{transform:scale(0.98);opacity:0.85;}
.history-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
.history-item{background:var(--tg-theme-secondary-bg-color,#1c1c2e);
border-radius:12px;overflow:hidden;
box-shadow:0 2px 8px rgba(0,0,0,0.15);}
.history-item img{width:100%;display:block;aspect-ratio:1;object-fit:cover;
background:#000;}
.history-item .cap{padding:8px 10px;font-size:11px;opacity:0.75;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
background:rgba(0,0,0,0.3);}
.loading{text-align:center;padding:60px 20px;opacity:0.5;font-size:15px;}
.loading::before{content:"";display:inline-block;width:24px;height:24px;
border:3px solid rgba(255,255,255,0.1);
border-top-color:#667eea;border-radius:50%;
animation:spin 0.8s linear infinite;margin-right:12px;
vertical-align:middle;}
@keyframes spin{to{transform:rotate(360deg);}}
.tabs{display:flex;gap:8px;margin-bottom:16px;
background:var(--tg-theme-secondary-bg-color,#1c1c2e);
padding:4px;border-radius:12px;}
.tab{flex:1;padding:10px;background:none;border:none;
border-radius:10px;color:var(--tg-theme-text-color,#fff);
font-size:14px;font-weight:500;cursor:pointer;opacity:0.6;
transition:all 0.15s;}
.tab.active{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
opacity:1;font-weight:600;}
.input-wrap{margin-bottom:14px;}
.input-wrap textarea,.input-wrap input{width:100%;padding:14px 16px;
background:var(--tg-theme-secondary-bg-color,#1c1c2e);
border:1px solid rgba(255,255,255,0.08);
border-radius:12px;color:var(--tg-theme-text-color,#fff);
font-size:15px;font-family:inherit;resize:none;outline:none;
transition:border 0.15s;}
.input-wrap textarea:focus,.input-wrap input:focus{border-color:#667eea;}
.input-wrap textarea{min-height:100px;}
.action-btn{width:100%;padding:16px;border:none;border-radius:14px;
background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
color:#fff;font-size:16px;font-weight:600;cursor:pointer;
transition:all 0.15s;box-shadow:0 4px 16px rgba(102,126,234,0.4);}
.action-btn:active{transform:scale(0.98);opacity:0.9;}
.action-btn:disabled{opacity:0.5;cursor:not-allowed;
box-shadow:none;transform:none;}
.chat-messages{display:flex;flex-direction:column;gap:12px;
margin-bottom:16px;}
.msg{max-width:85%;padding:12px 16px;border-radius:16px;
font-size:15px;line-height:1.4;word-wrap:break-word;
white-space:pre-wrap;}
.msg.user{align-self:flex-end;
background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
color:#fff;border-bottom-right-radius:4px;}
.msg.bot{align-self:flex-start;
background:var(--tg-theme-secondary-bg-color,#1c1c2e);
color:var(--tg-theme-text-color,#fff);
border-bottom-left-radius:4px;
border:1px solid rgba(255,255,255,0.06);}
.progress-wrap{margin:20px 0;}
.progress-bar{width:100%;height:8px;background:rgba(255,255,255,0.1);
border-radius:10px;overflow:hidden;}
.progress-fill{height:100%;
background:linear-gradient(90deg,#667eea 0%,#764ba2 100%);
border-radius:10px;transition:width 0.3s ease;
box-shadow:0 0 12px rgba(102,126,234,0.6);}
.progress-text{text-align:center;margin-top:12px;
font-size:14px;opacity:0.8;}
.result-img{width:100%;border-radius:16px;
box-shadow:0 4px 24px rgba(0,0,0,0.4);margin-bottom:16px;
display:block;}
.result-caption{text-align:center;font-size:14px;opacity:0.7;
margin-bottom:16px;padding:0 10px;line-height:1.4;}
.err{color:#ff6b6b;text-align:center;padding:40px 20px;font-size:15px;
background:rgba(255,107,107,0.1);border-radius:12px;}
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;
background:rgba(0,0,0,0.7);z-index:1000;
display:none;align-items:center;justify-content:center;padding:24px;
backdrop-filter:blur(6px);}
.modal-overlay.active{display:flex;animation:fadeIn 0.15s;}
.modal-box{background:var(--tg-theme-secondary-bg-color,#1c1c2e);
border-radius:20px;padding:24px;width:100%;max-width:340px;
box-shadow:0 8px 40px rgba(0,0,0,0.5);
animation:modalIn 0.2s ease;}
@keyframes modalIn{from{transform:scale(0.9);opacity:0;}to{transform:scale(1);opacity:1;}}
.modal-title{font-size:18px;font-weight:700;margin-bottom:12px;
text-align:center;}
.modal-text{font-size:15px;opacity:0.8;margin-bottom:24px;
text-align:center;line-height:1.4;}
.modal-buttons{display:flex;gap:10px;}
.modal-btn{flex:1;padding:14px;border:none;border-radius:12px;
font-size:15px;font-weight:600;cursor:pointer;transition:all 0.15s;}
.modal-btn.cancel{background:rgba(255,255,255,0.1);
color:var(--tg-theme-text-color,#fff);}
.modal-btn.confirm{background:linear-gradient(135deg,#f5576c 0%,#f093fb 100%);color:#fff;}
.modal-btn:active{transform:scale(0.97);}
.setting-row{display:flex;align-items:center;justify-content:space-between;
padding:16px;background:var(--tg-theme-secondary-bg-color,#1c1c2e);
border-radius:14px;margin-bottom:10px;
border:1px solid rgba(255,255,255,0.06);}
.setting-row .lbl{font-size:15px;display:flex;align-items:center;gap:10px;}
.setting-row .lbl .emoji{font-size:20px;}
.setting-row .val{font-size:14px;font-weight:600;opacity:0.8;}
.setting-row .toggle{width:52px;height:30px;border-radius:20px;
background:rgba(255,255,255,0.15);position:relative;cursor:pointer;
transition:background 0.2s;flex-shrink:0;}
.setting-row .toggle.on{background:#667eea;}
.setting-row .toggle::after{content:"";position:absolute;
top:3px;left:3px;width:24px;height:24px;border-radius:50%;
background:#fff;transition:transform 0.2s;
box-shadow:0 2px 6px rgba(0,0,0,0.3);}
.setting-row .toggle.on::after{transform:translateX(22px);}
.mode-option{padding:14px 18px;background:var(--tg-theme-secondary-bg-color,#1c1c2e);
border:1px solid rgba(255,255,255,0.06);
border-radius:12px;margin-bottom:8px;cursor:pointer;
display:flex;align-items:center;gap:12px;font-size:15px;
transition:all 0.15s;}
.mode-option:active{transform:scale(0.98);}
.mode-option.active{border-color:#667eea;
background:linear-gradient(135deg,rgba(102,126,234,0.15),rgba(118,75,162,0.15));}
.mode-option .check{margin-left:auto;font-size:18px;
color:#667eea;opacity:0;}
.mode-option.active .check{opacity:1;}
.output-box{padding:16px;background:var(--tg-theme-secondary-bg-color,#1c1c2e);
border-radius:14px;font-size:14px;line-height:1.5;
white-space:pre-wrap;word-wrap:break-word;margin-bottom:14px;
border:1px solid rgba(255,255,255,0.06);
max-height:60vh;overflow-y:auto;}
.section-title{font-size:13px;opacity:0.6;text-transform:uppercase;
letter-spacing:1px;margin:20px 0 12px;font-weight:600;}
.stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;
margin-bottom:16px;}
.stat-card{padding:16px;background:var(--tg-theme-secondary-bg-color,#1c1c2e);
border-radius:12px;border:1px solid rgba(255,255,255,0.06);}
.stat-card .num{font-size:22px;font-weight:700;
background:linear-gradient(135deg,#667eea,#764ba2);
-webkit-background-clip:text;-webkit-text-fill-color:transparent;
background-clip:text;margin-bottom:4px;}
.stat-card .lbl{font-size:12px;opacity:0.6;font-weight:500;}
</style>
</head>
<body>
<div class="app-header">
    <div class="title"><span class="emoji">🤖</span> БОБ</div>
    <div class="balance">💰 <span id="balance-tokens">...</span></div>
</div>
<div class="container">
<div id="screen-main" class="screen active">
    <div class="menu">
        <button class="btn" onclick="showScreen('generate')">
            <span class="emoji">🎨</span><span class="label">Сгенерировать</span><span class="arrow">›</span>
        </button>
        <button class="btn" onclick="openBot('menu_edit')">
            <span class="emoji">🖼</span><span class="label">Редактировать фото</span><span class="arrow">›</span>
        </button>
        <button class="btn" onclick="showScreen('chat')">
            <span class="emoji">🤖</span><span class="label">Чат с ИИ</span><span class="arrow">›</span>
        </button>
        <button class="btn" onclick="showScreen('history')">
            <span class="emoji">📜</span><span class="label">История</span><span class="arrow">›</span>
        </button>
        <button class="btn" onclick="showScreen('buy')">
            <span class="emoji">💳</span><span class="label">Купить токены</span><span class="arrow">›</span>
        </button>
        <button class="btn" onclick="showScreen('settings')">
            <span class="emoji">⚙️</span><span class="label">Настройки</span><span class="arrow">›</span>
        </button>
        <button class="btn" onclick="openBot('menu_support')">
            <span class="emoji">🆘</span><span class="label">Поддержка</span><span class="arrow">›</span>
        </button>
        <div id="admin-block" style="display:none;" class="admin-section">
            <div class="admin-title">Админ</div>
            <button class="btn" onclick="showScreen('admin')">
                <span class="emoji">👑</span><span class="label">Админ-панель</span><span class="arrow">›</span>
            </button>
        </div>
    </div>
</div>
<div id="screen-generate" class="screen">
    <div class="screen-header">
        <button class="back-btn" onclick="showScreen('main')">←</button>
        <span class="screen-title">🎨 Генерация</span>
    </div>
    <div id="gen-form">
        <div class="input-wrap">
            <textarea id="gen-prompt" placeholder="Опиши что нарисовать..."></textarea>
        </div>
        <button class="action-btn" id="gen-btn" onclick="doGenerate()">✅ Нарисовать — 4💎</button>
    </div>
    <div id="gen-progress" style="display:none;">
        <div class="progress-wrap">
            <div class="progress-bar"><div class="progress-fill" id="gen-fill" style="width:0%;"></div></div>
            <div class="progress-text" id="gen-status">⏳ Анализирую...</div>
        </div>
    </div>
    <div id="gen-result" style="display:none;"></div>
</div>
<div id="screen-chat" class="screen">
    <div class="screen-header">
        <button class="back-btn" onclick="showScreen('main')">←</button>
        <span class="screen-title">🤖 Чат с Бобом</span>
    </div>
    <div class="chat-messages" id="chat-messages"></div>
    <div class="input-wrap">
        <textarea id="chat-input" placeholder="Задай вопрос..." style="min-height:60px;"></textarea>
    </div>
    <button class="action-btn" id="chat-btn" onclick="doChat()">📤 Отправить — 1💎</button>
</div>
<div id="screen-history" class="screen">
    <div class="screen-header">
        <button class="back-btn" onclick="showScreen('main')">←</button>
        <span class="screen-title">📜 История</span>
    </div>
    <div class="tabs">
        <button class="tab active" onclick="loadHistory('gen',this)">🎨 Генерации</button>
        <button class="tab" onclick="loadHistory('edit',this)">🖼 Правки</button>
    </div>
    <div id="history-content"><div class="loading">Загрузка...</div></div>
</div>
<div id="screen-buy" class="screen">
    <div class="screen-header">
        <button class="back-btn" onclick="showScreen('main')">←</button>
        <span class="screen-title">💳 Купить токены</span>
    </div>
    <div class="buy-option" onclick="buyTokens(100,20)">💵 100 ₽ — 20 токенов</div>
    <div class="buy-option" onclick="buyTokens(250,50)">💵 250 ₽ — 50 токенов</div>
    <div class="buy-option" onclick="buyTokens(500,100)">💵 500 ₽ — 100 токенов</div>
    <div class="buy-option" onclick="buyTokens(1000,200)">💵 1000 ₽ — 200 токенов</div>
</div>
<div id="screen-settings" class="screen">
    <div class="screen-header">
        <button class="back-btn" onclick="showScreen('main')">←</button>
        <span class="screen-title">⚙️ Настройки</span>
    </div>
    <div class="section-title">🎯 Режим</div>
    <div id="modes-list"></div>
    <div class="section-title">🧠 Поведение</div>
    <div id="ai-modes-list"></div>
    <div class="section-title">🔧 Прочее</div>
    <div class="setting-row" onclick="toggleSetting('send_files',this)">
        <div class="lbl"><span class="emoji">📎</span> Ответы файлами</div>
        <div class="toggle" id="toggle-send_files"></div>
    </div>
    <div class="setting-row" onclick="toggleSetting('sound_on',this)">
        <div class="lbl"><span class="emoji">🔊</span> Звук уведомлений</div>
        <div class="toggle" id="toggle-sound_on"></div>
    </div>
</div>
<div id="screen-admin" class="screen">
    <div class="screen-header">
        <button class="back-btn" onclick="showScreen('main')">←</button>
        <span class="screen-title">👑 Админ-панель</span>
    </div>
    <div class="stats-grid" id="admin-stats"></div>
    <div class="section-title">📢 Рассылка</div>
    <div class="input-wrap">
        <textarea id="broadcast-text" placeholder="Текст рассылки..."></textarea>
    </div>
    <button class="action-btn" onclick="sendBroadcast()">📢 Отправить</button>
    <div class="section-title">🛠 Тех.работы</div>
    <button class="action-btn" id="maint-btn" onclick="toggleMaintenance()">Загрузка...</button>
    <div class="section-title">👥 Список юзеров</div>
    <div id="admin-users" class="loading">Загрузка...</div>
</div>
<div class="modal-overlay" id="exit-modal">
    <div class="modal-box">
        <div class="modal-title">🚪 Выйти из приложения?</div>
        <div class="modal-text">Вы точно хотите выйти?</div>
        <div class="modal-buttons">
            <button class="modal-btn cancel" onclick="closeExitModal()">Остаться</button>
            <button class="modal-btn confirm" onclick="confirmExit()">Выйти</button>
        </div>
    </div>
</div>
</div>
<script>
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();
tg.setHeaderColor('#667eea');
tg.setBackgroundColor('#0f0f1a');
const initData = tg.initData;
let state = {me:null,isAdmin:false,currentScreen:'main',isGenerating:false,isChatting:false};
async function apiCall(endpoint, data = {}) {
    data.init_data = initData;
    try {
        const res = await fetch('/webapp/api/' + endpoint, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data),
        });
        return await res.json();
    } catch (e) { return {error: 'network: ' + e.message}; }
}
function showScreen(name) {
    if (state.isGenerating && name !== 'generate') { tg.showAlert('⏳ Генерация идёт. Подожди окончания.'); return; }
    if (state.isChatting && name !== 'chat') { tg.showAlert('⏳ Боб думает. Подожди ответа.'); return; }
    document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
    document.getElementById('screen-' + name).classList.add('active');
    state.currentScreen = name;
    if (name === 'history') loadHistory('gen', document.querySelector('.tab.active'));
    if (name === 'settings') loadSettings();
    if (name === 'admin') { loadAdminStats(); loadAdminUsers(1); loadMaintenanceStatus(); }
    if (name === 'chat') loadChatHistory();
    if (name === 'generate') resetGenForm();
    updateBackButton();
}
function openExitModal() { document.getElementById('exit-modal').classList.add('active'); }
function closeExitModal() { document.getElementById('exit-modal').classList.remove('active'); }
function confirmExit() { closeExitModal(); tg.close(); }
tg.BackButton.onClick(function() {
    if (state.currentScreen === 'main') openExitModal();
    else showScreen('main');
});
function updateBackButton() {
    if (state.currentScreen !== 'main') tg.BackButton.show();
    else tg.BackButton.hide();
}
async function loadMe() {
    const d = await apiCall('me');
    if (d.error === 'banned') {
        document.body.innerHTML = '<div style="text-align:center;padding:80px 20px;"><h1 style="font-size:60px;margin-bottom:20px;">🚫</h1><h2 style="margin-bottom:12px;">Вы забанены</h2><p style="opacity:0.7;">Причина: ' + escapeHtml(d.reason || 'не указана') + '</p></div>';
        return;
    }
    if (d.error) { document.getElementById('balance-tokens').textContent = 'ошибка'; return; }
    state.me = d; state.isAdmin = d.is_admin;
    document.getElementById('balance-tokens').textContent = d.tokens;
    if (d.is_admin) document.getElementById('admin-block').style.display = 'block';
}
function updateBalance(newBalance) {
    if (newBalance !== undefined && newBalance !== null) {
        state.me.tokens = newBalance;
        document.getElementById('balance-tokens').textContent = newBalance;
    }
}
function resetGenForm() {
    document.getElementById('gen-form').style.display = 'block';
    document.getElementById('gen-progress').style.display = 'none';
    document.getElementById('gen-result').style.display = 'none';
    document.getElementById('gen-result').innerHTML = '';
    document.getElementById('gen-fill').style.width = '0%';
    document.getElementById('gen-status').textContent = '⏳ Анализирую...';
    const btn = document.getElementById('gen-btn');
    btn.disabled = false; btn.textContent = '✅ Нарисовать — 4💎';
}
async function doGenerate() {
    if (state.isGenerating) return;
    const prompt = (document.getElementById('gen-prompt').value || '').trim();
    if (!prompt) { tg.showAlert('❌ Введи промт'); return; }
    if (!state.me) { tg.showAlert('❌ Ошибка загрузки профиля'); return; }
    if (state.me.tokens < 4) { tg.showAlert('❌ Нужно 4 токена'); return; }
    state.isGenerating = true;
    document.getElementById('gen-form').style.display = 'none';
    document.getElementById('gen-progress').style.display = 'block';
    document.getElementById('gen-result').style.display = 'none';
    let percent = 0;
    const stages = [[0,'⏳ Анализирую...'],[20,'🎨 Начинаю...'],[40,'💎 Рисую основу...'],[60,'✨ Добавляю детали...'],[80,'🔥 Финал...'],[95,'✅ Почти готово...']];
    let status = '⏳ Анализирую...';
    const interval = setInterval(() => {
        if (percent < 90) percent += Math.random() * 8;
        else percent = Math.min(percent + 1, 95);
        document.getElementById('gen-fill').style.width = percent + '%';
        for (const [p, s] of stages) if (percent >= p) status = s;
        document.getElementById('gen-status').textContent = status;
    }, 400);
    const d = await apiCall('generate', {prompt});
    clearInterval(interval);
    if (d.error) {
        document.getElementById('gen-fill').style.width = '100%';
        document.getElementById('gen-status').textContent = '❌ Ошибка';
        setTimeout(() => { resetGenForm(); tg.showAlert('❌ ' + d.error); }, 600);
        state.isGenerating = false;
        return;
    }
    document.getElementById('gen-fill').style.width = '100%';
    document.getElementById('gen-status').textContent = '✅ Готово!';
    updateBalance(d.balance);
    setTimeout(() => {
        document.getElementById('gen-progress').style.display = 'none';
        document.getElementById('gen-result').style.display = 'block';
        document.getElementById('gen-result').innerHTML = '<img class="result-img" src="' + d.url + '"><div class="result-caption">🎨 ' + escapeHtml(d.prompt) + '</div><button class="action-btn" onclick="resetGenForm()" style="margin-bottom:10px;">🔁 Ещё раз — 4💎</button><button class="action-btn" onclick="showScreen(\'main\')" style="background:rgba(255,255,255,0.1);box-shadow:none;">🏠 В меню</button>';
        state.isGenerating = false;
    }, 800);
}
async function loadChatHistory() {
    const d = await apiCall('history/chat');
    const container = document.getElementById('chat-messages');
    container.innerHTML = '';
    if (d.error) { container.innerHTML = '<div class="err">Ошибка: ' + escapeHtml(d.error) + '</div>'; return; }
    if (!d.items || d.items.length === 0) { container.innerHTML = '<div style="text-align:center;opacity:0.5;padding:30px;font-size:14px;">Задай первый вопрос Бобу 👇</div>'; return; }
    for (const item of d.items) appendMessage(item.role, item.content);
    scrollChatBottom();
}
function appendMessage(role, text) {
    const container = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = 'msg ' + (role === 'user' ? 'user' : 'bot');
    div.textContent = text;
    container.appendChild(div);
}
function scrollChatBottom() { setTimeout(() => { window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'}); }, 100); }
async function doChat() {
    if (state.isChatting) return;
    const input = document.getElementById('chat-input');
    const question = (input.value || '').trim();
    if (!question) return;
    if (!state.me || state.me.tokens < 1) { tg.showAlert('❌ Нужно 1 токен'); return; }
    state.isChatting = true;
    input.value = '';
    appendMessage('user', question);
    scrollChatBottom();
    const btn = document.getElementById('chat-btn');
    btn.disabled = true; btn.textContent = '⏳ Боб думает...';
    const typingDiv = document.createElement('div');
    typingDiv.className = 'msg bot'; typingDiv.id = 'typing-msg';
    typingDiv.textContent = '⏳ Печатает...';
    document.getElementById('chat-messages').appendChild(typingDiv);
    scrollChatBottom();
    const d = await apiCall('chat', {question});
    const typing = document.getElementById('typing-msg');
    if (typing) typing.remove();
    btn.disabled = false; btn.textContent = '📤 Отправить — 1💎';
    if (d.error) { appendMessage('bot', '❌ ' + d.error); state.isChatting = false; scrollChatBottom(); return; }
    updateBalance(d.balance);
    appendMessage('bot', d.answer);
    scrollChatBottom();
    state.isChatting = false;
}
async function loadHistory(kind, tabEl) {
    document.querySelectorAll('#screen-history .tab').forEach(t => t.classList.remove('active'));
    if (tabEl) tabEl.classList.add('active');
    const c = document.getElementById('history-content');
    c.innerHTML = '<div class="loading">Загрузка...</div>';
    const d = await apiCall('history/image', {kind});
    if (d.error) { c.innerHTML = '<div class="err">Ошибка: ' + escapeHtml(d.error) + '</div>'; return; }
    if (!d.items || d.items.length === 0) { c.innerHTML = '<div style="text-align:center;padding:50px 20px;opacity:0.5;">История пуста<br><span style="font-size:13px;">Сгенерируй первую картинку 🎨</span></div>'; return; }
    let html = '<div class="history-grid">';
    for (const it of d.items) {
        html += '<div class="history-item"><img src="' + it.url + '" loading="lazy" onclick="tg.openLink(\'' + it.url + '\')"><div class="cap">' + escapeHtml((it.prompt || '').substring(0, 40)) + '</div></div>';
    }
    html += '</div>';
    c.innerHTML = html;
}
async function buyTokens(amount, tokens) {
    tg.showConfirm('Купить ' + tokens + ' токенов за ' + amount + ' ₽?', async (ok) => {
        if (!ok) return;
        const d = await apiCall('buy', {amount, tokens});
        if (d.error) { tg.showAlert('❌ ' + d.error); return; }
        tg.openLink(d.pay_url);
    });
}
async function loadSettings() {
    if (!state.me) return;
    const modes = [{key:'regular',label:'🤖 Обычный ИИ'},{key:'coder',label:'💻 Кодер'},{key:'explainer',label:'📖 Объяснятор'},{key:'translator',label:'🌍 Переводчик'}];
    const modesEl = document.getElementById('modes-list');
    modesEl.innerHTML = '';
    for (const m of modes) {
        const div = document.createElement('div');
        div.className = 'mode-option' + (state.me.mode === m.key ? ' active' : '');
        div.innerHTML = m.label + '<span class="check">✓</span>';
        div.onclick = () => setMode('mode', m.key);
        modesEl.appendChild(div);
    }
    const aiModes = [{key:'regular',label:'🤖 Обычный'},{key:'smart',label:'🧠 Умный'},{key:'open',label:'💬 Откровенный'},{key:'uncensored',label:'🔥 Без цензуры'}];
    const aiEl = document.getElementById('ai-modes-list');
    aiEl.innerHTML = '';
    for (const m of aiModes) {
        const div = document.createElement('div');
        div.className = 'mode-option' + (state.me.ai_mode === m.key ? ' active' : '');
        div.innerHTML = m.label + '<span class="check">✓</span>';
        div.onclick = () => setMode('ai_mode', m.key);
        aiEl.appendChild(div);
    }
    document.getElementById('toggle-send_files').classList.toggle('on', state.me.send_files);
    document.getElementById('toggle-sound_on').classList.toggle('on', state.me.sound_on);
}
async function setMode(field, value) {
    const d = await apiCall('settings', {field, value});
    if (d.error) { tg.showAlert('❌ ' + d.error); return; }
    state.me[field] = value;
    tg.HapticFeedback.impactOccurred('light');
    loadSettings();
}
async function toggleSetting(field, el) {
    const d = await apiCall('settings', {field});
    if (d.error) { tg.showAlert('❌ ' + d.error); return; }
    state.me[field] = d.value;
    document.getElementById('toggle-' + field).classList.toggle('on', d.value);
    tg.HapticFeedback.impactOccurred('light');
}
async function loadAdminStats() {
    const d = await apiCall('admin/stats');
    if (d.error) return;
    const el = document.getElementById('admin-stats');
    el.innerHTML = '<div class="stat-card"><div class="num">' + d.users + '</div><div class="lbl">👥 Юзеров</div></div><div class="stat-card"><div class="num">' + d.tokens + '</div><div class="lbl">💰 Токенов</div></div><div class="stat-card"><div class="num">' + d.images_gen + '</div><div class="lbl">🎨 Генераций</div></div><div class="stat-card"><div class="num">' + d.images_edit + '</div><div class="lbl">🖼 Правок</div></div><div class="stat-card"><div class="num">' + d.ai_messages + '</div><div class="lbl">🤖 Сообщений</div></div><div class="stat-card"><div class="num">' + d.orders_paid + '</div><div class="lbl">💳 Оплат</div></div><div class="stat-card"><div class="num">' + d.tickets_open + '</div><div class="lbl">📋 Тикетов</div></div><div class="stat-card"><div class="num">' + d.banned + '</div><div class="lbl">🚫 Банов</div></div>';
}
async function loadAdminUsers(page) {
    const el = document.getElementById('admin-users');
    el.innerHTML = '<div class="loading">Загрузка...</div>';
    const d = await apiCall('admin/users', {page});
    if (d.error) { el.innerHTML = '<div class="err">Ошибка: ' + escapeHtml(d.error) + '</div>'; return; }
    let html = '';
    for (const u of d.items) {
        const name = u.username || '—';
        html += '<div class="setting-row" style="padding:12px 14px;"><div class="lbl" style="font-size:14px;">🆔 <code style="font-size:12px;">' + u.chat_id + '</code> ' + escapeHtml(name) + '</div><div class="val">' + u.tokens + ' 💎</div></div>';
    }
    html += '<div style="text-align:center;margin-top:10px;opacity:0.6;font-size:13px;">Стр. ' + d.page + '/' + d.total_pages + ' • Всего: ' + d.total + '</div>';
    el.innerHTML = html;
}
async function sendBroadcast() {
    const text = (document.getElementById('broadcast-text').value || '').trim();
    if (!text) { tg.showAlert('❌ Введи текст'); return; }
    tg.showConfirm('Отправить рассылку всем юзерам?', async (ok) => {
        if (!ok) return;
        const d = await apiCall('admin/broadcast', {text, signed: true});
        if (d.error) { tg.showAlert('❌ ' + d.error); return; }
        document.getElementById('broadcast-text').value = '';
        tg.showAlert('✅ Рассылка запущена');
    });
}
async function loadMaintenanceStatus() {
    const d = await apiCall('admin/maintenance', {action: 'get'});
    const btn = document.getElementById('maint-btn');
    if (d.error) { btn.textContent = '❌ Ошибка'; return; }
    btn.textContent = d.enabled ? '🔴 Выключить тех.работы' : '🟢 Включить тех.работы';
    btn.dataset.enabled = d.enabled ? '1' : '0';
}
async function toggleMaintenance() {
    const btn = document.getElementById('maint-btn');
    const enabled = btn.dataset.enabled === '1';
    const action = enabled ? 'off' : 'on';
    tg.showConfirm(enabled ? 'Выключить тех.работы?' : '⚠️ Включить тех.работы?', async (ok) => {
        if (!ok) return;
        const d = await apiCall('admin/maintenance', {action});
        if (d.error) { tg.showAlert('❌ ' + d.error); return; }
        tg.showAlert(enabled ? '✅ Выключены' : '✅ Включены');
        loadMaintenanceStatus();
    });
}
function openBot(callback) { tg.sendData(JSON.stringify({callback})); tg.close(); }
function escapeHtml(t) { const d = document.createElement('div'); d.textContent = t == null ? '' : String(t); return d.innerHTML; }
loadMe();
document.addEventListener('DOMContentLoaded', () => { updateBackButton(); });
setInterval(() => { if (state.currentScreen === 'main') loadMe(); }, 60000);
</script>
</body>
</html>'''

@app.route('/webapp/')
def webapp_index():
    try:
        row = get_maintenance()
        enabled = row[2] if len(row) > 2 else 0
        if enabled:
            msg = row[1] if len(row) > 1 else "Технические работы"
            return WEBAPP_MAINTENANCE_HTML.replace("MSG_PLACEHOLDER", escape_html(msg))
        return WEBAPP_HTML
    except Exception as e:
        log_error(f"webapp_index: {e}")
        return WEBAPP_HTML


@bot.message_handler(content_types=['web_app_data'])
def handle_web_app_data(message):
    try:
        data_str = message.web_app_data.data
        data = json.loads(data_str)
        callback = data.get("callback", "")
        bot.send_message(
            message.chat.id,
            f"✅ Открыт бот. Нажми /start если не видно меню.\n\n<i>Действие: {callback}</i>",
            parse_mode='HTML'
        )
    except Exception as e:
        log_error(f"handle_web_app_data: {e}")


def run_flask():
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 3000)), threaded=True)


if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=send_startup_report, daemon=True).start()
    threading.Thread(target=maintenance_worker, daemon=True).start()
    threading.Thread(target=cleanup_orders_worker, daemon=True).start()
    threading.Thread(target=cleanup_edit_cache, daemon=True).start()
    threading.Thread(target=cleanup_active_operations, daemon=True).start()
    threading.Thread(target=cleanup_errors_log, daemon=True).start()
    threading.Thread(target=errors_uploader, daemon=True).start()
    threading.Thread(target=backup_worker, daemon=True).start()
    threading.Thread(target=broadcast_worker, daemon=True).start()
    while True:
        try:
            bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
        except Exception as e:
            log_error(f"Polling error: {e}")
            time.sleep(5)
