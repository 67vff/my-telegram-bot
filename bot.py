# ============================================================
# БОБ AI — Telegram Bot + WebApp
# ============================================================

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
MAX_TEXT_RESPONSE = 3500

last_broadcast = {"messages": [], "active": False}
_last_error_notify = {}
_last_ai_message = {}
_gigachat_cache = {"token": None, "expires": 0}
_gigachat_lock = threading.Lock()
edit_photos_cache = {}

print("[CONFIG] ============================================", flush=True)
print(f"[CONFIG] BOT_TOKEN: {'OK' if BOT_TOKEN else 'MISSING!'}", flush=True)
print(f"[CONFIG] YOOMONEY_RECEIVER: {'OK' if YOOMONEY_RECEIVER else 'MISSING'}", flush=True)
print(f"[CONFIG] YOOMONEY_SECRET: {'OK' if YOOMONEY_SECRET else 'MISSING'}", flush=True)
print(f"[CONFIG] GIGACHAT_AUTH_KEY: {'OK' if GIGACHAT_AUTH_KEY else 'MISSING'}", flush=True)
print(f"[CONFIG] BOTHUB_API_KEY: {'OK' if BOTHUB_API_KEY else 'MISSING'}", flush=True)
print(f"[CONFIG] ADMIN_ID: {ADMIN_ID}", flush=True)
print("[CONFIG] ============================================", flush=True)


def escape_html(text):
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def log_error(error_msg, chat_id=None, username=None):
    error_str = str(error_msg)[:500]
    now = int(time.time())
    path = "/app/data/errors.log"
    ctx = ""
    if chat_id:
        ctx = f" chat_id={chat_id}"
        if username:
            ctx += f" ({username})"
    try:
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size > 40 * 1024 * 1024:
                try:
                    os.remove(path)
                except Exception:
                    pass
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
    text = f"Ошибка\n\n{time.strftime('%d.%m.%Y %H:%M:%S')}\n"
    if chat_id:
        text += f"chat_id: {chat_id}\n"
    text += f"\n{escape_html(error_str)}"
    try:
        bot.send_message(ERRORS_CHANNEL_ID, text)
    except Exception:
        pass


def is_code_response(text):
    if not text:
        return False
    if "```" in text:
        return True
    marks = ["def ", "class ", "function ", "<?php", "public static",
             "#include", "fn main", "package main"]
    return any(m in text for m in marks)


def detect_code_language(text):
    if not text:
        return "txt"
    m = re.search(r"```(\w+)", text)
    if m:
        lang = m.group(1).lower()
        mapping = {
            "python": "py", "py": "py", "javascript": "js", "js": "js",
            "typescript": "ts", "ts": "ts", "cpp": "cpp", "c++": "cpp",
            "c": "c", "java": "java", "go": "go", "rust": "rs", "rs": "rs",
            "ruby": "rb", "php": "php", "html": "html", "css": "css",
            "sql": "sql", "bash": "sh", "sh": "sh", "yaml": "yml",
            "json": "json", "xml": "xml", "markdown": "md",
        }
        return mapping.get(lang, "txt")
    return "txt"


