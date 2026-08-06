#!/usr/bin/env python3
"""
欢悦夜赏 后端服务
=============================================
核心能力：
  - 图库 / 收件箱 / 聊天 / 用户管理（PostgreSQL 持久化）
  - 图片上传中转（ImgBB 直连优先，多图床回退）
  - 图片服务端 LRU 缓存代理（解决国内访问图床失败）

启动：gunicorn app:app（Render 用），或 python app.py（本地）
依赖：flask requests psycopg2-binary
"""

import os
import re
import json
import time
import hashlib
import traceback
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, Response

import db


def hash_password(pwd):
    """密码哈希（sha256 + 固定盐）"""
    return hashlib.sha256(f"mg_salt_{pwd}".encode()).hexdigest()


app = Flask(__name__)


# ============================== CORS ==============================

@app.after_request
def add_cors_headers(resp):
    """给所有响应添加 CORS 头，允许前端跨域调用"""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


# ============================== 配置 ==============================

# 客服图库写操作密码
STAFF_PASSWORD = os.environ.get("STAFF_PASSWORD", "11111111")


class Config:
    # 通用请求 UA 与超时
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    TIMEOUT = 15

    # 图片代理浏览器缓存（秒）—— 24 小时，浏览器二次访问直接命中本地缓存
    IMAGE_CACHE_TIME = 86400

    # 图片代理服务端 LRU 缓存（同一 URL 只回源一次）
    IMG_SERVER_CACHE = {}
    IMG_SERVER_CACHE_MAX = 200          # 最多缓存的图片条目
    IMG_SERVER_CACHE_TTL = 6 * 3600     # 服务端缓存有效期 6 小时
    IMG_CACHE_HITS = 0
    IMG_CACHE_MISS = 0
    IMG_MAX_BYTES = 15 * 1024 * 1024    # 单张图片代理上限 15MB

    # 用户清理：30 天未登录自动清除
    USER_CLEANUP_DAYS = 30
    LAST_CLEANUP_TIME = 0


# 允许代理的图床域名白名单（SSRF 防护）
IMG_CACHE_ALLOWED_DOMAINS = (
    "catbox.moe", "litter.catbox.moe",
    "freeimage.host", "i.imgur.com", "imgur.com",
    "imgbb.com", "i.ibb.co",
    "sm.ms", "postimg.cc", "i.postimg.cc",
    "img.vim-cn.com", "upload.cc", "0x0.st", "tmpfiles.org",
)


# ============================== 工具 ==============================

