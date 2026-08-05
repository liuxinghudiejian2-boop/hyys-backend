"""
数据库模块 — 双引擎支持 (SQLite / PostgreSQL)

环境变量 DATABASE_URL 存在时使用 PostgreSQL（生产，Render 免费数据库，持久化不随部署丢失）
否则回退到本地 SQLite（开发调试）

所有对外函数签名保持不变，app.py 无需改动。
"""

import os
import time
import json
import hashlib
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "app.db")

# 判断是否使用 PostgreSQL
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

_lock = threading.Lock()

# psycopg2 连接池（懒加载）
_pg_conn = None


def _now():
    """当前 Unix 时间戳（秒）"""
    return int(time.time())


def _hash_password(pwd):
    """密码哈希（与 app.py 保持一致）"""
    return hashlib.sha256(f"mg_salt_{pwd}".encode()).hexdigest()


# ============================== 连接管理 ==============================

def _get_db():
    """获取数据库连接（线程安全）。返回 (conn, is_postgres)"""
    if USE_POSTGRES:
        import psycopg2
        global _pg_conn
        if _pg_conn is None or _pg_conn.closed:
            _pg_conn = psycopg2.connect(DATABASE_URL)
            _pg_conn.autocommit = False
        return _pg_conn, True
    else:
        import sqlite3
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn, False


def _sql(query, is_pg):
    """根据数据库类型转换 SQL 语法"""
    if not is_pg:
        return query
    # SQLite -> PostgreSQL 占位符
    query = query.replace("?", "%s")
    # INSERT OR IGNORE -> ON CONFLICT DO NOTHING
    query = query.replace("INSERT OR IGNORE", "INSERT INTO")
    return query


def _commit(conn, is_pg):
    conn.commit()


def _close(conn, is_pg):
    # PostgreSQL 复用连接；SQLite 每次关闭
    if not is_pg:
        conn.close()


def _cur_fetchall(cur, is_pg):
    """兼容 fetchall：psycopg2 返回 tuple 行，需要转 dict"""
    if is_pg:
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    else:
        return [dict(r) for r in cur.fetchall()]


def _cur_fetchone(cur, is_pg):
    """兼容 fetchone"""
    if is_pg:
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description] if cur.description else []
        return dict(zip(cols, row))
    else:
        r = cur.fetchone()
        return dict(r) if r else None


# ============================== 建表 ==============================

def _init_db():
    """初始化数据库表结构（PostgreSQL 用 SERIAL 替代 AUTOINCREMENT）"""
    conn, is_pg = _get_db()
    cur = conn.cursor()

    gallery_ddl = """
        CREATE TABLE IF NOT EXISTS gallery (
            id BIGINT PRIMARY KEY,
            title TEXT, s TEXT, grad INTEGER DEFAULT 0,
            url TEXT DEFAULT '', srcTag TEXT DEFAULT '',
            ratio TEXT DEFAULT '', nickname TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            intro TEXT DEFAULT '',
            height TEXT DEFAULT '', age TEXT DEFAULT '',
            job TEXT DEFAULT '', hobby TEXT DEFAULT '',
            bwh TEXT DEFAULT '',
            created_at BIGINT DEFAULT 0
        )
    """
    inbox_ddl = """
        CREATE TABLE IF NOT EXISTS inbox (
            id BIGINT PRIMARY KEY,
            title TEXT DEFAULT '美图',
            grad INTEGER DEFAULT 0,
            url TEXT DEFAULT '',
            inbox_from TEXT DEFAULT '用户',
            username TEXT DEFAULT '',
            created_at BIGINT DEFAULT 0
        )
    """
    users_ddl = """
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT,
            role TEXT DEFAULT 'user',
            name TEXT DEFAULT '',
            last_login_at BIGINT DEFAULT 0
        )
    """
    chat_ddl = """
        CREATE TABLE IF NOT EXISTS chat (
            id %s PRIMARY KEY,
            username TEXT,
            sender TEXT,
            text TEXT,
            msg_time BIGINT,
            created_at BIGINT DEFAULT 0
        )
    """ % ("SERIAL" if is_pg else "INTEGER")

    inbox_state_ddl = """
        CREATE TABLE IF NOT EXISTS inbox_read_state (
            id INTEGER PRIMARY KEY,
            last_read_at BIGINT DEFAULT 0
        )
    """

    cur.execute(_sql(gallery_ddl, is_pg))
    cur.execute(_sql(inbox_ddl, is_pg))
    cur.execute(_sql(users_ddl, is_pg))
    cur.execute(_sql(chat_ddl, is_pg))
    cur.execute(_sql(inbox_state_ddl, is_pg))

    # 初始化收件箱未读状态（id=1 唯一记录）
    if is_pg:
        cur.execute("INSERT INTO inbox_read_state (id, last_read_at) VALUES (1, 0) ON CONFLICT (id) DO NOTHING")
    else:
        cur.execute("INSERT OR IGNORE INTO inbox_read_state (id, last_read_at) VALUES (1, 0)")

    # 兼容旧数据库：users 表添加 last_login_at（SQLite 才需要；PostgreSQL 建表已包含）
    if not is_pg:
        try:
            cur.execute("ALTER TABLE users ADD COLUMN last_login_at INTEGER DEFAULT 0")
        except Exception:
            pass

    # 已有 user 记录设置默认 last_login_at
    if is_pg:
        cur.execute(
            "UPDATE users SET last_login_at = %s WHERE last_login_at IS NULL OR last_login_at = 0",
            (_now(),),
        )
    else:
        cur.execute(
            "UPDATE users SET last_login_at = strftime('%s','now') WHERE last_login_at IS NULL OR last_login_at = 0"
        )

    # 确保客服账号存在
    staff_account = "12356789"
    staff_password = "11111111"
    if is_pg:
        cur.execute(
            "INSERT INTO users (username, password, role, name, last_login_at) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (username) DO NOTHING",
            (staff_account, _hash_password(staff_password), "staff", "客服", _now()),
        )
    else:
        cur.execute(
            "INSERT OR IGNORE INTO users (username, password, role, name) VALUES (?, ?, ?, ?)",
            (staff_account, _hash_password(staff_password), "staff", "客服"),
        )
    # 客服 last_login_at 兜底
    cur.execute(_sql(
        "UPDATE users SET last_login_at = %s WHERE username = %s AND (last_login_at IS NULL OR last_login_at = 0)" if is_pg
        else "UPDATE users SET last_login_at = strftime('%s','now') WHERE username = ? AND (last_login_at IS NULL OR last_login_at = 0)",
        is_pg,
    ), (_now(), staff_account) if is_pg else (staff_account,))

    _commit(conn, is_pg)
    _close(conn, is_pg)


