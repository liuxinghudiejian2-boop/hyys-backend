"""
数据库模块 — 使用 SQLite 替代 JSON 文件存储
解决 Render 冷启动时 GitHub 旧版本覆盖本地数据的问题

SQLite 数据库文件存储在 Render 持久化磁盘上，冷启动不会丢失。
"""

import os
import sqlite3
import json
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "app.db")

_lock = threading.Lock()


def _get_db():
    """获取数据库连接（线程安全）"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    """初始化数据库表结构"""
    conn = _get_db()
    cur = conn.cursor()

    # 图库
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gallery (
            id INTEGER PRIMARY KEY,
            title TEXT, s TEXT, grad INTEGER DEFAULT 0,
            url TEXT DEFAULT '', srcTag TEXT DEFAULT '',
            ratio TEXT DEFAULT '', nickname TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            intro TEXT DEFAULT '',
            height TEXT DEFAULT '', age TEXT DEFAULT '',
            job TEXT DEFAULT '', hobby TEXT DEFAULT '',
            bwh TEXT DEFAULT '',
            created_at INTEGER DEFAULT (strftime('%s','now'))
        )
    """)

    # 收件箱
    cur.execute("""
        CREATE TABLE IF NOT EXISTS inbox (
            id INTEGER PRIMARY KEY,
            title TEXT DEFAULT '美图',
            grad INTEGER DEFAULT 0,
            url TEXT DEFAULT '',
            inbox_from TEXT DEFAULT '用户',
            username TEXT DEFAULT '',
            created_at INTEGER DEFAULT (strftime('%s','now'))
        )
    """)

    # 用户
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT,
            role TEXT DEFAULT 'user',
            name TEXT DEFAULT ''
        )
    """)

    # 聊天记录
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            sender TEXT,
            text TEXT,
            msg_time INTEGER,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        )
    """)

    # 收件箱未读状态（客服最后查看收件箱的时间）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS inbox_read_state (
            id INTEGER PRIMARY KEY,
            last_read_at INTEGER DEFAULT 0
        )
    """)
    # 初始化（id=1 的唯一记录）
    cur.execute("INSERT OR IGNORE INTO inbox_read_state (id, last_read_at) VALUES (1, 0)")

    # 数据库迁移：为 users 表添加 last_login_at 字段（兼容已有数据库）
    try:
        cur.execute("ALTER TABLE users ADD COLUMN last_login_at INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # 列已存在

    # 为已有 user 记录设置默认 last_login_at（设为当前时间）
    cur.execute(
        "UPDATE users SET last_login_at = strftime('%s','now') WHERE last_login_at IS NULL OR last_login_at = 0"
    )

    # 确保客服账号存在
    import hashlib

    def hash_password(pwd):
        return hashlib.sha256(f"mg_salt_{pwd}".encode()).hexdigest()

    staff_account = "12356789"
    staff_password = "11111111"
    cur.execute(
        "INSERT OR IGNORE INTO users (username, password, role, name) VALUES (?, ?, ?, ?)",
        (staff_account, hash_password(staff_password), "staff", "客服")
    )
    # 确保客服账号 last_login_at 也有值
    cur.execute(
        "UPDATE users SET last_login_at = strftime('%s','now') WHERE username = ? AND (last_login_at IS NULL OR last_login_at = 0)",
        (staff_account,)
    )

    conn.commit()
    conn.close()


# ============================== 图库 ==============================

def get_gallery():
    """获取图库列表"""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM gallery ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_gallery_item(item):
    """添加图库条目"""
    conn = _get_db()
    cur = conn.cursor()
    tags = item.get("tags")
    tags_json = json.dumps(tags, ensure_ascii=False) if isinstance(tags, list) else "[]"
    cur.execute(
        """INSERT INTO gallery
           (id, title, s, grad, url, srcTag, ratio, nickname, tags, intro,
            height, age, job, hobby, bwh)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item.get("id"), item.get("title", "美图"), item.get("s", "美图"),
            int(item.get("grad", 0)), item.get("url", ""), item.get("srcTag", ""),
            item.get("ratio", ""), item.get("nickname", ""), tags_json,
            item.get("intro", ""), item.get("height", ""), item.get("age", ""),
            item.get("job", ""), item.get("hobby", ""), item.get("bwh", ""),
        )
    )
    conn.commit()
    conn.close()


def delete_gallery_item(item_id):
    """删除图库条目"""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM gallery WHERE id = ?", (item_id,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted


# ============================== 收件箱 ==============================