def create_session():
    """创建带浏览器 UA 的 requests 会话（用于图床拉取）"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": Config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return session


# ============================== 图片代理服务端缓存（LRU） ==============================

def _img_cache_get(key):
    """从服务端缓存读取图片；命中更新 LRU，过期删除"""
    entry = Config.IMG_SERVER_CACHE.get(key)
    if not entry:
        Config.IMG_CACHE_MISS += 1
        return None
    if time.time() - entry["ts"] > Config.IMG_SERVER_CACHE_TTL:
        Config.IMG_SERVER_CACHE.pop(key, None)
        Config.IMG_CACHE_MISS += 1
        return None
    entry["ts"] = time.time()
    Config.IMG_CACHE_HITS += 1
    return entry


def _img_cache_put(key, content, content_type):
    """写入服务端缓存，超限淘汰最久未访问的条目"""
    if len(Config.IMG_SERVER_CACHE) >= Config.IMG_SERVER_CACHE_MAX:
        old_key = min(Config.IMG_SERVER_CACHE, key=lambda k: Config.IMG_SERVER_CACHE[k]["ts"])
        Config.IMG_SERVER_CACHE.pop(old_key, None)
    Config.IMG_SERVER_CACHE[key] = {
        "content": content,
        "content_type": content_type,
        "etag": '"%s"' % hashlib.md5(content[:8192]).hexdigest(),
        "ts": time.time(),
    }


def _img_cache_stats():
    total = Config.IMG_CACHE_HITS + Config.IMG_CACHE_MISS
    hit_rate = round(Config.IMG_CACHE_HITS / total, 3) if total else 0
    return {
        "entries": len(Config.IMG_SERVER_CACHE),
        "max": Config.IMG_SERVER_CACHE_MAX,
        "hits": Config.IMG_CACHE_HITS,
        "miss": Config.IMG_CACHE_MISS,
        "hit_rate": hit_rate,
    }


@app.route("/api/img-cache", methods=["GET"])
def api_img_cache():
    """
    图片代理 + 服务端 LRU 缓存 + 24 小时浏览器缓存。
    后端从图床拉取图片返回给浏览器，解决国内直连图床失败。
    支持条件请求（If-None-Match -> 304）。
    """
    target_url = request.args.get("url", "")
    if not target_url:
        return "Missing 'url' parameter", 400

    # SSRF 防护：仅允许代理白名单图床域名
    parsed = urlparse(target_url)
    if not (parsed.hostname and any(parsed.hostname.endswith(d) for d in IMG_CACHE_ALLOWED_DOMAINS)):
        return f"Forbidden: domain not allowed -> {parsed.hostname}", 403

    cache_key = "img:" + hashlib.md5(target_url.encode()).hexdigest()

    # 命中服务端缓存
    cached = _img_cache_get(cache_key)
    if cached:
        etag = cached["etag"]
        if request.headers.get("If-None-Match") == etag:
            resp304 = Response(status=304)
            resp304.headers["Cache-Control"] = f"public, max-age={Config.IMAGE_CACHE_TIME}"
            resp304.headers["Access-Control-Allow-Origin"] = "*"
            resp304.headers["ETag"] = etag
            return resp304
        response = Response(cached["content"], content_type=cached["content_type"])
        response.headers["Cache-Control"] = f"public, max-age={Config.IMAGE_CACHE_TIME}"
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["ETag"] = etag
        response.headers["X-Cache"] = "HIT"
        return response

    try:
        session = create_session()
        resp = session.get(target_url, timeout=Config.TIMEOUT, stream=True)
        if resp.status_code != 200:
            resp.close()
            return f"Failed to fetch image: HTTP {resp.status_code}", 502

        content_type = resp.headers.get("Content-Type", "image/jpeg")

        # 流式读取并限制大小，防止超大图拖垮内存
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=8192):
            if not chunk:
                break
            total += len(chunk)
            if total > Config.IMG_MAX_BYTES:
                resp.close()
                return "Image too large", 413
            chunks.append(chunk)
        resp.close()
        content = b"".join(chunks)

        if not content:
            return "Empty image data", 502

        _img_cache_put(cache_key, content, content_type)

        response = Response(content, content_type=content_type)
        response.headers["Cache-Control"] = f"public, max-age={Config.IMAGE_CACHE_TIME}"
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["ETag"] = '"%s"' % hashlib.md5(content[:8192]).hexdigest()
        response.headers["X-Cache"] = "MISS"
        return response
    except requests.exceptions.Timeout:
        return "Proxy timeout", 504
    except Exception as e:
        print(f"[IMG-CACHE ERROR] {e}")
        return f"Proxy error: {e}", 500


# ============================== 健康检查 ==============================

@app.route("/api/health", methods=["GET"])
def api_health():
    """健康检查 + 自动清理不活跃用户（每天最多执行一次）"""
    _try_cleanup_users()
    return jsonify({
        "status": "ok",
        "service": "hyys-backend",
        "img_cache": _img_cache_stats(),
    })


def _try_cleanup_users():
    now = int(time.time())
    if now - Config.LAST_CLEANUP_TIME < 86400:
        return
    Config.LAST_CLEANUP_TIME = now
    try:
        deleted = db.cleanup_inactive_users(days=Config.USER_CLEANUP_DAYS)
        if deleted:
            print(f"[清理] 已删除 {len(deleted)} 个不活跃用户: {', '.join(deleted)}")
    except Exception as e:
        print(f"[清理] 出错: {e}")


# ============================== 注册 / 登录 ==============================

@app.route("/api/register", methods=["POST", "OPTIONS"])
def api_register():
    """用户注册"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    try:
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""

        if not username or not password:
            return jsonify({"success": False, "error": "请输入用户名和密码"})
        if len(username) < 2:
            return jsonify({"success": False, "error": "用户名至少 2 个字符"})
        if len(password) < 4:
            return jsonify({"success": False, "error": "密码至少 4 位"})

        if db.get_user(username):
            return jsonify({"success": False, "error": "用户名已存在"})

        db.create_user(username, hash_password(password), "user")
        return jsonify({"success": True, "message": "注册成功"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/login", methods=["POST", "OPTIONS"])
def api_login():
    """用户登录（普通用户 + 客服统一入口）"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    try:
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""

        if not username or not password:
            return jsonify({"success": False, "error": "请输入用户名和密码"})

        users = db.get_user(username)
        if not users:
            return jsonify({"success": False, "error": "用户不存在，请先注册"})
        if users["password"] != hash_password(password):
            return jsonify({"success": False, "error": "密码错误"})

        db.update_login_time(username)
        return jsonify({
            "success": True,
            "role": users["role"],
            "username": users.get("name", username),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "trace": traceback.format_exc()}), 500


def check_staff_password(data):
    """校验客服密码（图库/收件箱写操作需要）"""
    pwd = data.get("password") or ""
    return pwd == STAFF_PASSWORD


# ============================== 用户管理接口（客服用） ==============================

@app.route("/api/users", methods=["POST", "OPTIONS"])
def api_users_list():
    """获取所有注册用户列表（客服，需密码验证）"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    if not check_staff_password(data):
        return jsonify({"success": False, "error": "无权限：需要客服密码"})
    users = db.get_all_users()
    return jsonify({"success": True, "users": users})


@app.route("/api/cleanup-users", methods=["POST", "OPTIONS"])
def api_cleanup_users():
    """手动触发清理不活跃用户（客服，需密码验证）"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    if not check_staff_password(data):
        return jsonify({"success": False, "error": "无权限：需要客服密码"})
    days = data.get("days", Config.USER_CLEANUP_DAYS)
    try:
        deleted = db.cleanup_inactive_users(days=int(days))
        Config.LAST_CLEANUP_TIME = int(time.time())
        return jsonify({
            "success": True,
            "deleted": deleted,
            "count": len(deleted),
            "message": f"已清理 {len(deleted)} 个超过 {days} 天未登录的用户"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ============================== 图库接口 ==============================

@app.route("/api/gallery", methods=["GET"])
def api_gallery_list():
    """获取图库列表（所有用户共享）"""
    return jsonify({"success": True, "gallery": db.get_gallery()})


@app.route("/api/gallery", methods=["POST", "OPTIONS"])
def api_gallery_add():
    """添加图片到图库（客服，需密码）"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    if not check_staff_password(data):
        return jsonify({"success": False, "error": "无权限：需要客服密码"})
    item = {
        "id": int(time.time() * 1000) % 1000000000,
        "title": (data.get("title") or "美图").strip(),
        "s": (data.get("s") or "美图").strip(),
        "grad": int(data.get("grad", 0)),
        "url": (data.get("url") or "").strip(),
        "srcTag": (data.get("srcTag") or "").strip(),
        "ratio": (data.get("ratio") or "").strip(),
        "nickname": (data.get("nickname") or "").strip(),
        "tags": data.get("tags") if isinstance(data.get("tags"), list) else [],
        "intro": (data.get("intro") or "").strip(),
        "height": (data.get("height") or "").strip(),
        "age": (data.get("age") or "").strip(),
        "job": (data.get("job") or "").strip(),
        "hobby": (data.get("hobby") or "").strip(),
        "bwh": (data.get("bwh") or "").strip(),
    }
    db.add_gallery_item(item)
    return jsonify({"success": True, "item": item})


@app.route("/api/gallery/<int:item_id>", methods=["DELETE", "OPTIONS"])
def api_gallery_delete(item_id):
    """删除图库图片（客服，需密码）"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    pwd = request.args.get("password") or ""
    if pwd != STAFF_PASSWORD:
        return jsonify({"success": False, "error": "无权限：需要客服密码"})
    deleted = db.delete_gallery_item(item_id)
    if not deleted:
        return jsonify({"success": False, "error": "图片不存在"})
    return jsonify({"success": True})


# ============================== 收件箱接口 ==============================

@app.route("/api/inbox/unread-count", methods=["GET"])
def api_inbox_unread_count():
    """返回收件箱未读条目数量（客服用）"""
    count = db.get_inbox_unread_count()
    return jsonify({"success": True, "unread": count})


@app.route("/api/inbox/mark-read", methods=["POST", "OPTIONS"])
def api_inbox_mark_read():
    """标记收件箱已读（客服点开收件箱时调用）"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    db.set_inbox_last_read(int(time.time()))
    return jsonify({"success": True})


@app.route("/api/inbox", methods=["GET"])
def api_inbox_list():
    """获取收件箱列表（客服查看用户上传）"""
    inbox = db.get_inbox()
    unread = db.get_inbox_unread_count()
    return jsonify({"success": True, "inbox": inbox, "unread": unread})


@app.route("/api/inbox", methods=["POST", "OPTIONS"])
def api_inbox_add():
    """用户一键上传图片给客服"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    db.add_inbox_items(items)
    return jsonify({"success": True, "added": len(items)})


def _extract_username(item):
    """从收件箱条目提取用户名（兼容新旧字段名）"""
    if item.get("username"):
        return item["username"]
    from_str = item.get("inbox_from", "") or item.get("from", "")
    parts = from_str.split("\u00b7")
    return parts[-1].strip() if len(parts) > 1 else from_str


@app.route("/api/inbox", methods=["DELETE", "OPTIONS"])
def api_inbox_delete():
    """删除收件箱记录（客服，需密码）"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    pwd = data.get("password") or ""
    if pwd != STAFF_PASSWORD:
        return jsonify({"success": False, "error": "无权限：需要客服密码"})
    username = (data.get("username") or "").strip()
    item_id = data.get("id")
    if username:
        deleted = db.delete_inbox_by_username(username)
    elif item_id:
        deleted = db.delete_inbox_by_id(item_id)
    else:
        return jsonify({"success": False, "error": "需要指定删除的用户或记录ID"})
    return jsonify({"success": True, "deleted": deleted})


# ============================== 聊天接口 ==============================

@app.route("/api/chat/partners", methods=["GET"])
def api_chat_partners():
    """返回有聊天记录的不同用户列表（客服用）"""
    partners = db.get_chat_partners()
    return jsonify({"success": True, "partners": partners})


@app.route("/api/chat/<username>", methods=["GET"])
def api_chat_list(username):
    """获取与某用户的聊天记录"""
    return jsonify({"success": True, "messages": db.get_chat(username)})


@app.route("/api/chat/<username>", methods=["POST", "OPTIONS"])
def api_chat_send(username):
    """发送消息（客服或用户均可）"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    sender = data.get("sender", "user")  # "staff" 或 "user"
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"success": False, "error": "消息不能为空"}), 400

    msg_id = int(time.time() * 1000)
    db.add_chat_message(username, sender, text, msg_id)
    return jsonify({"success": True, "message": {"id": msg_id, "sender": sender, "text": text, "time": msg_id}})


@app.route("/api/user-likes/<username>", methods=["GET"])
def api_user_likes(username):
    """获取某用户上传到收件箱的图片（即用户喜欢的图）"""
    inbox = db.get_inbox()
    likes = [it for it in inbox if _extract_username(it) == username]
    return jsonify({"success": True, "likes": likes})


# ============================== 图片上传中转 ==============================

IMGBB_API_KEY = os.environ.get(
    "IMGBB_API_KEY",
    "46da64c4ed5006f1c007a094443c650d"
)

IMGBB_WORKER_URL = os.environ.get(
    "IMGBB_WORKER_URL",
    "https://imgbb-proxy.imgbb-proxy.workers.dev"
)


def _try_upload_image(image_b64):
    """
    尝试多个图床上传，返回第一个成功的 URL。

    优先级: ImgBB直连 > ImgBB Worker > Catbox > Freeimage > 0x0.st

    说明：
      - ImgBB 直连第一优先：api.imgbb.com 国内可访问，返回 i.ibb.co 下载域名，
        可被 /api/img-cache 代理正常拉取。
      - Cloudflare Worker（*.workers.dev）国内被墙，仅作第二优先备用。
      - Catbox 上传稳定但下载域名对 Render 数据中心 IP 被拒，降为备份。
    """
    import base64
    try:
        img_data = base64.b64decode(image_b64 + "==")
    except Exception:
        img_data = base64.b64decode(image_b64)

    def _post_upload(url, files=None, data=None, timeout=15):
        """统一上传请求封装，返回 (成功标志, resp 或 None)"""
        try:
            resp = requests.post(url, files=files, data=data, timeout=timeout)
            return True, resp
        except Exception as e:
            print(f"[上传] 请求异常 {url}: {e}")
            return False, None

    # --- 方法1: ImgBB 直连 API（第一优先，国内可访问）---
    try:
        ok, resp = _post_upload(
            "https://api.imgbb.com/1/upload",
            data={"key": IMGBB_API_KEY, "image": image_b64},
            timeout=15,
        )
        if ok:
            result = resp.json() if resp.content else {}
            if result.get("success"):
                url = (
                    result.get("data", {}).get("url", "")
                    or result.get("data", {}).get("display_url", "")
                )
                if url:
                    print(f"[上传] ImgBB 直连成功: {url[:60]}")
                    return url
            else:
                code = result.get("status_code")
                if code == 103:
                    print("[上传] ImgBB 直连: code 103 (IP 被封)，尝试 Worker")
                else:
                    print(f"[上传] ImgBB 直连失败: {str(result)[:120]}")
    except Exception as e:
        print(f"[上传] ImgBB 直连异常: {e}")

    # --- 方法2: Cloudflare Worker -> ImgBB（备用）---
    try:
        ok, resp = _post_upload(
            IMGBB_WORKER_URL,
            data={"image": image_b64},  # Worker 只接受 form 格式
            timeout=10,
        )
        if ok:
            result = resp.json() if resp.content else {}
            if result.get("success"):
                url = result.get("data", {}).get("url", "")
                if url:
                    print(f"[上传] ImgBB Worker 成功: {url[:60]}")
                    return url
    except Exception as e:
        print(f"[上传] ImgBB Worker 失败: {e}")

    # --- 方法3: Catbox（无需认证）---
    try:
        ok, resp = _post_upload(
            "https://catbox.moe/user/api.php",
            files={"fileToUpload": ("image.png", img_data, "image/png")},
            data={"reqtype": "fileupload"},
            timeout=15,
        )
        if ok:
            url = resp.text.strip()
            if url.startswith("https://") and "catbox.moe" in url:
                print(f"[上传] Catbox 成功: {url[:60]}")
                return url
    except Exception as e:
        print(f"[上传] Catbox 失败: {e}")

    # --- 方法4: Freeimage（匿名 key=free）---
    try:
        ok, resp = _post_upload(
            "https://freeimage.host/api/1/upload",
            files={"source": ("image.png", img_data, "image/png")},
            data={"key": "free"},
            timeout=15,
        )
        if ok:
            result = resp.json() if resp.content else {}
            if result.get("status_code") == 200:
                url = result.get("image", {}).get("url") or result.get("url", "")
                if url:
                    print(f"[上传] Freeimage 成功: {url[:60]}")
                    return url
    except Exception as e:
        print(f"[上传] Freeimage 失败: {e}")

    # --- 方法5: 0x0.st（匿名，上限 512MB）---
    try:
        ok, resp = _post_upload(
            "https://0x0.st",
            files={"file": ("image.png", img_data, "image/png")},
            timeout=15,
        )
        if ok:
            url = resp.text.strip()
            if url.startswith("https://") and "0x0.st" in url:
                print(f"[上传] 0x0.st 成功: {url[:60]}")
                return url
    except Exception as e:
        print(f"[上传] 0x0.st 失败: {e}")

    return None


@app.route("/api/upload-imgbb", methods=["POST", "OPTIONS"])
def api_upload_imgbb():
    """
    接收前端图片 → 多图床自动回退上传 → 返回直链

    请求体: {"image": "<base64 编码图片>"}
    响应:   {"success": true, "url": "..."} 或 {"success": false, "error": "..."}
    """
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    data = request.get_json(silent=True) or {}
    image_b64 = data.get("image", "").strip()

    if not image_b64:
        return jsonify({"success": False, "error": "缺少图片数据"})

    try:
        url = _try_upload_image(image_b64)
        if url:
            return jsonify({"success": True, "url": url})
        return jsonify({"success": False, "error": "所有图床均上传失败，请稍后重试"})
    except Exception as e:
        print(f"[IMGBB ERROR] {e}")
        return jsonify({"success": False, "error": f"上传异常: {e}"})


# ============================== 启动 ==============================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