# ============================== 图库 ==============================

def get_gallery():
    conn, pg = _get_db()
    cur = conn.cursor()
    cur.execute(_sql("SELECT * FROM gallery ORDER BY created_at DESC", pg))
    rows = _cur_fetchall(cur, pg)
    _close(conn, pg)
    return rows


def add_gallery_item(item):
    conn, pg = _get_db()
    cur = conn.cursor()
    tags = item.get("tags")
    tags_json = json.dumps(tags, ensure_ascii=False) if isinstance(tags, list) else "[]"
    cur.execute(_sql(
        """INSERT INTO gallery
           (id, title, s, grad, url, srcTag, ratio, nickname, tags, intro,
            height, age, job, hobby, bwh, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", pg),
        (
            item.get("id"), item.get("title", "美图"), item.get("s", "美图"),
            int(item.get("grad", 0)), item.get("url", ""), item.get("srcTag", ""),
            item.get("ratio", ""), item.get("nickname", ""), tags_json,
            item.get("intro", ""), item.get("height", ""), item.get("age", ""),
            item.get("job", ""), item.get("hobby", ""), item.get("bwh", ""),
            _now(),
        )
    )
    _commit(conn, pg)
    _close(conn, pg)


def delete_gallery_item(item_id):
    conn, pg = _get_db()
    cur = conn.cursor()
    cur.execute(_sql("DELETE FROM gallery WHERE id = ?", pg), (item_id,))
    _commit(conn, pg)
    deleted = cur.rowcount
    _close(conn, pg)
    return deleted


# ============================== 收件箱 ==============================

def get_inbox():
    conn, pg = _get_db()
    cur = conn.cursor()
    cur.execute(_sql("SELECT * FROM inbox ORDER BY created_at DESC", pg))
    rows = _cur_fetchall(cur, pg)
    _close(conn, pg)
    return rows


def add_inbox_items(items):
    conn, pg = _get_db()
    cur = conn.cursor()
    base = int(time.time() * 1000)
    for i, it in enumerate(items):
        cur.execute(_sql(
            """INSERT INTO inbox (id, title, grad, url, inbox_from, username, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""", pg),
            (
                base + i, it.get("title", "美图"), int(it.get("grad", 0)),
                it.get("url", ""), it.get("from", "用户"), it.get("username", ""),
                _now(),
            )
        )
    _commit(conn, pg)
    _close(conn, pg)


def delete_inbox_by_username(username):
    conn, pg = _get_db()
    cur = conn.cursor()
    cur.execute(_sql("DELETE FROM inbox WHERE username = ?", pg), (username,))
    _commit(conn, pg)
    deleted = cur.rowcount
    _close(conn, pg)
    return deleted


def delete_inbox_by_id(item_id):
    conn, pg = _get_db()
    cur = conn.cursor()
    cur.execute(_sql("DELETE FROM inbox WHERE id = ?", pg), (item_id,))
    _commit(conn, pg)
    deleted = cur.rowcount
    _close(conn, pg)
    return deleted


# ============================== 用户 ==============================

def get_user(username):
    conn, pg = _get_db()
    cur = conn.cursor()
    cur.execute(_sql("SELECT * FROM users WHERE username = ?", pg), (username,))
    row = _cur_fetchone(cur, pg)
    _close(conn, pg)
    return row


def create_user(username, password, role="user"):
    conn, pg = _get_db()
    cur = conn.cursor()
    now = _now()
    cur.execute(_sql(
        "INSERT INTO users (username, password, role, name, last_login_at) VALUES (?, ?, ?, ?, ?)", pg),
        (username, password, role, username, now)
    )
    _commit(conn, pg)
    _close(conn, pg)


def update_login_time(username):
    conn, pg = _get_db()
    cur = conn.cursor()
    cur.execute(_sql("UPDATE users SET last_login_at = ? WHERE username = ?", pg),
                (_now(), username))
    _commit(conn, pg)
    _close(conn, pg)


def get_all_users():
    conn, pg = _get_db()
    cur = conn.cursor()
    cur.execute(_sql(
        "SELECT username, role, name, last_login_at FROM users ORDER BY last_login_at DESC", pg))
    rows = _cur_fetchall(cur, pg)
    _close(conn, pg)
    return rows


def cleanup_inactive_users(days=30):
    cutoff = _now() - days * 86400
    conn, pg = _get_db()
    cur = conn.cursor()
    # 先查出要被删除的用户（排除客服）
    cur.execute(_sql("SELECT username FROM users WHERE role != 'staff' AND last_login_at < ?", pg), (cutoff,))
    to_delete = [r["username"] for r in _cur_fetchall(cur, pg)]

    if to_delete:
        placeholders = ",".join(["%s"] * len(to_delete) if pg else ["?"] * len(to_delete))
        cur.execute(_sql(f"DELETE FROM chat WHERE username IN ({placeholders})", pg), to_delete)
        cur.execute(_sql(f"DELETE FROM inbox WHERE username IN ({placeholders})", pg), to_delete)
        cur.execute(_sql("DELETE FROM users WHERE role != 'staff' AND last_login_at < ?", pg), (cutoff,))
        _commit(conn, pg)

    _close(conn, pg)
    return to_delete


def ensure_staff():
    staff_account = "12356789"
    staff_password = "11111111"
    conn, pg = _get_db()
    cur = conn.cursor()
    if pg:
        cur.execute(
            "INSERT INTO users (username, password, role, name, last_login_at) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (username) DO NOTHING",
            (staff_account, _hash_password(staff_password), "staff", "客服", _now()),
        )
    else:
        cur.execute(
            "INSERT OR IGNORE INTO users (username, password, role, name) VALUES (?, ?, ?, ?)",
            (staff_account, _hash_password(staff_password), "staff", "客服"),
        )
    _commit(conn, pg)
    _close(conn, pg)


# ============================== 聊天 ==============================

def get_chat(username):
    conn, pg = _get_db()
    cur = conn.cursor()
    cur.execute(_sql("SELECT * FROM chat WHERE username = ? ORDER BY msg_time ASC", pg), (username,))
    rows = _cur_fetchall(cur, pg)
    _close(conn, pg)
    return rows


def add_chat_message(username, sender, text, msg_time):
    conn, pg = _get_db()
    cur = conn.cursor()
    if pg:
        cur.execute(
            "INSERT INTO chat (username, sender, text, msg_time, created_at) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (username, sender, text, msg_time, _now()),
        )
        row = cur.fetchone()
        msg_id = row[0] if row else msg_time
    else:
        cur.execute(
            "INSERT INTO chat (username, sender, text, msg_time, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, sender, text, msg_time, _now()),
        )
        msg_id = cur.lastrowid
    _commit(conn, pg)
    _close(conn, pg)
    return msg_id


def get_chat_partners():
    conn, pg = _get_db()
    cur = conn.cursor()
    cur.execute(_sql("SELECT DISTINCT username FROM chat ORDER BY username", pg))
    rows = _cur_fetchall(cur, pg)
    _close(conn, pg)
    return [r["username"] for r in rows]


# ============================== 收件箱未读状态 ==============================

def get_inbox_last_read():
    conn, pg = _get_db()
    cur = conn.cursor()
    cur.execute(_sql("SELECT last_read_at FROM inbox_read_state WHERE id = 1", pg))
    row = _cur_fetchone(cur, pg)
    _close(conn, pg)
    return row["last_read_at"] if row else 0


def set_inbox_last_read(ts):
    conn, pg = _get_db()
    cur = conn.cursor()
    if pg:
        cur.execute(
            "INSERT INTO inbox_read_state (id, last_read_at) VALUES (1, %s) "
            "ON CONFLICT (id) DO UPDATE SET last_read_at = %s", (ts, ts))
    else:
        cur.execute("UPDATE inbox_read_state SET last_read_at = ? WHERE id = 1", (ts,))
    _commit(conn, pg)
    _close(conn, pg)


def get_inbox_unread_count():
    last_read = get_inbox_last_read()
    conn, pg = _get_db()
    cur = conn.cursor()
    if last_read == 0:
        cur.execute(_sql("SELECT COUNT(*) as cnt FROM inbox", pg))
    else:
        cur.execute(_sql("SELECT COUNT(*) as cnt FROM inbox WHERE created_at > ?", pg), (last_read,))
    row = _cur_fetchone(cur, pg)
    _close(conn, pg)
    return row["cnt"] if row else 0


# ============================== 初始化 ==============================

_init_db()