def get_inbox():
    """获取收件箱列表"""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM inbox ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_inbox_items(items):
    """批量添加收件箱条目"""
    conn = _get_db()
    cur = conn.cursor()
    base = int(__import__("time").time() * 1000)
    for i, it in enumerate(items):
        cur.execute(
            """INSERT INTO inbox (id, title, grad, url, inbox_from, username)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                base + i, it.get("title", "美图"), int(it.get("grad", 0)),
                it.get("url", ""), it.get("from", "用户"), it.get("username", ""),
            )
        )
    conn.commit()
    conn.close()


def delete_inbox_by_username(username):
    """按用户名删除收件箱条目"""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM inbox WHERE username = ?", (username,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted


def delete_inbox_by_id(item_id):
    """按ID删除收件箱条目"""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM inbox WHERE id = ?", (item_id,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted


# ============================== 用户 ==============================

def get_user(username):
    """获取单个用户"""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def create_user(username, password, role="user"):
    """创建用户，同时记录注册时间（即首次登录时间）"""
    conn = _get_db()
    cur = conn.cursor()
    import time
    now = int(time.time())
    cur.execute(
        "INSERT INTO users (username, password, role, name, last_login_at) VALUES (?, ?, ?, ?, ?)",
        (username, password, role, username, now)
    )
    conn.commit()
    conn.close()


def update_login_time(username):
    """更新用户最后登录时间"""
    conn = _get_db()
    cur = conn.cursor()
    import time
    cur.execute(
        "UPDATE users SET last_login_at = ? WHERE username = ?",
        (int(time.time()), username)
    )
    conn.commit()
    conn.close()


def get_all_users():
    """获取所有用户列表（客服用，查看注册用户及其活跃情况）"""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT username, role, name, last_login_at FROM users ORDER BY last_login_at DESC"
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cleanup_inactive_users(days=30):
    """删除超过指定天数未登录的用户（客服账号除外）
    返回被删除的用户名列表"""
    import time
    cutoff = int(time.time()) - days * 86400
    conn = _get_db()
    cur = conn.cursor()
    # 先查出要被删除的用户（排除客服）
    cur.execute(
        "SELECT username FROM users WHERE role != 'staff' AND last_login_at < ?",
        (cutoff,)
    )
    to_delete = [r["username"] for r in cur.fetchall()]

    if to_delete:
        # 删除这些用户的聊天记录
        placeholders = ",".join(["?"] * len(to_delete))
        cur.execute(f"DELETE FROM chat WHERE username IN ({placeholders})", to_delete)
        # 删除这些用户的收件箱记录
        cur.execute(f"DELETE FROM inbox WHERE username IN ({placeholders})", to_delete)
        # 删除用户
        cur.execute(f"DELETE FROM users WHERE role != 'staff' AND last_login_at < ?", (cutoff,))
        conn.commit()

    conn.close()
    return to_delete


def ensure_staff():
    """确保客服账号存在"""
    import hashlib

    def hash_password(pwd):
        return hashlib.sha256(f"mg_salt_{pwd}".encode()).hexdigest()

    staff_account = "12356789"
    staff_password = "11111111"
    conn = _get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (username, password, role, name) VALUES (?, ?, ?, ?)",
        (staff_account, hash_password(staff_password), "staff", "客服")
    )
    conn.commit()
    conn.close()


# ============================== 聊天 ==============================

def get_chat(username):
    """获取某用户的聊天记录"""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM chat WHERE username = ? ORDER BY msg_time ASC",
        (username,)
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_chat_message(username, sender, text, msg_time):
    """添加聊天消息"""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chat (username, sender, text, msg_time) VALUES (?, ?, ?, ?)",
        (username, sender, text, msg_time)
    )
    msg_id = cur.lastrowid
    conn.commit()
    conn.close()
    return msg_id


def get_chat_partners():
    """返回 chat 表中所有不同的 username（客服用，查看哪些用户发过消息）"""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT username FROM chat ORDER BY username")
    rows = cur.fetchall()
    conn.close()
    return [r["username"] for r in rows]


# ============================== 收件箱未读状态 ==============================

def get_inbox_last_read():
    """获取客服上次查看收件箱的时间戳"""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("SELECT last_read_at FROM inbox_read_state WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    return row["last_read_at"] if row else 0


def set_inbox_last_read(ts):
    """更新客服查看收件箱的时间戳"""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("UPDATE inbox_read_state SET last_read_at = ? WHERE id = 1", (ts,))
    conn.commit()
    conn.close()


def get_inbox_unread_count():
    """返回收件箱中 created_at > last_read_at 的条目数量"""
    last_read = get_inbox_last_read()
    conn = _get_db()
    cur = conn.cursor()
    if last_read == 0:
        # 首次访问，返回总条目数（全部视为未读）
        cur.execute("SELECT COUNT(*) as cnt FROM inbox")
    else:
        cur.execute("SELECT COUNT(*) as cnt FROM inbox WHERE created_at > ?", (last_read,))
    row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ============================== 初始化 ==============================

_init_db()