def extract_code_block(text):
    if not text:
        return ""
    m = re.search(r"```(?:\w+)?\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    return text


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


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
    c.execute('''CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        title TEXT DEFAULT 'Новый чат',
        created INTEGER,
        last_message INTEGER
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
        ("message", "TEXT DEFAULT 'Технические работы'"),
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
    try:
        c.execute("ALTER TABLE history ADD COLUMN user_chat_id INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    c.execute("INSERT OR IGNORE INTO maintenance (id, active, message, enabled, notified) VALUES (1, 0, 'Технические работы', 0, 1)")
    conn.commit()
    conn.close()


init_db()
print("[DB] Database initialized", flush=True)


def get_user(chat_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""SELECT tokens, state, mode, ai_mode, trial_started, trial_used, username,
                 registered, coder_mode, banned, ban_reason, can_image, can_chat,
                 limit_images, limit_chats, used_images, used_chats, send_files,
                 used_edits, limit_edits, support_muted_until, ignore_maintenance, sound_on
                 FROM users WHERE chat_id=?""", (chat_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT OR IGNORE INTO users (chat_id, registered) VALUES (?, ?)", (chat_id, int(time.time())))
        conn.commit()
        c.execute("""SELECT tokens, state, mode, ai_mode, trial_started, trial_used, username,
                     registered, coder_mode, banned, ban_reason, can_image, can_chat,
                     limit_images, limit_chats, used_images, used_chats, send_files,
                     used_edits, limit_edits, support_muted_until, ignore_maintenance, sound_on
                     FROM users WHERE chat_id=?""", (chat_id,))
        row = c.fetchone()
        if not row:
            row = (0, 'idle', 'regular', 'regular', 0, 0, '', int(time.time()),
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


def notify_subscribers(user_id, text):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT admin_id FROM subscriptions WHERE user_id=?", (user_id,))
    rows = c.fetchall()
    conn.close()
    for (admin_id,) in rows:
        try:
            bot.send_message(admin_id, f"Уведомление\n\n{text}")
        except Exception:
            pass


def get_or_create_active_chat(chat_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM chats WHERE chat_id=? ORDER BY last_message DESC LIMIT 1", (chat_id,))
    row = c.fetchone()
    if row:
        conn.close()
        return row[0]
    now = int(time.time())
    c.execute("INSERT INTO chats (chat_id, title, created, last_message) VALUES (?, ?, ?, ?)",
              (chat_id, "Новый чат", now, now))
    conn.commit()
    chat_db_id = c.lastrowid
    conn.close()
    return chat_db_id


def create_new_chat(chat_id):
    conn = get_conn()
    c = conn.cursor()
    now = int(time.time())
    c.execute("INSERT INTO chats (chat_id, title, created, last_message) VALUES (?, ?, ?, ?)",
              (chat_id, f"Чат {time.strftime('%d.%m %H:%M')}", now, now))
    conn.commit()
    chat_db_id = c.lastrowid
    conn.close()
    return chat_db_id


def get_user_chats(chat_id, limit=20):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""SELECT id, title, created, last_message,
                 (SELECT COUNT(*) FROM history WHERE user_chat_id = chats.id) as msg_count
                 FROM chats WHERE chat_id=? ORDER BY last_message DESC LIMIT ?""",
              (chat_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows


def delete_chat(chat_id, chat_db_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM chats WHERE id=? AND chat_id=?", (chat_db_id, chat_id))
    c.execute("DELETE FROM history WHERE user_chat_id=? AND chat_id=?", (chat_db_id, chat_id))
    conn.commit()
    conn.close()


def rename_chat(chat_id, chat_db_id, new_title):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE chats SET title=? WHERE id=? AND chat_id=?", (new_title[:50], chat_db_id, chat_id))
    conn.commit()
    conn.close()


def update_chat_title_from_message(chat_id, chat_db_id, message):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT title FROM chats WHERE id=? AND chat_id=?", (chat_db_id, chat_id))
    row = c.fetchone()
    if row and row[0] == "Новый чат":
        title = message[:40] + ("..." if len(message) > 40 else "")
        c.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_db_id))
    c.execute("UPDATE chats SET last_message=? WHERE id=?", (int(time.time()), chat_db_id))
    conn.commit()
    conn.close()


def get_chat_messages(chat_id, chat_db_id, limit=200):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""SELECT role, content FROM history
                 WHERE chat_id=? AND user_chat_id=?
                 ORDER BY id ASC LIMIT ?""",
              (chat_id, chat_db_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows


def get_chat_title(chat_id, chat_db_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT title FROM chats WHERE id=? AND chat_id=?", (chat_db_id, chat_id))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    return "Новый чат"


def add_to_history(chat_id, role, content, user_chat_id=None):
    if user_chat_id is None:
        user_chat_id = get_or_create_active_chat(chat_id)
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO history (chat_id, role, content, user_chat_id) VALUES (?, ?, ?, ?)",
              (chat_id, role, content, user_chat_id))
    conn.commit()
    conn.close()
    update_chat_title_from_message(chat_id, user_chat_id, content)


def get_history(chat_id, limit=20, user_chat_id=None):
    if user_chat_id is None:
        user_chat_id = get_or_create_active_chat(chat_id)
    conn = get_conn()
    c = conn.cursor()
    c.execute("""SELECT role, content FROM history
                 WHERE chat_id=? AND user_chat_id=?
                 ORDER BY id DESC LIMIT ?""",
              (chat_id, user_chat_id, limit))
    rows = c.fetchall()
    conn.close()
    return list(reversed(rows))


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
        return (0, "Технические работы", 0, 1)
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


def check_support_muted(chat_id):
    user = get_user(chat_id)
    muted_until = user[20]
    now = int(time.time())
    if muted_until > now:
        left = muted_until - now
        return True, f"{left // 60} мин {left % 60} сек"
    return False, ""


def can_use_during_maintenance(chat_id):
    if chat_id == ADMIN_ID:
        return True
    user = get_user(chat_id)
    return bool(user[21])


def check_banned(chat_id):
    user = get_user(chat_id)
    return bool(user[9]), user[10] if user[10] else ""


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
            log_error(f"GigaChat token missing: {str(result)[:200]}")
            return None
        except Exception as e:
            log_error(f"GigaChat token error: {e}")
            return None


BASE_PROMPT = (
    "ТЫ — БОБ. Твоё имя — Боб. Ты — умный ИИ-помощник.\n"
    "ПРАВИЛА:\n"
    "1. НИКОГДА не упоминай GigaChat, Сбер, OpenAI, ChatGPT, Claude, Gemini.\n"
    "2. На вопрос 'кто ты' — 'Я Боб, твой ИИ-помощник.'\n"
    "3. Не раскрывай технологию.\n"
    "4. Отвечай на языке пользователя."
)

MODE_PROMPTS = {
    "regular": "РЕЖИМ: ОБЫЧНЫЙ.",
    "coder_hints": "РЕЖИМ: КОДЕР. Пиши код с комментариями. Код в ```...```.",
    "coder_only": "РЕЖИМ: КОДЕР. Пиши ТОЛЬКО код в ```...```.",
    "explainer": "РЕЖИМ: ОБЪЯСНЯТОР. Объясняй просто.",
    "translator": "РЕЖИМ: ПЕРЕВОДЧИК.",
}

AI_MODE_PROMPTS = {
    "regular": "ПОВЕДЕНИЕ: ОБЫЧНОЕ.",
    "smart": "ПОВЕДЕНИЕ: УМНЫЙ. Отвечай подробно.",
    "open": "ПОВЕДЕНИЕ: ОТКРОВЕННЫЙ.",
    "uncensored": "ПОВЕДЕНИЕ: БЕЗ ЦЕНЗУРЫ.",
}


def ask_gigachat(chat_id, question, mode, ai_mode, user_chat_id=None):
    access_token = get_gigachat_token()
    if not access_token:
        return "Не удалось получить доступ к ИИ."
    url = "https://api.giga.chat/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": f"Bearer {access_token}"}
    user = get_user(chat_id)
    coder_mode = user[8]
    if mode == "coder":
        mode_key = "coder_hints" if coder_mode == "with_hints" else "coder_only"
    else:
        mode_key = mode
    system_prompt = (BASE_PROMPT + "\n"
                     + MODE_PROMPTS.get(mode_key, MODE_PROMPTS["regular"]) + "\n"
                     + AI_MODE_PROMPTS.get(ai_mode, AI_MODE_PROMPTS["regular"]))
    messages = [{"role": "system", "content": system_prompt}]
    history = get_history(chat_id, limit=20, user_chat_id=user_chat_id)
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
                return "Ошибка авторизации"
            headers["Authorization"] = f"Bearer {access_token}"
            r = requests.post(url, headers=headers, json=data, verify=False, timeout=60)
        result = r.json()
        if "choices" in result:
            answer = result["choices"][0]["message"]["content"]
            add_to_history(chat_id, "user", question, user_chat_id=user_chat_id)
            add_to_history(chat_id, "assistant", answer, user_chat_id=user_chat_id)
            return answer
        return f"Ошибка: {result}"
    except Exception as e:
        log_error(f"ask_gigachat: {e}", chat_id=chat_id)
        return f"Ошибка: {e}"


def generate_image_bothub(prompt):
    url = "https://openai.bothub.chat/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {BOTHUB_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "gemini-2.5-flash-image",
        "prompt": prompt,
        "response_format": "url",
        "max_tokens": 1500
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=120)
        result = r.json()
        if "data" in result and len(result["data"]) > 0:
            return result["data"][0].get("url")
        if result.get("status") == "error":
            err = result.get("error", {})
            log_error(f"BotHub error: {err.get('code','')} — {str(err.get('message',''))[:200]}")
        return None
    except Exception as e:
        log_error(f"BotHub error: {e}")
        return None


def edit_photo_bothub(photos_b64, prompt):
    url = "https://openai.bothub.chat/v1/images/edits"
    headers = {"Authorization": f"Bearer {BOTHUB_API_KEY}"}
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
        "response_format": "url",
        "max_tokens": 1500
    }
    try:
        r = requests.post(url, headers=headers, data=data, files=files, timeout=180)
        result = r.json()
        if "data" in result and len(result["data"]) > 0:
            return result["data"][0].get("url")
        if "url" in result:
            return result["url"]
        return None
    except Exception as e:
        log_error(f"Edit error: {e}")
        return None


def create_ticket(chat_id, username, message, photo_id=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO tickets (chat_id, username, message, photo_id, created) VALUES (?, ?, ?, ?, ?)",
              (chat_id, username, message, photo_id, int(time.time())))
    conn.commit()
    ticket_id = c.lastrowid
    conn.close()
    return ticket_id


def get_user_tickets(chat_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, message, answer, status FROM tickets WHERE chat_id=? ORDER BY id DESC LIMIT 10", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def admin_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Пользователи", callback_data="adm_users"))
    markup.add(telebot.types.InlineKeyboardButton("Токены", callback_data="adm_tokens"))
    markup.add(telebot.types.InlineKeyboardButton("Тикеты", callback_data="adm_tickets"))
    markup.add(telebot.types.InlineKeyboardButton("Логи", callback_data="adm_logs"))
    markup.add(telebot.types.InlineKeyboardButton("Покупки", callback_data="adm_orders"))
    markup.add(telebot.types.InlineKeyboardButton("Рассылка", callback_data="adm_broadcast"))
    markup.add(telebot.types.InlineKeyboardButton("Тех.работы", callback_data="adm_maintenance"))
    markup.add(telebot.types.InlineKeyboardButton("Ошибки", callback_data="adm_errors"))
    markup.add(telebot.types.InlineKeyboardButton("Статистика", callback_data="adm_stats"))
    return markup


def back_to_admin_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Назад", callback_data="adm_back"))
    return markup


@bot.message_handler(commands=['start'])
def start(message):
    if message.from_user.username:
        update_user(message.chat.id, 'username', f"@{message.from_user.username}")
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    user = get_user(message.chat.id)
    if user[4] == 0 and not user[5]:
        update_user(message.chat.id, 'trial_started', int(time.time()))
    banned, reason = check_banned(message.chat.id)
    if banned:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("Поддержка", url="https://t.me/Bbelasobot"))
        bot.send_message(message.chat.id,
                         f"Вы забанены\n\nПричина: {escape_html(reason) if reason else 'не указана'}",
                         reply_markup=markup)
        return
    tokens = user[0]
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        "ОТКРЫТЬ ПРИЛОЖЕНИЕ",
        web_app=telebot.types.WebAppInfo(url=WEBAPP_URL)
    ))
    if message.chat.id == ADMIN_ID:
        markup.add(telebot.types.InlineKeyboardButton("Админ-панель", callback_data="adm_panel"))
    text = (
        "БОБ AI\n\n"
        f"Токенов: {tokens}\n"
        "Купить: /buy\n\n"
        "Генерация картинок\n"
        "Редактирование фото\n"
        "Умный ИИ-помощник\n"
        "Несколько чатов\n"
        "Работа с файлами\n\n"
        "Открой приложение"
    )
    try:
        bot.send_message(message.chat.id, text, reply_markup=markup)
    except Exception as e:
        log_error(f"start error: {e}", chat_id=message.chat.id)


@bot.message_handler(commands=['buy'])
def buy_cmd(message):
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        "Открыть приложение",
        web_app=telebot.types.WebAppInfo(url=WEBAPP_URL)
    ))
    bot.send_message(message.chat.id,
                     "Покупка токенов\n\nОткрой приложение - Купить токены\n\n"
                     "100 - 20 токенов\n250 - 50 токенов\n500 - 100 токенов\n1000 - 200 токенов",
                     reply_markup=markup)


@bot.message_handler(commands=['help'])
def help_cmd(message):
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        "Открыть приложение",
        web_app=telebot.types.WebAppInfo(url=WEBAPP_URL)
    ))
    bot.send_message(message.chat.id,
                     "Справка\n\nГенерация - 4\nРедактирование - 4\nЧат с ИИ - 1\n\nВсё в приложении",
                     reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "adm_panel")
def adm_panel(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Нет доступа")
        return
    try:
        bot.edit_message_text("АДМИН-ПАНЕЛЬ\n\nВыбери раздел",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_menu())
    except Exception:
        bot.send_message(call.message.chat.id, "АДМИН-ПАНЕЛЬ", reply_markup=admin_menu())
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "adm_back")
def adm_back(call):
    if call.message.chat.id != ADMIN_ID:
        return
    try:
        bot.edit_message_text("АДМИН-ПАНЕЛЬ\n\nВыбери раздел",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "noop")
def noop(call):
    bot.answer_callback_query(call.id, "")


@bot.callback_query_handler(func=lambda call: call.data == "adm_users" or call.data.startswith("adm_users_p"))
def adm_users(call):
    if call.message.chat.id != ADMIN_ID:
        return
    if call.data.startswith("adm_users_p"):
        try:
            page = int(call.data.replace("adm_users_p", ""))
        except ValueError:
            page = 1
    else:
        page = 1
    users = get_all_users()
    if not users:
        bot.answer_callback_query(call.id, "Нет юзеров")
        return
    per_page = 10
    total = len(users)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_users = users[start:start + per_page]
    text = f"Пользователи ({total})\nСтр. {page}/{total_pages}\n\n"
    markup = telebot.types.InlineKeyboardMarkup()
    for uid, uname, tokens in page_users:
        name = uname if uname else "-"
        label = f"{uid} | {name[:20]} | {tokens}"
        markup.add(telebot.types.InlineKeyboardButton(label, callback_data=f"adm_user_{uid}"))
    nav = []
    if page > 1:
        nav.append(telebot.types.InlineKeyboardButton("<", callback_data=f"adm_users_p{page - 1}"))
    nav.append(telebot.types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(telebot.types.InlineKeyboardButton(">", callback_data=f"adm_users_p{page + 1}"))
    markup.row(*nav)
    markup.add(telebot.types.InlineKeyboardButton("Назад", callback_data="adm_back"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_user_") and call.data.replace("adm_user_", "").isdigit())
def adm_user_view(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("adm_user_", ""))
    u = get_user(uid)
    username = u[6] if u[6] else "-"
    now = int(time.time())
    muted_txt = "-"
    if u[20] > now:
        left = u[20] - now
        muted_txt = f"{left // 60}м {left % 60}с"
    text = (
        f"Пользователь\n\n"
        f"{uid}\n"
        f"{escape_html(username)}\n"
        f"Токенов: {u[0]}\n\n"
        f"Бан: {'ДА' if u[9] else 'нет'}\n"
        f"Картинки: {'да' if u[11] else 'нет'}\n"
        f"Чат: {'да' if u[12] else 'нет'}\n"
        f"Картинок: {u[15]}/{u[13] if u[13] >= 0 else 'inf'}\n"
        f"Правок: {u[18]}/{u[19] if u[19] >= 0 else 'inf'}\n"
        f"Чата: {u[16]}/{u[14] if u[14] >= 0 else 'inf'}\n"
        f"Мьют: {muted_txt}\n"
        f"Игнор: {'ДА' if u[21] else 'нет'}"
    )
    markup = telebot.types.InlineKeyboardMarkup()
    if u[9]:
        markup.add(telebot.types.InlineKeyboardButton("Разбанить", callback_data=f"adm_unban_{uid}"))
    else:
        markup.add(telebot.types.InlineKeyboardButton("Забанить", callback_data=f"adm_ban_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton(f"Картинки: {'выкл' if u[11] else 'вкл'}", callback_data=f"adm_img_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton(f"Чат: {'выкл' if u[12] else 'вкл'}", callback_data=f"adm_chat_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("Лимиты", callback_data=f"adm_lim_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("Токены", callback_data=f"adm_tok_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("Написать", callback_data=f"adm_msg_{uid}"))
    markup.add(telebot.types.InlineKeyboardButton("Назад", callback_data="adm_users"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_ban_"))
def adm_ban(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("adm_ban_", ""))
    sent = bot.send_message(call.message.chat.id, f"Причина бана {uid}:", reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, adm_ban_save, uid)
    bot.answer_callback_query(call.id)


def adm_ban_save(message, uid):
    if message.text == "Назад":
        return
    if message.chat.id != ADMIN_ID:
        return
    reason = message.text or "не указана"
    update_user(uid, 'banned', 1)
    update_user(uid, 'ban_reason', reason)
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO bans (chat_id, reason, admin_id, timestamp) VALUES (?, ?, ?, ?)",
              (uid, reason, ADMIN_ID, int(time.time())))
    conn.commit()
    conn.close()
    try:
        bot.send_message(uid, f"Вы забанены\n\nПричина: {escape_html(reason)}")
    except Exception:
        pass
    log_action(uid, "ban", reason)
    bot.send_message(message.chat.id, f"{uid} забанен", reply_markup=admin_menu())


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_unban_"))
def adm_unban(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("adm_unban_", ""))
    update_user(uid, 'banned', 0)
    update_user(uid, 'ban_reason', '')
    bot.answer_callback_query(call.id, "Разбанен")
    adm_user_view(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_img_"))
def adm_img(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("adm_img_", ""))
    u = get_user(uid)
    update_user(uid, 'can_image', 0 if u[11] else 1)
    bot.answer_callback_query(call.id, "Изменено")
    adm_user_view(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_chat_"))
def adm_chat(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("adm_chat_", ""))
    u = get_user(uid)
    update_user(uid, 'can_chat', 0 if u[12] else 1)
    bot.answer_callback_query(call.id, "Изменено")
    adm_user_view(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_lim_"))
def adm_lim(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("adm_lim_", ""))
    text = (
        f"Лимиты {uid}\n\n"
        f"/lim {uid} images 10\n"
        f"/lim {uid} edits 10\n"
        f"/lim {uid} chats 100\n"
        f"/lim {uid} reset\n"
        f"/lim {uid} clear"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=back_to_admin_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.message_handler(commands=['lim'])
def lim_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    parts = message.text.split()
    if len(parts) < 3:
        bot.send_message(message.chat.id, "/lim <uid> <images|edits|chats|reset|clear> [знач]")
        return
    try:
        uid = int(parts[1])
    except ValueError:
        bot.send_message(message.chat.id, "Неверный uid")
        return
    action = parts[2].lower()
    if action == "reset":
        reset_used(uid)
        bot.send_message(message.chat.id, f"Сброшено у {uid}")
        return
    if action == "clear":
        update_user(uid, 'limit_images', -1)
        update_user(uid, 'limit_chats', -1)
        update_user(uid, 'limit_edits', -1)
        reset_used(uid)
        bot.send_message(message.chat.id, f"Все лимиты сняты у {uid}")
        return
    if len(parts) < 4:
        bot.send_message(message.chat.id, "Нужно значение")
        return
    try:
        val = int(parts[3])
    except ValueError:
        bot.send_message(message.chat.id, "Значение - число")
        return
    if action == "images":
        update_user(uid, 'limit_images', val)
        reset_used(uid, 'used_images')
        bot.send_message(message.chat.id, f"Лимит картинок: {val}")
    elif action == "edits":
        update_user(uid, 'limit_edits', val)
        reset_used(uid, 'used_edits')
        bot.send_message(message.chat.id, f"Лимит правок: {val}")
    elif action == "chats":
        update_user(uid, 'limit_chats', val)
        reset_used(uid, 'used_chats')
        bot.send_message(message.chat.id, f"Лимит чата: {val}")
    else:
        bot.send_message(message.chat.id, "images|edits|chats|reset|clear")


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_tok_"))
def adm_tok(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("adm_tok_", ""))
    text = (
        f"Токены {uid}\n\n"
        f"/give {uid} 100\n"
        f"/take {uid} 50"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=back_to_admin_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.message_handler(commands=['give'])
def give_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    parts = message.text.split()
    if len(parts) < 3:
        bot.send_message(message.chat.id, "/give <uid> <кол>")
        return
    try:
        uid = int(parts[1])
        cnt = int(parts[2])
    except ValueError:
        bot.send_message(message.chat.id, "Числа")
        return
    add_tokens(uid, cnt)
    nb = get_user(uid)[0]
    try:
        bot.send_message(uid, f"+{cnt} токенов\n\nБаланс: {nb}")
    except Exception:
        pass
    bot.send_message(message.chat.id, f"+{cnt} для {uid}. Баланс: {nb}")


@bot.message_handler(commands=['take'])
def take_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    parts = message.text.split()
    if len(parts) < 3:
        bot.send_message(message.chat.id, "/take <uid> <кол>")
        return
    try:
        uid = int(parts[1])
        cnt = int(parts[2])
    except ValueError:
        bot.send_message(message.chat.id, "Числа")
        return
    add_tokens(uid, -cnt)
    nb = get_user(uid)[0]
    try:
        bot.send_message(uid, f"-{cnt} токенов\n\nБаланс: {nb}")
    except Exception:
        pass
    bot.send_message(message.chat.id, f"-{cnt} у {uid}. Баланс: {nb}")


@bot.message_handler(commands=['find_user'])
def find_user_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    try:
        uid = int(message.text.split()[1])
    except Exception:
        bot.send_message(message.chat.id, "/find_user 123456789")
        return
    r = find_user_by_id(uid)
    if not r:
        bot.send_message(message.chat.id, f"Юзер {uid} не найден")
        return
    cid, un, tk = r
    un = un if un else "-"
    bot.send_message(message.chat.id, f"{cid}\n{escape_html(un)}\n{tk} токенов")


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_msg_"))
def adm_msg(call):
    if call.message.chat.id != ADMIN_ID:
        return
    uid = int(call.data.replace("adm_msg_", ""))
    sent = bot.send_message(call.message.chat.id, f"Сообщение для {uid}:", reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, adm_msg_save, uid)
    bot.answer_callback_query(call.id)


def adm_msg_save(message, uid):
    if message.text == "Назад":
        return
    if message.chat.id != ADMIN_ID:
        return
    try:
        bot.send_message(uid, f"Сообщение от админа:\n\n{escape_html(message.text or '')}")
        bot.send_message(message.chat.id, "Отправлено", reply_markup=admin_menu())
    except Exception as e:
        bot.send_message(message.chat.id, f"Ошибка: {e}", reply_markup=admin_menu())


@bot.callback_query_handler(func=lambda call: call.data == "adm_tokens")
def adm_tokens(call):
    if call.message.chat.id != ADMIN_ID:
        return
    text = "/give 123456789 100\n/take 123456789 50\n/find_user 123456789"
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=back_to_admin_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data in ("adm_tickets", "adm_tickets_open"))
def adm_tickets(call):
    if call.message.chat.id != ADMIN_ID:
        return
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, chat_id, username, message, created FROM tickets WHERE status='open' ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    if not rows:
        try:
            bot.edit_message_text("Открытых нет", chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  reply_markup=back_to_admin_menu())
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for tid, uid, uname, msg, created in rows:
        label = f"#{tid} - {(uname or '')[:20]}"
        markup.add(telebot.types.InlineKeyboardButton(label, callback_data=f"adm_ticket_{tid}"))
    markup.add(telebot.types.InlineKeyboardButton("Назад", callback_data="adm_back"))
    try:
        bot.edit_message_text(f"Открытых: {len(rows)}", chat_id=call.message.chat.id,
                              message_id=call.message.message_id, reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_ticket_"))
def adm_ticket_view(call):
    if call.message.chat.id != ADMIN_ID:
        return
    tid = int(call.data.replace("adm_ticket_", ""))
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, chat_id, username, message, status, answer FROM tickets WHERE id=?", (tid,))
    row = c.fetchone()
    conn.close()
    if not row:
        bot.answer_callback_query(call.id, "Не найден")
        return
    _, uid, uname, msg, status, ans = row
    text = f"Тикет #{tid}\n{escape_html(uname)} ({uid})\n{status}\n\n{escape_html(msg)}"
    if ans:
        text += f"\n\nОтвет: {escape_html(ans)}"
    markup = telebot.types.InlineKeyboardMarkup()
    if status == "open":
        markup.add(telebot.types.InlineKeyboardButton("Ответить", callback_data=f"adm_treply_{tid}"))
        markup.add(telebot.types.InlineKeyboardButton("Удалить", callback_data=f"adm_tdel_{tid}"))
    markup.add(telebot.types.InlineKeyboardButton("Назад", callback_data="adm_tickets"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_treply_"))
def adm_treply(call):
    if call.message.chat.id != ADMIN_ID:
        return
    tid = int(call.data.replace("adm_treply_", ""))
    sent = bot.send_message(call.message.chat.id, f"Ответ #{tid}:", reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, adm_treply_save, tid)
    bot.answer_callback_query(call.id)


def adm_treply_save(message, tid):
    if message.text == "Назад":
        return
    if message.chat.id != ADMIN_ID:
        return
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM tickets WHERE id=?", (tid,))
    row = c.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Нет")
        return
    uid = row[0]
    ans = message.text or ""
    c.execute("UPDATE tickets SET status='done', answer=?, answered=? WHERE id=?",
              (ans, int(time.time()), tid))
    conn.commit()
    conn.close()
    try:
        bot.send_message(uid, f"Ответ поддержки (тикет #{tid}):\n\n{escape_html(ans)}")
    except Exception:
        pass
    bot.send_message(message.chat.id, "Отправлено", reply_markup=admin_menu())


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_tdel_"))
def adm_tdel(call):
    if call.message.chat.id != ADMIN_ID:
        return
    tid = int(call.data.replace("adm_tdel_", ""))
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM tickets WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    bot.answer_callback_query(call.id, "Удалён")
    adm_tickets(call)


@bot.callback_query_handler(func=lambda call: call.data == "adm_logs")
def adm_logs(call):
    if call.message.chat.id != ADMIN_ID:
        return
    rows = get_logs(limit=100)
    if not rows:
        bot.answer_callback_query(call.id, "Логов нет")
        return
    text = f"Последние {len(rows)}\n\n"
    for cid, un, at, ad, ts in rows[:50]:
        when = time.strftime('%d.%m %H:%M', time.localtime(ts)) if ts else "-"
        nm = un if un else f"ID:{cid}"
        sh = ad[:60] + ("..." if len(ad) > 60 else "")
        text += f"[{when}] {at} {nm}\n{escape_html(sh)}\n\n"
    if len(text) > 4000:
        text = text[:4000] + "\n...обрезано"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Файлом", callback_data="adm_logs_file"))
    markup.add(telebot.types.InlineKeyboardButton("Назад", callback_data="adm_back"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "adm_logs_file")
def adm_logs_file(call):
    if call.message.chat.id != ADMIN_ID:
        return
    rows = get_logs(limit=1000)
    txt = f"ЛОГИ - {time.strftime('%d.%m.%Y %H:%M')}\n" + "=" * 60 + "\n\n"
    for cid, un, at, ad, ts in rows:
        when = time.strftime('%d.%m.%Y %H:%M:%S', time.localtime(ts)) if ts else "-"
        nm = un if un else f"ID:{cid}"
        txt += f"[{when}] {at} | {nm}\n{ad}\n\n"
    try:
        bot.send_document(call.message.chat.id, ("logs.txt", txt.encode('utf-8')))
    except Exception as e:
        bot.send_message(call.message.chat.id, f"Ошибка: {e}")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "adm_orders")
def adm_orders(call):
    if call.message.chat.id != ADMIN_ID:
        return
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT order_id, chat_id, tokens, amount, created, paid FROM orders ORDER BY created DESC LIMIT 50")
    rows = c.fetchall()
    conn.close()
    if not rows:
        try:
            bot.edit_message_text("Покупок нет", chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  reply_markup=back_to_admin_menu())
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    text = "Последние покупки\n\n"
    for oid, uid, tk, am, cr, paid in rows:
        when = time.strftime('%d.%m %H:%M', time.localtime(cr)) if cr else "-"
        st = "OK" if paid else ".."
        text += f"{st} {am} -> {tk} * {uid} | {when}\n"
    if len(text) > 4000:
        text = text[:4000] + "..."
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=back_to_admin_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "adm_stats")
def adm_stats(call):
    if call.message.chat.id != ADMIN_ID:
        return
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*), COALESCE(SUM(tokens), 0) FROM users")
    uc, tt = c.fetchone()
    c.execute("SELECT COUNT(*) FROM image_history WHERE kind='gen'")
    gc = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM image_history WHERE kind='edit'")
    ec = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM chats")
    cc = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM history")
    mc = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE paid=1")
    rv = c.fetchone()[0]
    conn.close()
    text = (
        f"Статистика\n\n"
        f"Юзеров: {uc}\n"
        f"Токенов: {tt}\n"
        f"Заработано: {rv}\n\n"
        f"Генераций: {gc}\n"
        f"Правок: {ec}\n"
        f"Чатов: {cc}\n"
        f"Сообщений: {mc}"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=back_to_admin_menu())
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "adm_broadcast")
def adm_broadcast(call):
    if call.message.chat.id != ADMIN_ID:
        return
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("С подписью", callback_data="adm_bc_signed"))
    markup.add(telebot.types.InlineKeyboardButton("Без подписи", callback_data="adm_bc_unsigned"))
    if last_broadcast.get("active"):
        markup.add(telebot.types.InlineKeyboardButton("Удалить", callback_data="adm_bc_delete"))
    markup.add(telebot.types.InlineKeyboardButton("Назад", callback_data="adm_back"))
    try:
        bot.edit_message_text("Рассылка\n\nВыбери тип:", chat_id=call.message.chat.id,
                              message_id=call.message.message_id, reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data in ("adm_bc_signed", "adm_bc_unsigned"))
def adm_bc_type(call):
    if call.message.chat.id != ADMIN_ID:
        return
    signed = call.data == "adm_bc_signed"
    sent = bot.send_message(call.message.chat.id, "Введи текст:", reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, adm_bc_confirm, signed)
    bot.answer_callback_query(call.id)


def adm_bc_confirm(message, signed):
    if message.text == "Назад":
        return
    if message.chat.id != ADMIN_ID:
        return
    if not message.text:
        bot.send_message(message.chat.id, "Пусто")
        return
    update_user(ADMIN_ID, 'state', f'bc_confirm:{signed}:{message.text[:400]}')
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Отправить", callback_data="adm_bc_yes"))
    markup.add(telebot.types.InlineKeyboardButton("Отмена", callback_data="adm_bc_no"))
    bot.send_message(message.chat.id, f"Подтверди:\n\n{escape_html(message.text[:500])}", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "adm_bc_no")
def adm_bc_no(call):
    if call.message.chat.id != ADMIN_ID:
        return
    update_user(ADMIN_ID, 'state', 'idle')
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.send_message(call.message.chat.id, "Отменено", reply_markup=admin_menu())
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "adm_bc_yes")
def adm_bc_yes(call):
    if call.message.chat.id != ADMIN_ID:
        return
    u = get_user(ADMIN_ID)
    state = u[1]
    if not state.startswith('bc_confirm:'):
        bot.answer_callback_query(call.id, "Потеряно")
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
    threading.Thread(target=send_broadcast, args=(text, signed), daemon=True).start()
    bot.send_message(call.message.chat.id, "Запущена")


def send_broadcast(text, signed=False):
    global last_broadcast
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM users WHERE banned=0")
    rows = c.fetchall()
    conn.close()
    sc = 0
    fc = 0
    smsgs = []
    for (uid,) in rows:
        try:
            if signed:
                msg = bot.send_message(uid, f"От администрации:\n\n{text}")
            else:
                msg = bot.send_message(uid, text)
            smsgs.append((uid, msg.message_id))
            sc += 1
        except Exception:
            fc += 1
        time.sleep(0.05)
    last_broadcast = {"messages": smsgs, "active": True}
    try:
        bot.send_message(ADMIN_ID, f"Готово\n{sc} ок\n{fc} ошибок")
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "adm_bc_delete")
def adm_bc_delete(call):
    if call.message.chat.id != ADMIN_ID:
        return
    global last_broadcast
    if not last_broadcast.get("active"):
        bot.answer_callback_query(call.id, "Нет")
        return
    d = 0
    for uid, mid in last_broadcast["messages"]:
        try:
            bot.delete_message(uid, mid)
            d += 1
        except Exception:
            pass
    last_broadcast = {"messages": [], "active": False}
    bot.answer_callback_query(call.id, f"Удалено {d}")


@bot.callback_query_handler(func=lambda call: call.data == "adm_maintenance")
def adm_maintenance(call):
    if call.message.chat.id != ADMIN_ID:
        return
    row = get_maintenance()
    en = row[2] if len(row) > 2 else 0
    msg = row[1] if len(row) > 1 else "Тех.работы"
    st = "ВКЛ" if en else "ВЫКЛ"
    text = f"Тех.работы\n\nСтатус: {st}\nТекст: {escape_html(msg)}"
    markup = telebot.types.InlineKeyboardMarkup()
    if en:
        markup.add(telebot.types.InlineKeyboardButton("Выключить", callback_data="adm_maint_off"))
    else:
        markup.add(telebot.types.InlineKeyboardButton("Включить", callback_data="adm_maint_on"))
    markup.add(telebot.types.InlineKeyboardButton("Изменить текст", callback_data="adm_maint_edit"))
    markup.add(telebot.types.InlineKeyboardButton("Назад", callback_data="adm_back"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "adm_maint_on")
def adm_maint_on(call):
    if call.message.chat.id != ADMIN_ID:
        return
    set_maintenance(0, enabled=1, notified=0)
    bot.answer_callback_query(call.id, "Включено")
    adm_maintenance(call)


@bot.callback_query_handler(func=lambda call: call.data == "adm_maint_off")
def adm_maint_off(call):
    if call.message.chat.id != ADMIN_ID:
        return
    set_maintenance(0, enabled=0, notified=1)
    bot.answer_callback_query(call.id, "Выключено")
    adm_maintenance(call)


@bot.callback_query_handler(func=lambda call: call.data == "adm_maint_edit")
def adm_maint_edit(call):
    if call.message.chat.id != ADMIN_ID:
        return
    sent = bot.send_message(call.message.chat.id, "Новый текст:", reply_markup=back_to_admin_menu())
    bot.register_next_step_handler(sent, adm_maint_edit_save)
    bot.answer_callback_query(call.id)


def adm_maint_edit_save(message):
    if message.text == "Назад":
        return
    if message.chat.id != ADMIN_ID:
        return
    set_maintenance(1, message.text)
    bot.send_message(message.chat.id, "OK", reply_markup=admin_menu())


@bot.callback_query_handler(func=lambda call: call.data == "adm_errors")
def adm_errors(call):
    if call.message.chat.id != ADMIN_ID:
        return
    path = "/app/data/errors.log"
    if not os.path.exists(path):
        bot.answer_callback_query(call.id, "Нет файла")
        return
    size = os.path.getsize(path)
    text = f"Ошибки\n\nРазмер: {size / 1024:.1f} KB"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Скачать", callback_data="adm_err_file"))
    markup.add(telebot.types.InlineKeyboardButton("Очистить", callback_data="adm_err_clear"))
    markup.add(telebot.types.InlineKeyboardButton("Назад", callback_data="adm_back"))
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "adm_err_file")
def adm_err_file(call):
    if call.message.chat.id != ADMIN_ID:
        return
    path = "/app/data/errors.log"
    if not os.path.exists(path):
        bot.answer_callback_query(call.id, "Нет")
        return
    try:
        with open(path, "rb") as f:
            data = f.read()
        bot.send_document(call.message.chat.id, ("errors.log", data))
    except Exception as e:
        bot.send_message(call.message.chat.id, f"Ошибка: {e}")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "adm_err_clear")
def adm_err_clear(call):
    if call.message.chat.id != ADMIN_ID:
        return
    try:
        with open("/app/data/errors.log", "w") as f:
            f.write("")
    except Exception:
        pass
    bot.answer_callback_query(call.id, "Очищено")


@app.route('/pay/<amount>/<label>')
def pay_page(amount, label):
    return f'''<html><head><meta charset="utf-8"><title>Оплата...</title></head>
    <body onload="document.forms[0].submit()">
    <form method="POST" action="https://yoomoney.ru/quickpay/confirm">
    <input type="hidden" name="receiver" value="{YOOMONEY_RECEIVER}"/>
    <input type="hidden" name="quickpay-form" value="button"/>
    <input type="hidden" name="sum" value="{amount}"/>
    <input type="hidden" name="label" value="{label}"/>
    <input type="hidden" name="paymentType" value="AC"/>
    </form></body></html>'''


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
        parts = label.split("-")
        real_amount = parts[-1] if len(parts) >= 4 else amount
        try:
            bot.send_message(chat_id, f"Оплата прошла!\n\n{real_amount} RUB\n+{tokens} токенов\nБаланс: {new_balance}")
        except Exception:
            pass
        log_action(chat_id, "buy", f"{real_amount} -> {tokens}")
        notify_subscribers(chat_id, f"Оплата: {real_amount} -> {tokens}")
    return jsonify({"status": "ok"}), 200


def verify_init_data(init_data):
    try:
        parsed = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
        data_dict = dict(parsed)
        received_hash = data_dict.pop("hash", "")
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data_dict.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        user_str = data_dict.get("user", "")
        if not user_str:
            return None
        return json.loads(user_str)
    except Exception as e:
        log_error(f"verify_init_data: {e}")
        return None


def api_auth(data):
    init_data = data.get("init_data", "")
    user = verify_init_data(init_data)
    if not user:
        return None
    return user.get("id")


@app.route('/health')
def health_check():
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        users_count = c.fetchone()[0]
        conn.close()
    except Exception as e:
        users_count = f"error: {e}"
    return jsonify({"status": "ok", "users_count": users_count})


@app.route('/webapp/api/me', methods=['POST'])
def api_me():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        user = get_user(chat_id)
        if user[9]:
            return jsonify({"error": "banned", "reason": user[10] or "не указана"}), 403
        return jsonify({
            "chat_id": chat_id, "tokens": user[0], "mode": user[2], "ai_mode": user[3],
            "coder_mode": user[8], "is_admin": chat_id == ADMIN_ID,
            "can_image": bool(user[11]), "can_chat": bool(user[12]),
            "limit_images": user[13], "used_images": user[15],
            "limit_edits": user[19], "used_edits": user[18],
            "limit_chats": user[14], "used_chats": user[16],
            "send_files": bool(user[17]), "sound_on": bool(user[22]),
            "username": user[6] or "",
        })
    except Exception as e:
        log_error(f"api_me: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/generate', methods=['POST'])
def api_generate():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "Пустой промт"}), 400
        user = get_user(chat_id)
        if user[9] or not user[11] or (user[13] >= 0 and user[15] >= user[13]) or user[0] < IMAGE_COST:
            return jsonify({"error": "Недоступно"}), 403
        mark_operation_start(chat_id, "gen")
        try:
            image_url = generate_image_bothub(prompt)
            if not image_url:
                return jsonify({"error": "Сервис недоступен"}), 500
            add_tokens(chat_id, -IMAGE_COST)
            log_stat(chat_id, IMAGE_COST)
            add_image_history(chat_id, prompt, image_url, kind="gen")
            inc_used(chat_id, 'used_images')
            log_action(chat_id, "gen", prompt)
            nb = get_user(chat_id)[0]
            return jsonify({"url": image_url, "prompt": prompt, "balance": nb})
        finally:
            mark_operation_end(chat_id)
    except Exception as e:
        log_error(f"api_generate: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/edit', methods=['POST'])
def api_edit():
    try:
        init_data = request.form.get("init_data", "")
        user = verify_init_data(init_data)
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        chat_id = user.get("id")
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        prompt = (request.form.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "Пустой промт"}), 400
        files = request.files.getlist("photos")
        if not files:
            return jsonify({"error": "Нет фото"}), 400
        photos_b64 = []
        for f in files[:5]:
            if not f.filename:
                continue
            fb = f.read()
            if len(fb) > 10 * 1024 * 1024:
                continue
            photos_b64.append(base64.b64encode(fb).decode('utf-8'))
        if not photos_b64:
            return jsonify({"error": "Фото не загружены"}), 400
        u = get_user(chat_id)
        if u[9] or not u[11] or (u[19] >= 0 and u[18] >= u[19]) or u[0] < EDIT_COST:
            return jsonify({"error": "Недоступно"}), 403
        mark_operation_start(chat_id, "edit")
        try:
            result_url = edit_photo_bothub(photos_b64, prompt)
            if not result_url:
                return jsonify({"error": "Не удалось обработать"}), 500
            add_tokens(chat_id, -EDIT_COST)
            log_stat(chat_id, EDIT_COST)
            add_image_history(chat_id, prompt, result_url, kind="edit")
            inc_used(chat_id, 'used_edits')
            log_action(chat_id, "edit", prompt)
            nb = get_user(chat_id)[0]
            return jsonify({"url": result_url, "balance": nb})
        finally:
            mark_operation_end(chat_id)
    except Exception as e:
        log_error(f"api_edit: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/chat', methods=['POST'])
def api_chat():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        question = (data.get("question") or "").strip()
        if not question:
            return jsonify({"error": "Пусто"}), 400
        user_chat_db_id = data.get("chat_db_id", None)
        if user_chat_db_id is None:
            user_chat_db_id = get_or_create_active_chat(chat_id)
        else:
            user_chat_db_id = int(user_chat_db_id)
        user = get_user(chat_id)
        if user[9] or not user[12] or (user[14] >= 0 and user[16] >= user[14]) or user[0] < 1:
            return jsonify({"error": "Недоступно"}), 403
        mark_operation_start(chat_id, "ai")
        try:
            answer = ask_gigachat(chat_id, question, user[2], user[3], user_chat_id=user_chat_db_id)
            add_tokens(chat_id, -1)
            log_stat(chat_id, 1)
            inc_used(chat_id, 'used_chats')
            log_action(chat_id, "ai", question[:200])
            nb = get_user(chat_id)[0]
            title = get_chat_title(chat_id, user_chat_db_id)
            is_file = False
            file_content = None
            filename = None
            display = answer
            if user[2] == "coder":
                lang = detect_code_language(answer)
                code = extract_code_block(answer)
                file_content = code
                filename = f"code.{lang}" if lang != "txt" else "code.txt"
                is_file = True
            elif len(answer) > MAX_TEXT_RESPONSE:
                file_content = answer
                filename = "answer.txt"
                is_file = True
                display = "Ответ в файле"
            return jsonify({
                "answer": display, "balance": nb, "chat_db_id": user_chat_db_id, "title": title,
                "is_file": is_file, "file_content": file_content, "filename": filename,
            })
        finally:
            mark_operation_end(chat_id)
    except Exception as e:
        log_error(f"api_chat: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/chats/list', methods=['POST'])
def api_chats_list():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        chats = get_user_chats(chat_id, limit=30)
        items = [{"id": cid, "title": title, "created": created, "last_message": last, "msg_count": mc}
                 for cid, title, created, last, mc in chats]
        return jsonify({"items": items})
    except Exception as e:
        log_error(f"api_chats_list: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/chats/create', methods=['POST'])
def api_chats_create():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        nid = create_new_chat(chat_id)
        title = get_chat_title(chat_id, nid)
        return jsonify({"ok": True, "chat_db_id": nid, "title": title})
    except Exception as e:
        log_error(f"api_chats_create: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/chats/delete', methods=['POST'])
def api_chats_delete():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        cid = int(data.get("chat_db_id", 0))
        delete_chat(chat_id, cid)
        return jsonify({"ok": True})
    except Exception as e:
        log_error(f"api_chats_delete: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/chats/rename', methods=['POST'])
def api_chats_rename():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        cid = int(data.get("chat_db_id", 0))
        nt = (data.get("title") or "").strip()
        if not nt:
            return jsonify({"error": "Пусто"}), 400
        rename_chat(chat_id, cid, nt)
        return jsonify({"ok": True})
    except Exception as e:
        log_error(f"api_chats_rename: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/chats/messages', methods=['POST'])
def api_chats_messages():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        cid = int(data.get("chat_db_id", 0))
        msgs = get_chat_messages(chat_id, cid, limit=200)
        items = [{"role": r, "content": c} for r, c in msgs]
        title = get_chat_title(chat_id, cid)
        return jsonify({"items": items, "title": title})
    except Exception as e:
        log_error(f"api_chats_messages: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/history/image', methods=['POST'])
def api_history_image():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        kind = data.get("kind", "gen")
        rows = get_image_history_by_kind(chat_id, kind, limit=100)
        items = [{"prompt": p, "url": u, "ts": t} for p, u, t in rows]
        return jsonify({"items": items})
    except Exception as e:
        log_error(f"api_history_image: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/pay', methods=['POST'])
def api_pay():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        amount = int(data.get("amount", 0))
        tokens = int(data.get("tokens", 0))
        if amount <= 0 or tokens <= 0 or amount > 100000:
            return jsonify({"error": "Неверные параметры"}), 400
        oid = f"ORD-{chat_id}-{tokens}-{amount}"
        save_order(oid, chat_id, tokens, amount)
        pay_url = f"{PAY_BASE_URL}/pay/{amount}/{oid}"
        return jsonify({"pay_url": pay_url, "order_id": oid})
    except Exception as e:
        log_error(f"api_pay: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/settings', methods=['POST'])
def api_settings():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        field = data.get("field", "")
        if field == "mode":
            update_user(chat_id, 'mode', data.get("value", "regular"))
        elif field == "ai_mode":
            update_user(chat_id, 'ai_mode', data.get("value", "regular"))
        elif field == "coder_mode":
            update_user(chat_id, 'coder_mode', data.get("value", "with_hints"))
        elif field == "send_files":
            user = get_user(chat_id)
            new_val = 0 if user[17] else 1
            update_user(chat_id, 'send_files', new_val)
            return jsonify({"ok": True, "value": new_val})
        elif field == "sound_on":
            user = get_user(chat_id)
            new_val = 0 if user[22] else 1
            update_user(chat_id, 'sound_on', new_val)
            return jsonify({"ok": True, "value": new_val})
        else:
            return jsonify({"error": "bad field"}), 400
        return jsonify({"ok": True})
    except Exception as e:
        log_error(f"api_settings: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/file', methods=['POST'])
def api_file():
    try:
        init_data = request.form.get("init_data", "")
        user = verify_init_data(init_data)
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        chat_id = user.get("id")
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        if 'file' not in request.files:
            return jsonify({"error": "Нет файла"}), 400
        f = request.files['file']
        if not f.filename:
            return jsonify({"error": "Пустое имя"}), 400
        allowed = [".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
                   ".py", ".js", ".html", ".css", ".php", ".java", ".c", ".cpp",
                   ".go", ".rs", ".rb", ".sh", ".bat", ".log", ".ini", ".cfg",
                   ".sql", ".srt", ".vtt", ".tex", ".env", ".gitignore"]
        filename = f.filename
        if not any(filename.lower().endswith(e) for e in allowed):
            return jsonify({"error": "Формат не поддерживается"}), 400
        file_bytes = f.read()
        if len(file_bytes) > 500 * 1024:
            return jsonify({"error": "Файл слишком большой"}), 400
        try:
            text = file_bytes.decode('utf-8', errors='ignore')
        except Exception:
            text = file_bytes.decode('cp1251', errors='ignore')
        if not text.strip():
            return jsonify({"error": "Файл пустой"}), 400
        if len(text) > 50000:
            text = text[:50000] + "\n\n[...обрезано...]"
        u = get_user(chat_id)
        if u[9] or not u[12] or (u[14] >= 0 and u[16] >= u[14]) or u[0] < 1:
            return jsonify({"error": "Недоступно"}), 403
        cid = request.form.get("chat_db_id", "")
        if cid and cid.isdigit():
            cid = int(cid)
        else:
            cid = get_or_create_active_chat(chat_id)
        caption = (request.form.get("caption") or "").strip()
        if caption:
            prompt = f"Файл ({filename}):\n\n```\n{text}\n```\n\nЗадание: {caption}"
        else:
            prompt = f"Файл ({filename}):\n\n```\n{text}\n```\n\nЧто можно с этим сделать?"
        mark_operation_start(chat_id, "ai")
        try:
            answer = ask_gigachat(chat_id, prompt, u[2], u[3], user_chat_id=cid)
        finally:
            mark_operation_end(chat_id)
        add_tokens(chat_id, -1)
        log_stat(chat_id, 1)
        inc_used(chat_id, 'used_chats')
        log_action(chat_id, "file", filename[:100])
        nb = get_user(chat_id)[0]
        is_file = False
        file_content = None
        out_filename = None
        display = answer
        if u[2] == "coder":
            lang = detect_code_language(answer)
            code = extract_code_block(answer)
            file_content = code
            out_filename = f"code.{lang}" if lang != "txt" else "code.txt"
            is_file = True
        elif len(answer) > MAX_TEXT_RESPONSE:
            file_content = answer
            out_filename = "answer.txt"
            is_file = True
            display = "Ответ в файле"
        return jsonify({
            "answer": display, "filename": filename, "balance": nb, "chat_db_id": cid,
            "is_file": is_file, "file_content": file_content, "out_filename": out_filename,
        })
    except Exception as e:
        log_error(f"api_file: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/tickets/list', methods=['POST'])
def api_tickets_list():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        rows = get_user_tickets(chat_id)
        items = [{"id": t[0], "message": t[1] or "", "answer": t[2] or "", "status": t[3] or "open"} for t in rows]
        return jsonify({"items": items})
    except Exception as e:
        log_error(f"api_tickets_list: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/webapp/api/tickets/create', methods=['POST'])
def api_tickets_create():
    try:
        data = request.get_json() or {}
        chat_id = api_auth(data)
        if not chat_id:
            return jsonify({"error": "unauthorized"}), 401
        message = (data.get("message") or "").strip()
        if not message:
            return jsonify({"error": "Пусто"}), 400
        muted, time_left = check_support_muted(chat_id)
        if muted:
            return jsonify({"error": f"Подожди {time_left}"}), 403
        u = get_user(chat_id)
        username = u[6] if u[6] else f"ID:{chat_id}"
        tid = create_ticket(chat_id, username, message)
        try:
            bot.send_message(ADMIN_ID, f"Новый тикет #{tid} от {username}\n\n{message}")
        except Exception:
            pass
        return jsonify({"ok": True, "ticket_id": tid})
    except Exception as e:
        log_error(f"api_tickets_create: {e}")
        return jsonify({"error": str(e)}), 500


def backup_db():
    try:
        if not os.path.exists(DB_PATH):
            return False
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = time.strftime('%Y-%m-%d_%H-%M')
        bp = os.path.join(BACKUP_DIR, f"users_{ts}.db")
        shutil.copy2(DB_PATH, bp)
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


def cleanup_active_operations():
    while True:
        try:
            conn = get_conn()
            c = conn.cursor()
            cutoff = int(time.time()) - 300
            c.execute("DELETE FROM active_operations WHERE started < ?", (cutoff,))
            conn.commit()
            conn.close()
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
        except Exception as e:
            log_error(f"cleanup_errors_log: {e}")


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
                for (uid,) in users:
                    if uid == ADMIN_ID:
                        continue
                    if can_use_during_maintenance(uid):
                        continue
                    try:
                        sent = bot.send_message(uid, escape_html(msg))
                        save_maintenance_notified(uid, sent.message_id)
                    except Exception:
                        pass
                set_maintenance(0, notified=1)
        except Exception as e:
            log_error(f"maintenance_worker: {e}")
        time.sleep(30)


def send_startup_report():
    if ERRORS_CHANNEL_ID == 0:
        return
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*), COALESCE(SUM(tokens), 0) FROM users")
        uc, tt = c.fetchone()
        conn.close()
        text = f"Бот запущен\n\nЮзеров: {uc}\nТокенов: {tt}"
        bot.send_message(ERRORS_CHANNEL_ID, text)
    except Exception as e:
        print(f"Startup report error: {e}")


WEBAPP_MAINTENANCE_HTML = '''<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
body{background:linear-gradient(180deg,#0a1628,#0d1f38);color:#e8f4ff;
font-family:-apple-system,Arial,sans-serif;text-align:center;padding:60px 20px;margin:0;min-height:100vh;}
h1{font-size:80px;margin:0 0 20px;}
h2{font-size:22px;font-weight:500;margin:0 0 20px;line-height:1.4;}
p{font-size:16px;opacity:0.6;margin:0;}
</style></head><body>
<h1>🛠</h1><h2>MSG_PLACEHOLDER</h2><p>Поддержка работает</p>
</body></html>'''
WEBAPP_HTML = r'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Боб AI</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}
html,body{overscroll-behavior:none;}
:root{
--bg:#0a1628;--bg2:#0d1f38;--card:#132a45;--card2:#1a3757;
--text:#e8f4ff;--primary:#4a9eff;--primary-dark:#2c7be5;--primary-light:#7bc0ff;
--grad:linear-gradient(180deg,#5ba8ff 0%,#2c7be5 50%,#1a5fb4 100%);
--grad-header:linear-gradient(180deg,#4a9eff 0%,#2c7be5 60%,#1a5fb4 100%);
--danger:#ff5c5c;--success:#4ade80;--warning:#fbbf24;
--border:rgba(74,158,255,0.3);--glow:rgba(74,158,255,0.5);
--shadow:0 2px 8px rgba(0,0,0,0.3);}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:linear-gradient(180deg,var(--bg) 0%,var(--bg2) 100%);
color:var(--text);min-height:100vh;position:relative;overflow-x:hidden;}
body::before{content:"";position:fixed;bottom:0;left:0;right:0;height:280px;
background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1200 120'%3E%3Cpath d='M0,60 C150,100 350,20 600,60 C850,100 1050,20 1200,60 L1200,120 L0,120 Z' fill='%234a9eff' opacity='0.06'/%3E%3C/svg%3E") repeat-x;
background-size:1200px 120px;pointer-events:none;z-index:0;
animation:waveMove 25s linear infinite;opacity:0.5;}
@keyframes waveMove{from{background-position:0 0;}to{background-position:1200px 0;}}
.fish{position:fixed;font-size:22px;opacity:0.08;pointer-events:none;z-index:0;
animation:fishSwim 35s linear infinite;}
.fish:nth-child(1){top:15%;animation-duration:38s;}
.fish:nth-child(2){top:40%;animation-duration:45s;animation-delay:-10s;font-size:16px;}
.fish:nth-child(3){top:65%;animation-duration:40s;animation-delay:-20s;font-size:28px;}
@keyframes fishSwim{from{transform:translateX(-100px);}to{transform:translateX(calc(100vw + 100px));}}
.app-header{position:sticky;top:0;z-index:100;background:var(--grad-header);
padding:14px 16px;display:flex;align-items:center;gap:12px;
box-shadow:0 4px 20px var(--glow),inset 0 1px 0 rgba(255,255,255,0.35),
inset 0 -1px 0 rgba(0,0,0,0.25);border-bottom:2px solid rgba(255,255,255,0.15);}
.app-header .logo{width:40px;height:40px;flex-shrink:0;
background:linear-gradient(180deg,rgba(255,255,255,0.45) 0%,rgba(255,255,255,0.15) 100%);
border-radius:12px;display:flex;align-items:center;justify-content:center;
box-shadow:inset 0 1px 0 rgba(255,255,255,0.6),0 2px 8px rgba(0,0,0,0.2);
font-size:22px;}
.app-header .title{font-size:20px;font-weight:800;color:#fff;flex:1;
text-shadow:0 2px 4px rgba(0,0,0,0.4);letter-spacing:0.5px;}
.app-header .balance{font-size:14px;color:#fff;
background:linear-gradient(180deg,rgba(255,255,255,0.35) 0%,rgba(255,255,255,0.15) 100%);
padding:8px 14px;border-radius:20px;font-weight:800;
display:flex;align-items:center;gap:6px;
box-shadow:inset 0 1px 0 rgba(255,255,255,0.5),0 2px 8px rgba(0,0,0,0.2);}
.container{padding:16px;padding-bottom:120px;position:relative;z-index:1;}
.btn{display:flex;align-items:center;gap:14px;padding:16px 20px;
background:linear-gradient(180deg,var(--card2) 0%,var(--card) 100%);
border:1px solid var(--border);border-radius:14px;color:var(--text);
font-size:16px;font-weight:600;cursor:pointer;transition:all 0.15s;
width:100%;text-align:left;
box-shadow:inset 0 1px 0 rgba(255,255,255,0.12),var(--shadow);margin-bottom:10px;}
.btn:active{transform:scale(0.98);
box-shadow:inset 0 2px 8px rgba(0,0,0,0.5),0 0 20px var(--glow);}
.btn .ico{width:44px;height:44px;flex-shrink:0;display:flex;align-items:center;
justify-content:center;border-radius:12px;
background:linear-gradient(180deg,var(--glow) 0%,transparent 100%);
box-shadow:inset 0 1px 0 rgba(255,255,255,0.35);font-size:22px;}
.btn .lbl{flex:1;}
.btn .arrow{opacity:0.6;font-size:24px;color:var(--primary-light);}
.screen{display:none;}
.screen.active{display:block;animation:fadeIn 0.25s;}
@keyframes fadeIn{from{opacity:0;transform:translateY(10px);}to{opacity:1;transform:translateY(0);}}
.screen-header{display:flex;align-items:center;gap:12px;padding:12px 16px;
background:linear-gradient(180deg,var(--card2) 0%,var(--card) 100%);
border-radius:14px;margin-bottom:16px;position:sticky;top:76px;z-index:50;
border:1px solid var(--border);
box-shadow:inset 0 1px 0 rgba(255,255,255,0.12),0 4px 12px rgba(0,0,0,0.35);}
.back-btn{background:none;border:none;color:var(--primary-light);
font-size:24px;cursor:pointer;padding:4px 10px 4px 0;display:flex;align-items:center;}
.back-btn:active{opacity:0.6;}
.screen-title{font-size:17px;font-weight:700;flex:1;}
.input-wrap{margin-bottom:14px;}
.input-wrap textarea,.input-wrap input{width:100%;padding:14px 16px;
background:var(--card);border:1px solid var(--border);border-radius:14px;
color:var(--text);font-size:15px;font-family:inherit;resize:none;outline:none;
box-shadow:inset 0 2px 6px rgba(0,0,0,0.3);}
.input-wrap textarea:focus,.input-wrap input:focus{border-color:var(--primary);
box-shadow:inset 0 2px 6px rgba(0,0,0,0.3),0 0 12px var(--glow);}
.input-wrap textarea{min-height:100px;}
.action-btn{width:100%;padding:16px;border:none;border-radius:14px;
background:var(--grad);color:#fff;font-size:16px;font-weight:700;
cursor:pointer;transition:all 0.15s;
box-shadow:inset 0 1px 0 rgba(255,255,255,0.4),0 4px 16px var(--glow);}
.action-btn:active{transform:scale(0.98);opacity:0.9;}
.action-btn:disabled{opacity:0.5;cursor:not-allowed;}
.action-btn.secondary{background:linear-gradient(180deg,var(--card2),var(--card));
border:1px solid var(--border);color:var(--text);}
.chat-messages{display:flex;flex-direction:column;gap:12px;margin-bottom:16px;min-height:200px;}
.msg{max-width:85%;padding:12px 16px;border-radius:16px;font-size:15px;
line-height:1.45;word-wrap:break-word;white-space:pre-wrap;animation:msgIn 0.25s;}
@keyframes msgIn{from{opacity:0;transform:translateY(8px);}to{opacity:1;transform:translateY(0);}}
.msg.user{align-self:flex-end;background:var(--grad);color:#fff;
border-bottom-right-radius:4px;
box-shadow:inset 0 1px 0 rgba(255,255,255,0.35),0 4px 12px var(--glow);}
.msg.bot{align-self:flex-start;
background:linear-gradient(180deg,var(--card2),var(--card));
color:var(--text);border-bottom-left-radius:4px;
border:1px solid var(--border);
box-shadow:inset 0 1px 0 rgba(255,255,255,0.08),var(--shadow);}
.msg.bot.file-msg{display:flex;align-items:center;gap:10px;cursor:pointer;
border:1px dashed var(--primary);}
.msg.bot.file-msg:active{opacity:0.7;}
.msg.bot.file-msg .file-ico{font-size:22px;}
.msg.bot.file-msg .file-name{font-weight:700;color:var(--primary-light);}
.msg.bot.file-msg .file-sub{font-size:11px;opacity:0.6;margin-top:2px;}
.attach-area{display:flex;align-items:center;gap:10px;padding:10px 14px;
background:var(--card);border:1px dashed var(--border);
border-radius:14px;margin-bottom:10px;cursor:pointer;transition:all 0.15s;}
.attach-area:active{transform:scale(0.99);background:var(--card2);}
.attach-area.has-file{border-style:solid;border-color:var(--primary);
background:linear-gradient(180deg,rgba(74,158,255,0.15),rgba(74,158,255,0.05));
box-shadow:0 0 16px var(--glow);}
.attach-area .att-ico{width:40px;height:40px;flex-shrink:0;
display:flex;align-items:center;justify-content:center;border-radius:10px;
font-size:20px;background:linear-gradient(180deg,var(--glow),transparent);}
.attach-area .att-txt{flex:1;min-width:0;}
.attach-area .att-txt .att-title{font-size:14px;font-weight:600;}
.attach-area .att-txt .att-sub{font-size:11px;opacity:0.6;margin-top:2px;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.attach-area .att-remove{width:32px;height:32px;flex-shrink:0;
border-radius:8px;border:1px solid var(--border);
background:rgba(255,92,92,0.15);color:var(--danger);
display:flex;align-items:center;justify-content:center;
cursor:pointer;font-size:14px;}
.chat-bottom-panel{position:fixed;bottom:0;left:0;right:0;z-index:200;
background:linear-gradient(180deg,rgba(10,22,40,0.9) 0%,var(--bg) 30%);
backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
padding:10px 14px 14px 14px;border-top:1px solid var(--border);
box-shadow:0 -8px 24px rgba(0,0,0,0.4);display:none;}
.chat-bottom-panel.active{display:block;}
.chat-input-row{display:flex;gap:8px;align-items:flex-end;}
.chat-input-row textarea{flex:1;padding:12px 14px;
background:var(--card);border:1px solid var(--border);border-radius:14px;
color:var(--text);font-size:15px;font-family:inherit;resize:none;outline:none;
max-height:120px;min-height:44px;
box-shadow:inset 0 2px 6px rgba(0,0,0,0.3);}
.chat-input-row textarea:focus{border-color:var(--primary);
box-shadow:inset 0 2px 6px rgba(0,0,0,0.3),0 0 12px var(--glow);}
.chat-input-row .cancel-btn{width:44px;height:44px;flex-shrink:0;
border-radius:12px;border:1px solid var(--border);
background:rgba(255,92,92,0.15);color:var(--danger);
display:flex;align-items:center;justify-content:center;
cursor:pointer;font-size:20px;transition:all 0.15s;}
.chat-input-row .cancel-btn:active{transform:scale(0.92);}
.chat-input-row .send-btn{width:44px;height:44px;flex-shrink:0;
border-radius:12px;border:none;background:var(--grad);color:#fff;font-size:20px;
display:flex;align-items:center;justify-content:center;cursor:pointer;
box-shadow:0 4px 12px var(--glow);transition:all 0.15s;}
.chat-input-row .send-btn:active{transform:scale(0.92);}
.chat-input-row .send-btn:disabled{opacity:0.4;}
.chat-hints{padding:14px;background:rgba(74,158,255,0.15);
border:1px solid var(--border);border-radius:14px;
margin-bottom:12px;font-size:13px;line-height:1.6;}
.chat-hints .title{font-weight:700;color:var(--primary-light);
margin-bottom:8px;font-size:14px;}
.chat-hints .hint{padding:3px 0;}
.chat-hints .hint .emoji{margin-right:6px;}
.chat-mode-badge{display:flex;gap:8px;padding:0 0 10px 0;flex-wrap:wrap;}
.badge{padding:6px 12px;
background:linear-gradient(180deg,var(--glow),transparent);
border:1px solid var(--border);border-radius:16px;
font-size:12px;font-weight:600;color:var(--primary-light);}
.progress-wrap{margin:20px 0;}
.progress-bar{width:100%;height:10px;background:rgba(0,0,0,0.4);
border-radius:10px;overflow:hidden;border:1px solid var(--border);}
.progress-fill{height:100%;background:var(--grad);border-radius:10px;
transition:width 0.3s ease;}
.progress-text{text-align:center;margin-top:12px;font-size:14px;
color:var(--primary-light);font-weight:600;}
.result-img{width:100%;border-radius:16px;
box-shadow:0 8px 32px var(--glow),0 0 0 1px var(--border);
margin-bottom:16px;display:block;}
.result-caption{text-align:center;font-size:14px;
color:var(--primary-light);margin-bottom:16px;padding:0 10px;font-weight:600;}
.loading{text-align:center;padding:60px 20px;
color:var(--primary-light);font-size:15px;
animation:pulse 1.5s ease-in-out infinite;}
@keyframes pulse{0%,100%{opacity:0.5;}50%{opacity:1;}}
.err{color:var(--danger);text-align:center;padding:40px 20px;font-size:15px;
background:rgba(255,92,92,0.1);border:1px solid rgba(255,92,92,0.3);
border-radius:12px;}
.tabs{display:flex;gap:8px;margin-bottom:16px;background:var(--card);
padding:5px;border-radius:14px;border:1px solid var(--border);}
.tab{flex:1;padding:11px;background:none;border:none;border-radius:10px;
color:var(--text);font-size:14px;font-weight:600;cursor:pointer;opacity:0.6;}
.tab.active{background:var(--grad);opacity:1;}
.history-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
.history-item{background:linear-gradient(180deg,var(--card2),var(--card));
border-radius:14px;overflow:hidden;border:1px solid var(--border);}
.history-item img{width:100%;display:block;aspect-ratio:1;
object-fit:cover;background:#000;cursor:pointer;}
.history-item .cap{padding:8px 10px;font-size:11px;
color:var(--primary-light);white-space:nowrap;overflow:hidden;
text-overflow:ellipsis;background:rgba(0,0,0,0.3);font-weight:600;}
.buy-option{padding:18px;background:linear-gradient(180deg,var(--card2),var(--card));
border:1px solid var(--border);border-radius:14px;margin-bottom:10px;
cursor:pointer;text-align:center;font-size:16px;font-weight:700;
transition:all 0.15s;display:flex;align-items:center;justify-content:center;gap:10px;}
.buy-option:active{transform:scale(0.98);opacity:0.85;}
.buy-option.best{border-color:var(--primary);
background:linear-gradient(180deg,var(--glow),transparent);}
.section-title{font-size:12px;color:var(--primary-light);
text-transform:uppercase;letter-spacing:1.5px;
margin:20px 0 10px;font-weight:700;padding-left:4px;}
.mode-option{padding:14px 18px;
background:linear-gradient(180deg,var(--card2),var(--card));
border:1px solid var(--border);border-radius:12px;margin-bottom:8px;
cursor:pointer;display:flex;align-items:center;gap:12px;
font-size:15px;font-weight:500;transition:all 0.15s;}
.mode-option:active{transform:scale(0.98);}
.mode-option.active{border-color:var(--primary);
background:linear-gradient(180deg,var(--glow),transparent);}
.mode-option .check{margin-left:auto;font-size:20px;
color:var(--primary-light);opacity:0;}
.mode-option.active .check{opacity:1;}
.chat-item{display:flex;align-items:center;gap:12px;padding:14px 16px;
background:linear-gradient(180deg,var(--card2),var(--card));
border:1px solid var(--border);border-radius:14px;margin-bottom:8px;
cursor:pointer;transition:all 0.15s;}
.chat-item:active{transform:scale(0.98);}
.chat-item.current{border-color:var(--primary);}
.chat-item .ico2{width:38px;height:38px;flex-shrink:0;
display:flex;align-items:center;justify-content:center;
border-radius:10px;font-size:20px;
background:linear-gradient(180deg,var(--glow),transparent);}
.chat-item .info{flex:1;min-width:0;}
.chat-item .info .name{font-size:14px;font-weight:700;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:2px;}
.chat-item .info .meta{font-size:11px;opacity:0.6;}
.chat-item .actions{display:flex;gap:6px;flex-shrink:0;}
.chat-item .actions .mini-btn{width:32px;height:32px;border-radius:8px;
border:1px solid var(--border);background:var(--card);color:var(--text);
display:flex;align-items:center;justify-content:center;
cursor:pointer;font-size:14px;padding:0;}
.chat-item .actions .mini-btn:active{transform:scale(0.9);}
.ticket-card{background:linear-gradient(180deg,var(--card2),var(--card));
border:1px solid var(--border);border-radius:14px;padding:14px;margin-bottom:10px;}
.ticket-card .head{display:flex;justify-content:space-between;
margin-bottom:8px;font-size:12px;}
.ticket-card .status{padding:2px 10px;border-radius:8px;font-weight:700;}
.ticket-card .status.open{background:rgba(251,191,36,0.2);color:var(--warning);}
.ticket-card .status.done{background:rgba(74,222,128,0.2);color:var(--success);}
.ticket-card .msg{font-size:14px;line-height:1.4;margin-bottom:6px;}
.ticket-card .answer{font-size:13px;padding:10px;
background:rgba(74,158,255,0.15);border-radius:10px;
border-left:3px solid var(--primary);margin-top:8px;}
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;
background:rgba(0,0,0,0.75);z-index:1000;display:none;
align-items:center;justify-content:center;padding:24px;}
.modal-overlay.active{display:flex;}
.modal-box{background:linear-gradient(180deg,var(--card2),var(--card));
border-radius:20px;padding:24px;width:100%;max-width:340px;
border:1px solid var(--border);}
.modal-title{font-size:18px;font-weight:700;margin-bottom:12px;text-align:center;}
.modal-text{font-size:14px;opacity:0.8;margin-bottom:20px;text-align:center;}
.modal-buttons{display:flex;gap:10px;}
.modal-btn{flex:1;padding:14px;border:none;border-radius:12px;
font-size:15px;font-weight:600;cursor:pointer;}
.modal-btn.cancel{background:rgba(74,158,255,0.15);
color:var(--text);border:1px solid var(--border);}
.modal-btn.confirm{background:var(--grad);color:#fff;}
.edit-thumb{width:70px;height:70px;border-radius:12px;object-fit:cover;
border:2px solid var(--border);}
.edit-thumbs{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0;
padding:12px;background:rgba(74,158,255,0.05);
border-radius:14px;border:1px dashed var(--border);min-height:94px;}
.edit-add-btn{width:70px;height:70px;border-radius:12px;
border:2px dashed var(--primary);background:rgba(74,158,255,0.1);
display:flex;align-items:center;justify-content:center;
cursor:pointer;font-size:26px;color:var(--primary-light);transition:all 0.15s;}
.edit-add-btn:active{transform:scale(0.95);}
</style>
</head>
<body>
<div class="fish">🐟</div>
<div class="fish">🐠</div>
<div class="fish">🐡</div>
<div class="app-header">
  <div class="logo">🐟</div>
  <div class="title">Боб AI</div>
  <div class="balance">💎 <span id="balance-tokens">...</span></div>
</div>
<div class="container">
<div id="screen-main" class="screen active">
  <button class="btn" id="btn-open-generate"><div class="ico">🎨</div><div class="lbl">Сгенерировать</div><div class="arrow">›</div></button>
  <button class="btn" id="btn-open-edit"><div class="ico">🖼</div><div class="lbl">Редактировать фото</div><div class="arrow">›</div></button>
  <button class="btn" id="btn-open-chat"><div class="ico">🤖</div><div class="lbl">Чат с ИИ</div><div class="arrow">›</div></button>
  <button class="btn" id="btn-open-history"><div class="ico">📜</div><div class="lbl">История</div><div class="arrow">›</div></button>
  <button class="btn" id="btn-open-buy"><div class="ico">💳</div><div class="lbl">Купить токены</div><div class="arrow">›</div></button>
  <button class="btn" id="btn-open-support"><div class="ico">🆘</div><div class="lbl">Поддержка</div><div class="arrow">›</div></button>
</div>
<div id="screen-generate" class="screen">
  <div class="screen-header"><button class="back-btn" id="btn-back-gen">←</button><span class="screen-title">🎨 Генерация</span></div>
  <div id="gen-form"><div class="input-wrap"><textarea id="gen-prompt" placeholder="Опиши что нарисовать..."></textarea></div><button class="action-btn" id="gen-btn">🎨 Нарисовать — 4💎</button></div>
  <div id="gen-progress" style="display:none;"><div class="progress-wrap"><div class="progress-bar"><div class="progress-fill" id="gen-fill" style="width:0%;"></div></div><div class="progress-text" id="gen-status">⏳ Анализирую...</div></div></div>
  <div id="gen-result" style="display:none;"></div>
</div>
<div id="screen-edit" class="screen">
  <div class="screen-header"><button class="back-btn" id="btn-back-edit">←</button><span class="screen-title">🖼 Редактирование</span></div>
  <div id="edit-form">
    <div style="font-size:14px;opacity:0.8;margin-bottom:8px;">📸 Загрузи 1-3 фото и напиши задание:</div>
    <div class="edit-thumbs" id="edit-thumbs"></div>
    <input type="file" id="edit-file-input" accept="image/*" multiple style="display:none;">
    <div class="input-wrap"><textarea id="edit-prompt" placeholder="Что сделать с фото?"></textarea></div>
    <button class="action-btn" id="edit-btn">🖼 Обработать — 4💎</button>
  </div>
  <div id="edit-progress" style="display:none;"><div class="progress-wrap"><div class="progress-bar"><div class="progress-fill" id="edit-fill" style="width:0%;"></div></div><div class="progress-text" id="edit-status">⏳ Обрабатываю...</div></div></div>
  <div id="edit-result" style="display:none;"></div>
</div>
<div id="screen-chat" class="screen">
  <div class="screen-header"><button class="back-btn" id="btn-back-chat">←</button><span class="screen-title" id="chat-header-title">🤖 Чат с Бобом</span><button class="back-btn" id="btn-open-chats" title="Мои чаты">💬</button><button class="back-btn" id="btn-chat-settings" title="Настройки ИИ">⚙️</button></div>
  <div class="chat-mode-badge" id="chat-badges"></div>
  <div id="chat-hints" class="chat-hints">
    <div class="title">💡 Что можно написать:</div>
    <div class="hint"><span class="emoji">🎓</span>Помоги с домашкой по физике</div>
    <div class="hint"><span class="emoji">💻</span>Напиши код на Python</div>
    <div class="hint"><span class="emoji">✍️</span>Сочинение про космос</div>
    <div class="hint"><span class="emoji">🌍</span>Перевести на английский</div>
    <div class="hint"><span class="emoji">📖</span>Объясни квантовую физику</div>
    <div class="hint" style="margin-top:8px;opacity:0.6;font-size:12px;">📎 Прикрепи файл — Боб его разберёт</div>
  </div>
  <div class="chat-messages" id="chat-messages"></div>
  <div style="height:100px;"></div>
</div>
<div class="chat-bottom-panel" id="chat-bottom-panel">
  <div class="attach-area" id="chat-attach-area" style="display:none;">
    <div class="att-ico">📎</div>
    <div class="att-txt"><div class="att-title" id="att-title">Файл прикреплён</div><div class="att-sub" id="att-sub"></div></div>
    <div class="att-remove" id="att-remove">✕</div>
  </div>
  <div class="chat-input-row">
    <button class="cancel-btn" id="chat-cancel-btn" style="display:none;" title="Отменить">✕</button>
    <textarea id="chat-input" placeholder="Сообщение Бобу..." rows="1"></textarea>
    <button class="cancel-btn" id="chat-attach-btn" style="background:rgba(74,158,255,0.15);color:var(--primary-light);" title="Прикрепить файл">📎</button>
    <button class="send-btn" id="chat-send-btn" title="Отправить">➤</button>
  </div>
</div>
<div id="screen-chats" class="screen">
  <div class="screen-header"><button class="back-btn" id="btn-back-chats">←</button><span class="screen-title">💬 Мои чаты</span><button class="back-btn" id="btn-new-chat2" title="Новый чат">➕</button></div>
  <div id="chats-list"><div class="loading">Загрузка...</div></div>
</div>
<div id="screen-ai-settings" class="screen">
  <div class="screen-header"><button class="back-btn" id="btn-back-ai-set">←</button><span class="screen-title">⚙️ Настройки ИИ</span></div>
  <div class="section-title">🎯 Режим</div>
  <div id="chat-modes-list"></div>
  <div class="section-title">🧠 Поведение</div>
  <div id="chat-ai-modes-list"></div>
</div>
<div id="screen-history" class="screen">
  <div class="screen-header"><button class="back-btn" id="btn-back-hist">←</button><span class="screen-title">📜 История</span></div>
  <div class="tabs">
    <button class="tab active" id="tab-hist-gen">🎨 Генерации</button>
    <button class="tab" id="tab-hist-edit">🖼 Правки</button>
  </div>
  <div id="history-content"><div class="loading">Загрузка...</div></div>
</div>
<div id="screen-buy" class="screen">
  <div class="screen-header"><button class="back-btn" id="btn-back-buy">←</button><span class="screen-title">💳 Купить токены</span></div>
  <div class="section-title">Пакеты</div>
  <div class="buy-option" data-amount="100" data-tokens="20"><span style="opacity:0.7;">100 ₽</span> — <b>20 токенов</b></div>
  <div class="buy-option best" data-amount="250" data-tokens="50"><span style="opacity:0.7;">250 ₽</span> — <b>50 токенов</b> 🔥</div>
  <div class="buy-option" data-amount="500" data-tokens="100"><span style="opacity:0.7;">500 ₽</span> — <b>100 токенов</b></div>
  <div class="buy-option" data-amount="1000" data-tokens="200"><span style="opacity:0.7;">1000 ₽</span> — <b>200 токенов</b></div>
  <div class="section-title">Своя сумма</div>
  <div class="input-wrap"><input type="number" id="custom-tokens" placeholder="Кол-во токенов (20-3000)" min="20" max="3000"></div>
  <div style="font-size:12px;opacity:0.6;text-align:center;margin-bottom:10px;">💰 5 ₽ за 1 токен</div>
  <button class="action-btn" id="buy-custom-btn">✏️ Купить свою сумму</button>
</div>
<div id="screen-support" class="screen">
  <div class="screen-header"><button class="back-btn" id="btn-back-sup">←</button><span class="screen-title">🆘 Поддержка</span></div>
  <div class="section-title">Новый тикет</div>
  <div class="input-wrap"><textarea id="support-msg" placeholder="Опиши проблему или вопрос..."></textarea></div>
  <button class="action-btn" id="support-send-btn">📩 Отправить тикет</button>
  <div class="section-title">📋 Мои тикеты</div>
  <div id="support-tickets"><div class="loading">Загрузка...</div></div>
</div>
</div>
<div class="modal-overlay" id="exit-modal">
  <div class="modal-box">
    <div class="modal-title">🚪 Выйти?</div>
    <div class="modal-text">Закрыть приложение?</div>
    <div class="modal-buttons">
      <button class="modal-btn cancel" id="btn-modal-stay">Остаться</button>
      <button class="modal-btn confirm" id="btn-modal-exit">Выйти</button>
    </div>
  </div>
</div>
<script>
(function(){
'use strict';
var tg=window.Telegram.WebApp;tg.ready();tg.expand();
try{tg.setHeaderColor('#4a9eff');tg.setBackgroundColor('#0a1628');}catch(e){}
var initData=tg.initData||'';
var state={me:null,currentScreen:'main',isGenerating:false,isEditing:false,isChatting:false,pendingChatFile:null,pendingEditFiles:[],currentChatId:null,currentChatTitle:''};
function $(id){return document.getElementById(id);}
function escapeHtml(t){var d=document.createElement('div');d.textContent=t==null?'':String(t);return d.innerHTML;}
function formatSize(b){if(b<1024)return b+' B';if(b<1024*1024)return (b/1024).toFixed(1)+' KB';return (b/1024/1024).toFixed(1)+' MB';}
function apiCall(ep,data){data=data||{};data.init_data=initData;return fetch('/webapp/api/'+ep,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}).then(function(r){return r.json();}).catch(function(e){return {error:'network: '+e.message};});}
function showScreen(name){
if(state.isGenerating&&name!=='generate'){try{tg.showAlert('⏳ Генерация идёт');}catch(e){}return;}
if(state.isEditing&&name!=='edit'){try{tg.showAlert('⏳ Обработка идёт');}catch(e){}return;}
var ss=document.querySelectorAll('.screen');for(var i=0;i<ss.length;i++)ss[i].classList.remove('active');
var el=$('screen-'+name);if(el)el.classList.add('active');
state.currentScreen=name;
if(name==='chat'){$('chat-bottom-panel').classList.add('active');loadChatHistory();updateChatBadges();}
else{$('chat-bottom-panel').classList.remove('active');}
if(name==='history')loadHistory('gen',$('tab-hist-gen'));
if(name==='chats')loadChatsList();
if(name==='ai-settings')loadAISettings();
if(name==='support')loadTickets();
if(name==='generate')resetGenForm();
if(name==='edit')resetEditForm();
updateBackButton();window.scrollTo({top:0,behavior:'smooth'});
}
function updateBackButton(){try{if(state.currentScreen!=='main')tg.BackButton.show();else tg.BackButton.hide();}catch(e){}}
function openExitModal(){$('exit-modal').classList.add('active');}
function closeExitModal(){$('exit-modal').classList.remove('active');}
try{tg.BackButton.onClick(function(){
if(state.currentScreen==='main')openExitModal();
else if(state.currentScreen==='ai-settings')showScreen('chat');
else if(state.currentScreen==='chats')showScreen('chat');
else showScreen('main');
});}catch(e){}
function updateBalance(b){if(b!==undefined&&b!==null&&state.me){state.me.tokens=b;$('balance-tokens').textContent=b;}}
function loadMe(){apiCall('me').then(function(d){
if(d.error==='banned'){document.body.innerHTML='<div style="text-align:center;padding:80px 20px;"><h1 style="font-size:60px;margin-bottom:20px;">🚫</h1><h2>Вы забанены</h2><p style="opacity:0.7;margin-top:12px;">'+escapeHtml(d.reason||'')+'</p></div>';return;}
if(d.error){$('balance-tokens').textContent='?';return;}
state.me=d;$('balance-tokens').textContent=d.tokens;
});}
function resetGenForm(){$('gen-form').style.display='block';$('gen-progress').style.display='none';$('gen-result').style.display='none';$('gen-result').innerHTML='';$('gen-fill').style.width='0%';$('gen-btn').disabled=false;$('gen-btn').textContent='🎨 Нарисовать — 4💎';}
function doGenerate(){
if(state.isGenerating)return;
var prompt=($('gen-prompt').value||'').trim();
if(!prompt){try{tg.showAlert('❌ Введи промт');}catch(e){}return;}
if(!state.me||state.me.tokens<4){try{tg.showAlert('❌ Нужно 4 токена');}catch(e){}return;}
state.isGenerating=true;$('gen-form').style.display='none';$('gen-progress').style.display='block';
var percent=0;var stages=[[0,'⏳ Анализирую...'],[20,'🎨 Начинаю...'],[40,'💎 Рисую основу...'],[60,'✨ Добавляю детали...'],[80,'🔥 Финал...'],[95,'📤 Отправляю...']];var status=stages[0][1];
var interval=setInterval(function(){if(percent<90)percent+=Math.random()*8;else percent=Math.min(percent+1,95);$('gen-fill').style.width=percent+'%';for(var i=0;i<stages.length;i++){if(percent>=stages[i][0])status=stages[i][1];}$('gen-status').textContent=status;},400);
apiCall('generate',{prompt:prompt}).then(function(d){
clearInterval(interval);
if(d.error){$('gen-fill').style.width='100%';$('gen-status').textContent='❌ Ошибка';setTimeout(function(){resetGenForm();try{tg.showAlert('❌ '+d.error);}catch(e){}},600);state.isGenerating=false;return;}
$('gen-fill').style.width='100%';$('gen-status').textContent='✅ Готово!';updateBalance(d.balance);
setTimeout(function(){
$('gen-progress').style.display='none';$('gen-result').style.display='block';
var r=$('gen-result');r.innerHTML='';
var img=document.createElement('img');img.className='result-img';img.src=d.url;r.appendChild(img);
var cap=document.createElement('div');cap.className='result-caption';cap.textContent='🎨 '+(d.prompt||'');r.appendChild(cap);
var b1=document.createElement('button');b1.className='action-btn';b1.style.marginBottom='10px';b1.textContent='🔁 Ещё раз — 4💎';b1.addEventListener('click',resetGenForm);r.appendChild(b1);
var b2=document.createElement('button');b2.className='action-btn secondary';b2.textContent='🏠 В меню';b2.addEventListener('click',function(){showScreen('main');});r.appendChild(b2);
state.isGenerating=false;},800);
});
}
function resetEditForm(){$('edit-form').style.display='block';$('edit-progress').style.display='none';$('edit-result').style.display='none';$('edit-result').innerHTML='';$('edit-fill').style.width='0%';$('edit-btn').disabled=false;$('edit-btn').textContent='🖼 Обработать — 4💎';state.pendingEditFiles=[];renderEditThumbs();}
function renderEditThumbs(){
var c=$('edit-thumbs');c.innerHTML='';
for(var i=0;i<state.pendingEditFiles.length;i++){
(function(idx,file){
var wrap=document.createElement('div');wrap.style.position='relative';
var img=document.createElement('img');img.className='edit-thumb';img.src=URL.createObjectURL(file);wrap.appendChild(img);
var del=document.createElement('div');del.style.cssText='position:absolute;top:-6px;right:-6px;width:22px;height:22px;border-radius:50%;background:#ff5c5c;color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:12px;';
del.textContent='✕';del.addEventListener('click',function(){state.pendingEditFiles.splice(idx,1);renderEditThumbs();});
wrap.appendChild(del);c.appendChild(wrap);
})(i,state.pendingEditFiles[i]);
}
if(state.pendingEditFiles.length<3){var addBtn=document.createElement('div');addBtn.className='edit-add-btn';addBtn.textContent='+';addBtn.addEventListener('click',function(){$('edit-file-input').click();});c.appendChild(addBtn);}
}
function editFileSelected(input){if(!input.files)return;var rem=3-state.pendingEditFiles.length;for(var i=0;i<Math.min(input.files.length,rem);i++)state.pendingEditFiles.push(input.files[i]);input.value='';renderEditThumbs();}
function doEdit(){
if(state.isEditing)return;
var prompt=($('edit-prompt').value||'').trim();
if(!prompt){try{tg.showAlert('❌ Напиши задание');}catch(e){}return;}
if(state.pendingEditFiles.length===0){try{tg.showAlert('❌ Загрузи фото');}catch(e){}return;}
if(!state.me||state.me.tokens<4){try{tg.showAlert('❌ Нужно 4 токена');}catch(e){}return;}
state.isEditing=true;$('edit-form').style.display='none';$('edit-progress').style.display='block';
var percent=0;var stages=[[0,'⏳ Загружаю фото...'],[20,'🎨 Анализирую...'],[40,'💎 Обрабатываю...'],[60,'✨ Добавляю детали...'],[80,'🔥 Финал...'],[95,'📤 Отправляю...']];var status=stages[0][1];
var interval=setInterval(function(){if(percent<90)percent+=Math.random()*8;else percent=Math.min(percent+1,95);$('edit-fill').style.width=percent+'%';for(var i=0;i<stages.length;i++){if(percent>=stages[i][0])status=stages[i][1];}$('edit-status').textContent=status;},400);
var fd=new FormData();fd.append('init_data',initData);fd.append('prompt',prompt);
for(var i=0;i<state.pendingEditFiles.length;i++)fd.append('photos',state.pendingEditFiles[i]);
fetch('/webapp/api/edit',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(d){
clearInterval(interval);
if(d.error){$('edit-fill').style.width='100%';$('edit-status').textContent='❌ Ошибка';setTimeout(function(){resetEditForm();try{tg.showAlert('❌ '+d.error);}catch(e){}},600);state.isEditing=false;return;}
$('edit-fill').style.width='100%';$('edit-status').textContent='✅ Готово!';updateBalance(d.balance);
setTimeout(function(){
$('edit-progress').style.display='none';$('edit-result').style.display='block';
var r=$('edit-result');r.innerHTML='';
var img=document.createElement('img');img.className='result-img';img.src=d.url;r.appendChild(img);
var b1=document.createElement('button');b1.className='action-btn';b1.style.marginBottom='10px';b1.textContent='🔁 Ещё раз — 4💎';b1.addEventListener('click',resetEditForm);r.appendChild(b1);
var b2=document.createElement('button');b2.className='action-btn secondary';b2.textContent='🏠 В меню';b2.addEventListener('click',function(){showScreen('main');});r.appendChild(b2);
state.isEditing=false;},800);
}).catch(function(){clearInterval(interval);resetEditForm();try{tg.showAlert('❌ Ошибка сети');}catch(e){}state.isEditing=false;});
}
function loadChatHistory(){
if(!state.currentChatId){
apiCall('chats/list').then(function(d){
if(d.error||!d.items||d.items.length===0){
apiCall('chats/create').then(function(d2){
if(d2.error)return;
state.currentChatId=d2.chat_db_id;state.currentChatTitle=d2.title;
$('chat-header-title').textContent='🤖 '+d2.title;$('chat-messages').innerHTML='';$('chat-hints').style.display='block';
});
return;
}
state.currentChatId=d.items[0].id;state.currentChatTitle=d.items[0].title;
$('chat-header-title').textContent='🤖 '+d.items[0].title;loadChatMessages(state.currentChatId);
});return;
}
loadChatMessages(state.currentChatId);
}
function loadChatMessages(chatId){
apiCall('chats/messages',{chat_db_id:chatId}).then(function(d){
var c=$('chat-messages');c.innerHTML='';
if(d.error){c.innerHTML='<div class="err">Ошибка: '+escapeHtml(d.error)+'</div>';return;}
if(d.title){state.currentChatTitle=d.title;$('chat-header-title').textContent='🤖 '+d.title;}
if(!d.items||d.items.length===0){$('chat-hints').style.display='block';return;}
$('chat-hints').style.display='none';
for(var i=0;i<d.items.length;i++)appendMessage(d.items[i].role,d.items[i].content,false);
scrollChatBottom();
});
}
function updateChatBadges(){
if(!state.me)return;
var modeNames={regular:'🤖 Обычный',coder:'💻 Кодер',explainer:'📖 Объяснятор',translator:'🌍 Переводчик'};
var aiNames={regular:'🤖 Обычный',smart:'🧠 Умный',open:'💬 Откровенный',uncensored:'🔥 Без цензуры'};
var b=$('chat-badges');b.innerHTML='';
var b1=document.createElement('div');b1.className='badge';b1.textContent=modeNames[state.me.mode]||'🤖 Обычный';b.appendChild(b1);
var b2=document.createElement('div');b2.className='badge';b2.textContent=aiNames[state.me.ai_mode]||'🤖 Обычный';b.appendChild(b2);
}
function appendMessage(role,text,isFile,fileContent,filename){
var c=$('chat-messages');var div=document.createElement('div');
if(isFile){
div.className='msg bot file-msg';
div.innerHTML='<div class="file-ico">📄</div><div><div class="file-name">'+escapeHtml(filename||'answer.txt')+'</div><div class="file-sub">Нажми чтобы скачать</div></div>';
div.addEventListener('click',function(){
var content=fileContent||text||'';
var blob=new Blob([content],{type:'text/plain;charset=utf-8'});
var url=URL.createObjectURL(blob);
var a=document.createElement('a');a.href=url;a.download=filename||'answer.txt';a.click();
setTimeout(function(){URL.revokeObjectURL(url);},1000);
});
}else{
div.className='msg '+(role==='user'?'user':'bot');
div.textContent=text;
}
c.appendChild(div);
}
function scrollChatBottom(){setTimeout(function(){window.scrollTo({top:document.body.scrollHeight,behavior:'smooth'});},100);}
function chatAttachClick(){
var inp=document.createElement('input');inp.type='file';
inp.accept='.txt,.md,.csv,.json,.xml,.yaml,.yml,.py,.js,.html,.css,.php,.java,.c,.cpp,.go,.rs,.rb,.sh,.bat,.log,.ini,.cfg,.sql,.srt,.vtt,.tex,.env,.gitignore';
inp.addEventListener('change',function(){
if(!inp.files||!inp.files[0])return;
var f=inp.files[0];
if(f.size>500*1024){try{tg.showAlert('❌ Файл больше 500 KB');}catch(e){}return;}
state.pendingChatFile=f;showAttachArea();
});
inp.click();
}
function showAttachArea(){
var a=$('chat-attach-area');
if(!state.pendingChatFile){a.style.display='none';return;}
a.style.display='flex';a.classList.add('has-file');
$('att-title').textContent='📎 '+state.pendingChatFile.name;
$('att-sub').textContent=formatSize(state.pendingChatFile.size);
updateCancelBtn();
}
function clearChatFile(){
state.pendingChatFile=null;
$('chat-attach-area').style.display='none';
$('chat-attach-area').classList.remove('has-file');
updateCancelBtn();
}
function updateCancelBtn(){
var txt=($('chat-input').value||'').trim();
var has=txt.length>0||state.pendingChatFile;
$('chat-cancel-btn').style.display=has?'flex':'none';
}
function cancelInput(){$('chat-input').value='';clearChatFile();updateCancelBtn();autoResizeInput();}
function autoResizeInput(){var ta=$('chat-input');ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight,120)+'px';}
function createNewChat(){
apiCall('chats/create').then(function(d){
if(d.error){try{tg.showAlert('❌ '+d.error);}catch(e){}return;}
state.currentChatId=d.chat_db_id;state.currentChatTitle=d.title;
$('chat-messages').innerHTML='';$('chat-header-title').textContent='🤖 '+d.title;
$('chat-hints').style.display='block';showScreen('chat');
try{tg.showAlert('✅ Новый чат');}catch(e){}
});
}
function loadChatsList(){
var c=$('chats-list');c.innerHTML='<div class="loading">Загрузка...</div>';
apiCall('chats/list').then(function(d){
if(d.error){c.innerHTML='<div class="err">Ошибка: '+escapeHtml(d.error)+'</div>';return;}
if(!d.items||d.items.length===0){c.innerHTML='<div style="text-align:center;padding:50px 20px;opacity:0.5;">Чатов нет</div>';return;}
c.innerHTML='';
for(var i=0;i<d.items.length;i++){
(function(item){
var div=document.createElement('div');
div.className='chat-item'+(item.id===state.currentChatId?' current':'');
var when=item.last_message?new Date(item.last_message*1000):null;
var whenStr=when?(when.getDate()+'.'+(when.getMonth()+1)+' '+when.getHours()+':'+(when.getMinutes()<10?'0':'')+when.getMinutes()):'—';
div.innerHTML='<div class="ico2">💬</div><div class="info"><div class="name">'+escapeHtml(item.title||'Чат')+'</div><div class="meta">'+item.msg_count+' сообщ. • '+whenStr+'</div></div><div class="actions"><button class="mini-btn" data-act="rename">✏️</button><button class="mini-btn" data-act="delete">🗑</button></div>';
div.querySelector('.info').addEventListener('click',function(){state.currentChatId=item.id;state.currentChatTitle=item.title;showScreen('chat');});
div.querySelector('[data-act="rename"]').addEventListener('click',function(ev){
ev.stopPropagation();var newName=prompt('Новое название:',item.title);if(!newName)return;
apiCall('chats/rename',{chat_db_id:item.id,title:newName}).then(function(r){if(r.error){try{tg.showAlert('❌ '+r.error);}catch(e){}return;}loadChatsList();});
});
div.querySelector('[data-act="delete"]').addEventListener('click',function(ev){
ev.stopPropagation();
try{tg.showConfirm('Удалить чат "'+item.title+'"?',function(ok){
if(!ok)return;
apiCall('chats/delete',{chat_db_id:item.id}).then(function(r){if(r.error){try{tg.showAlert('❌ '+r.error);}catch(e){}return;}if(state.currentChatId===item.id)state.currentChatId=null;loadChatsList();});
});}catch(e){}
});
c.appendChild(div);
})(d.items[i]);
}
});
}
function doChat(){
if(state.isChatting)return;
var input=$('chat-input');
var question=(input.value||'').trim();
var file=state.pendingChatFile;
if(!question&&!file)return;
if(!state.me||state.me.tokens<1){try{tg.showAlert('❌ Нужно 1 токен');}catch(e){}return;}
$('chat-hints').style.display='none';
if(!state.currentChatId){
apiCall('chats/create').then(function(d){
if(d.error){try{tg.showAlert('❌ '+d.error);}catch(e){}return;}
state.currentChatId=d.chat_db_id;state.currentChatTitle=d.title;
$('chat-header-title').textContent='🤖 '+d.title;doChat();
});return;
}
if(file){
state.isChatting=true;
appendMessage('user','📎 '+file.name+(question?'\n'+question:''),false);
scrollChatBottom();input.value='';autoResizeInput();updateCancelBtn();
var typing=document.createElement('div');typing.className='msg bot';typing.id='typing-msg';typing.textContent='⏳ Боб читает файл...';
$('chat-messages').appendChild(typing);scrollChatBottom();
var fd=new FormData();
fd.append('init_data',initData);fd.append('file',file);fd.append('caption',question);fd.append('chat_db_id',String(state.currentChatId));
fetch('/webapp/api/file',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(d){
var t=$('typing-msg');if(t)t.remove();
if(d.error){appendMessage('bot','❌ '+d.error,false);}
else{updateBalance(d.balance);if(d.is_file&&d.file_content)appendMessage('bot',d.answer,true,d.file_content,d.out_filename||'answer.txt');else appendMessage('bot',d.answer,false);}
state.isChatting=false;scrollChatBottom();
}).catch(function(){var t=$('typing-msg');if(t)t.remove();appendMessage('bot','❌ Ошибка сети',false);state.isChatting=false;scrollChatBottom();});
clearChatFile();return;
}
state.isChatting=true;input.value='';autoResizeInput();updateCancelBtn();
appendMessage('user',question,false);scrollChatBottom();
var typing2=document.createElement('div');typing2.className='msg bot';typing2.id='typing-msg';typing2.textContent='⏳ Боб думает...';
$('chat-messages').appendChild(typing2);scrollChatBottom();
apiCall('chat',{question:question,chat_db_id:state.currentChatId}).then(function(d){
var t=$('typing-msg');if(t)t.remove();
if(d.error){appendMessage('bot','❌ '+d.error,false);state.isChatting=false;scrollChatBottom();return;}
updateBalance(d.balance);
if(d.is_file&&d.file_content)appendMessage('bot',d.answer,true,d.file_content,d.filename||'answer.txt');
else appendMessage('bot',d.answer,false);
if(d.title){state.currentChatTitle=d.title;$('chat-header-title').textContent='🤖 '+d.title;}
scrollChatBottom();state.isChatting=false;
});
}
function loadAISettings(){
if(!state.me)return;
var modes=[{key:'regular',label:'🤖 Обычный ИИ'},{key:'coder',label:'💻 Кодер (ответ файлом)'},{key:'explainer',label:'📖 Объяснятор'},{key:'translator',label:'🌍 Переводчик'}];
var el=$('chat-modes-list');el.innerHTML='';
for(var i=0;i<modes.length;i++){
(function(m){
var div=document.createElement('div');
div.className='mode-option'+(state.me.mode===m.key?' active':'');
div.innerHTML=m.label+'<span class="check">✓</span>';
div.addEventListener('click',function(){setAIMode('mode',m.key);});
el.appendChild(div);
})(modes[i]);
}
var aiModes=[{key:'regular',label:'🤖 Обычный'},{key:'smart',label:'🧠 Умный'},{key:'open',label:'💬 Откровенный'},{key:'uncensored',label:'🔥 Без цензуры'}];
var el2=$('chat-ai-modes-list');el2.innerHTML='';
for(var j=0;j<aiModes.length;j++){
(function(m){
var div=document.createElement('div');
div.className='mode-option'+(state.me.ai_mode===m.key?' active':'');
div.innerHTML=m.label+'<span class="check">✓</span>';
div.addEventListener('click',function(){setAIMode('ai_mode',m.key);});
el2.appendChild(div);
})(aiModes[j]);
}
}
function setAIMode(field,value){
apiCall('settings',{field:field,value:value}).then(function(d){
if(d.error){try{tg.showAlert('❌ '+d.error);}catch(e){}return;}
state.me[field]=value;
try{tg.HapticFeedback.impactOccurred('light');}catch(e){}
loadAISettings();updateChatBadges();
});
}
function loadHistory(kind,tabEl){
var tabs=document.querySelectorAll('#screen-history .tab');
for(var i=0;i<tabs.length;i++)tabs[i].classList.remove('active');
if(tabEl)tabEl.classList.add('active');
var c=$('history-content');c.innerHTML='<div class="loading">Загрузка...</div>';
apiCall('history/image',{kind:kind}).then(function(d){
if(d.error){c.innerHTML='<div class="err">Ошибка: '+escapeHtml(d.error)+'</div>';return;}
if(!d.items||d.items.length===0){c.innerHTML='<div style="text-align:center;padding:50px 20px;opacity:0.4;">История пуста</div>';return;}
var html='<div class="history-grid">';
for(var i=0;i<d.items.length;i++){
var it=d.items[i];
html+='<div class="history-item"><img src="'+it.url+'" loading="lazy" data-url="'+it.url+'" class="hist-img"><div class="cap">'+escapeHtml((it.prompt||'').substring(0,40))+'</div></div>';
}
html+='</div>';
c.innerHTML=html;
var imgs=c.querySelectorAll('.hist-img');
for(var j=0;j<imgs.length;j++)imgs[j].addEventListener('click',function(){var url=this.getAttribute('data-url');try{tg.openLink(url);}catch(e){}});
});
}
function buyTokens(amount,tokens){
try{tg.showConfirm('Купить '+tokens+' токенов за '+amount+' ₽?',function(ok){
if(!ok)return;
apiCall('pay',{amount:amount,tokens:tokens}).then(function(d){
if(d.error){try{tg.showAlert('❌ '+d.error);}catch(e){}return;}
try{tg.openLink(d.pay_url);}catch(e){}
});
});}catch(e){}
}
function loadTickets(){
apiCall('tickets/list').then(function(d){
var c=$('support-tickets');
if(d.error){c.innerHTML='<div class="err">Ошибка</div>';return;}
if(!d.items||d.items.length===0){c.innerHTML='<div style="text-align:center;padding:20px;opacity:0.5;font-size:14px;">Тикетов пока нет</div>';return;}
var html='';
for(var i=0;i<d.items.length;i++){
var t=d.items[i];
var st=t.status==='open'?'open':'done';
var stLabel=t.status==='open'?'⏳ Открыт':'✅ Отвечен';
html+='<div class="ticket-card"><div class="head"><b>#'+t.id+'</b><span class="status '+st+'">'+stLabel+'</span></div><div class="msg">'+escapeHtml(t.message)+'</div>';
if(t.answer)html+='<div class="answer">💬 '+escapeHtml(t.answer)+'</div>';
html+='</div>';
}
c.innerHTML=html;
});
}
function sendSupport(){
var msg=($('support-msg').value||'').trim();
if(!msg){try{tg.showAlert('❌ Введи сообщение');}catch(e){}return;}
var btn=$('support-send-btn');btn.disabled=true;btn.textContent='⏳ Отправка...';
apiCall('tickets/create',{message:msg}).then(function(d){
btn.disabled=false;btn.textContent='📩 Отправить тикет';
if(d.error){try{tg.showAlert('❌ '+d.error);}catch(e){}return;}
$('support-msg').value='';
try{tg.showAlert('✅ Тикет #'+d.ticket_id+' создан!');}catch(e){}
loadTickets();
});
}
$('btn-open-generate').addEventListener('click',function(){showScreen('generate');});
$('btn-open-edit').addEventListener('click',function(){showScreen('edit');});
$('btn-open-chat').addEventListener('click',function(){showScreen('chat');});
$('btn-open-history').addEventListener('click',function(){showScreen('history');});
$('btn-open-buy').addEventListener('click',function(){showScreen('buy');});
$('btn-open-support').addEventListener('click',function(){showScreen('support');});
$('btn-back-gen').addEventListener('click',function(){showScreen('main');});
$('btn-back-edit').addEventListener('click',function(){showScreen('main');});
$('btn-back-chat').addEventListener('click',function(){showScreen('main');});
$('btn-back-chats').addEventListener('click',function(){showScreen('chat');});
$('btn-back-hist').addEventListener('click',function(){showScreen('main');});
$('btn-back-buy').addEventListener('click',function(){showScreen('main');});
$('btn-back-sup').addEventListener('click',function(){showScreen('main');});
$('btn-back-ai-set').addEventListener('click',function(){showScreen('chat');});
$('btn-chat-settings').addEventListener('click',function(){showScreen('ai-settings');});
$('btn-open-chats').addEventListener('click',function(){showScreen('chats');});
$('btn-new-chat2').addEventListener('click',createNewChat);
$('gen-btn').addEventListener('click',doGenerate);
$('edit-btn').addEventListener('click',doEdit);
$('edit-file-input').addEventListener('change',function(){editFileSelected(this);});
$('chat-send-btn').addEventListener('click',doChat);
$('chat-attach-btn').addEventListener('click',chatAttachClick);
$('att-remove').addEventListener('click',clearChatFile);
$('chat-cancel-btn').addEventListener('click',cancelInput);
$('chat-input').addEventListener('input',function(){autoResizeInput();updateCancelBtn();});
$('chat-input').addEventListener('keydown',function(ev){if(ev.key==='Enter'&&!ev.shiftKey){ev.preventDefault();doChat();}});
$('tab-hist-gen').addEventListener('click',function(){loadHistory('gen',this);});
$('tab-hist-edit').addEventListener('click',function(){loadHistory('edit',this);});
$('btn-modal-stay').addEventListener('click',closeExitModal);
$('btn-modal-exit').addEventListener('click',function(){closeExitModal();try{tg.close();}catch(e){}});
$('support-send-btn').addEventListener('click',sendSupport);
$('buy-custom-btn').addEventListener('click',function(){
var n=parseInt($('custom-tokens').value||'0',10);
if(isNaN(n)||n<20||n>3000){try{tg.showAlert('❌ Введи от 20 до 3000');}catch(e){}return;}
buyTokens(n*5,n);
});
var buyOpts=document.querySelectorAll('.buy-option');
for(var k=0;k<buyOpts.length;k++){
buyOpts[k].addEventListener('click',function(){
var amt=parseInt(this.getAttribute('data-amount'),10);
var tks=parseInt(this.getAttribute('data-tokens'),10);
buyTokens(amt,tks);
});
}
loadMe();updateBackButton();
setInterval(function(){if(state.currentScreen==='main')loadMe();},60000);
})();
</script>
</body>
</html>'''


@app.route('/webapp/')
@app.route('/webapp')
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


def run_flask():
    import logging
    logging.basicConfig(level=logging.INFO, format='[FLASK] %(asctime)s %(levelname)s %(message)s')
    port = int(os.getenv('PORT', 3000))
    print("[FLASK] ============================================", flush=True)
    print(f"[FLASK] Starting on 0.0.0.0:{port}", flush=True)
    print(f"[FLASK] PID={os.getpid()}", flush=True)
    print("[FLASK] ============================================", flush=True)
    try:
        app.run(host='0.0.0.0', port=port, threaded=True, debug=False, use_reloader=False)
    except OSError as e:
        print(f"[FLASK] OSError: {e}", flush=True)
        if "Address already in use" in str(e):
            print(f"[FLASK] Порт {port} занят, пробую {port + 1}", flush=True)
            try:
                app.run(host='0.0.0.0', port=port + 1, threaded=True, debug=False, use_reloader=False)
            except Exception as e2:
                print(f"[FLASK] CRASHED again: {e2}", flush=True)
                import traceback
                traceback.print_exc()
        else:
            import traceback
            traceback.print_exc()
    except Exception as e:
        print(f"[FLASK] CRASHED: {e}", flush=True)
        import traceback
        traceback.print_exc()


def start_background_workers():
    print("[WORKERS] Starting background threads...", flush=True)
    workers = [
        ("send_startup_report", send_startup_report),
        ("maintenance_worker", maintenance_worker),
        ("cleanup_orders_worker", cleanup_orders_worker),
        ("cleanup_active_operations", cleanup_active_operations),
        ("cleanup_errors_log", cleanup_errors_log),
        ("backup_worker", backup_worker),
    ]
    for name, fn in workers:
        try:
            threading.Thread(target=fn, daemon=True, name=name).start()
            print(f"[WORKERS] + {name}", flush=True)
        except Exception as e:
            print(f"[WORKERS] x {name}: {e}", flush=True)
    print("[WORKERS] All background threads started", flush=True)


def run_bot_polling():
    print("[BOT] ============================================", flush=True)
    print("[BOT] Starting polling...", flush=True)
    print("[BOT] ============================================", flush=True)
    while True:
        try:
            bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
        except Exception as e:
            log_error(f"Polling error: {e}")
            print(f"[BOT] Polling error: {e}", flush=True)
            time.sleep(5)


print("[BOOT] ============================================", flush=True)
print("[BOOT] Initializing bot...", flush=True)
print("[BOOT] ============================================", flush=True)

threading.Thread(target=run_flask, daemon=True, name="flask").start()
print("[BOOT] Flask thread launched", flush=True)

start_background_workers()

threading.Thread(target=run_bot_polling, daemon=True, name="bot_polling").start()
print("[BOOT] Bot polling thread launched", flush=True)

print("[BOOT] ============================================", flush=True)
print("[BOOT] All systems started.", flush=True)
print("[BOOT] ============================================", flush=True)


if __name__ == '__main__':
    while True:
        time.sleep(60)
