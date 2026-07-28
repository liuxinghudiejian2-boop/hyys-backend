#!/usr/bin/env python3
"""
百度网盘分享链接解析服务
=============================================
功能：接收百度网盘分享链接 + 提取码，解析出图片直链
提供图片代理接口，解决前端 CORS / Referer 限制

启动：python baidu_pan_parser.py
依赖：pip install flask requests

API 列表：
  POST /api/parse   解析分享链接 -> 返回图片列表
  GET  /api/proxy   图片代理（绕过 Referer 限制）
  GET  /api/health  健康检查
"""

import os
import re
import json
import time
import hashlib
from urllib.parse import urlparse, parse_qs, quote, unquote
import db  # SQLite 数据库（持久化，冷启动不丢失）

import requests
from flask import Flask, request, jsonify, Response, send_file

app = Flask(__name__)


# ============================== CORS 跨域支持 ==============================

@app.after_request
def add_cors_headers(resp):
    """给所有响应添加 CORS 头，允许前端跨域调用"""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"

    # localtunnel 自动 bypass：通过后端设置 cookie，避免确认页面反复出现
    host = request.headers.get("Host", "")
    if "loca.lt" in host:
        client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if not client_ip:
            client_ip = request.remote_addr or ""
        if client_ip:
            resp.set_cookie(
                "bypass-tunnel-reminder",
                client_ip,
                domain=".loca.lt",
                max_age=604800,
                path="/",
            )

    return resp


# ============================== 配置 ==============================

class Config:
    BAIDU_PAN_BASE = "https://pan.baidu.com"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    # 图片代理缓存（秒）
    PROXY_CACHE_TIME = 3600
    # 解析结果缓存 {key: {result, ts}}
    PARSE_CACHE = {}
    PARSE_CACHE_TTL = 1800  # 30 分钟
    # 请求超时
    TIMEOUT = 15


# 支持的图片扩展名
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp",
    ".gif", ".bmp", ".tiff", ".heic",
}

# errno 错误码映射
ERRNO_MAP = {
    -12:   "提取码错误",
    -130:  "提取码错误",
    -62:   "请求过于频繁，请稍后重试",
    2:     "分享链接已过期或已删除",
    31066: "分享文件不存在",
    31034: "分享链接已失效",
    9019:  "分享链接已过期",
}


# ============================== 核心解析逻辑 ==============================

def extract_surl_and_pwd(url, code=None):
    """
    从百度网盘分享链接中提取 surl 和 pwd

    支持格式：
      1. https://pan.baidu.com/s/1xxxxxx?pwd=abcd   （新版，pwd 在 URL 里）
      2. https://pan.baidu.com/s/1xxxxxx             （旧版，提取码单独传）
      3. https://pan.baidu.com/share/init?surl=xxxxx

    返回: (surl, pwd)  surl 已去掉前缀 '1'
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    # 从 URL 参数提取 pwd
    if not code:
        code = query.get("pwd", [None])[0]

    # 提取 surl：/s/1xxxxxx -> 去掉 '1' 前缀
    match = re.search(r"/s/1([A-Za-z0-9_-]+)", parsed.path)
    if match:
        surl = match.group(1)
    else:
        # 兼容 /s/xxxxx（无1前缀）或 /share/init?surl=xxxxx
        match = re.search(r"/s/([A-Za-z0-9_-]+)", parsed.path)
        if match:
            surl = match.group(1)
            if surl.startswith("1"):
                surl = surl[1:]
        else:
            surl = query.get("surl", [None])[0]

    return surl, code


def create_session():
    """创建带浏览器 UA 的 requests 会话"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": Config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": Config.BAIDU_PAN_BASE,
        "Connection": "keep-alive",
    })
    return session


def init_session(session):
    """初始化会话，获取 BAIDUID 等基础 Cookie"""
    try:
        resp = session.get(f"{Config.BAIDU_PAN_BASE}/", timeout=Config.TIMEOUT)
        return resp.status_code == 200
    except Exception:
        return False


def get_share_info_from_page(session, surl, pwd):
    """
    从分享页面提取 share_uk 和 shareid

    访问 /share/init?surl=xxx 页面，从内嵌的 yunData 中提取
    share_uk 和 shareid，这两个参数是调用 wxlist API 的必需参数

    返回: (share_uk, shareid) 或 (None, None)
    """
    try:
        init_url = f"{Config.BAIDU_PAN_BASE}/share/init?surl={surl}"
        if pwd:
            init_url += f"&pwd={pwd}"

        resp = session.get(init_url, timeout=Config.TIMEOUT)
        html = resp.text

        share_uk = None
        shareid = None

        # 提取 share_uk（支持 share_uk:"123" 或 "share_uk": "123" 等格式）
        m = re.search(r'share_uk["\':\s]+["\']?(\d+)', html)
        if m:
            share_uk = m.group(1)

        # 提取 shareid
        m = re.search(r'shareid["\':\s]+["\']?(\d+)', html)
        if m:
            shareid = m.group(1)

        return share_uk, shareid

    except Exception:
        return None, None


def get_file_list_via_wxlist(session, share_uk, shareid, pwd):
    """
    通过 wxlist API 直接获取文件列表

    关键发现：wxlist API 可以直接用提取码作为 pwd 和 sekey 参数，
    无需先调用 /share/verify 验证提取码（verify 接口已不稳定）

    返回: (file_list, error_msg)
    """
    if not share_uk or not shareid:
        return [], "无法获取分享信息 (share_uk/shareid)"

    try:
        api_url = f"{Config.BAIDU_PAN_BASE}/share/wxlist"
        params = {
            "clienttype": "25",
            "shareid": shareid,
            "uk": share_uk,
            "root": "1",
            "page": "1",
            "num": "200",
            "refer": "",
            "pwd": pwd,
            "sekey": pwd,
            "short": "1",
        }

        resp = session.get(api_url, params=params, timeout=Config.TIMEOUT)
        data = resp.json()

        if data.get("errno") == 0:
            file_list = data.get("data", {}).get("list", [])
            # 同时保存 title 和 seckey 供后续使用
            return file_list, None

        errno = data.get("errno", -1)
        msg = ERRNO_MAP.get(errno, f"API 错误 (errno={errno})")
        return [], msg

    except requests.exceptions.Timeout:
        return [], "请求百度网盘 API 超时，请稍后重试"
    except Exception as e:
        return [], f"API 请求异常: {e}"


def get_thumbnail_url(session, file_info, randsk):
    """
    获取图片缩略图 URL

    优先使用文件列表中的 thumbs 字段；
    若没有，尝试构造缩略图 URL
    """
    # 1. 直接使用 thumbs 字段
    thumbs = file_info.get("thumbs", {})
    if thumbs:
        for key in ("url3", "url2", "url1", "icon"):
            url = thumbs.get(key)
            if url:
                return url

    # 2. 尝试使用 dlink
    dlink = file_info.get("dlink")
    if dlink:
        return dlink

    # 3. 构造缩略图 URL（通过 fs_id）
    fs_id = file_info.get("fs_id")
    if fs_id:
        # 百度网盘缩略图接口
        return f"{Config.BAIDU_PAN_BASE}/thumbnail/{fs_id}?c=3&quality=100&type=jpg"

    return None


def filter_images(files):
    """
    从文件列表中筛选图片文件

    wxlist API 返回的 isdir 是字符串 "0"/"1"，category "3" 表示图片

    返回: [{name, fs_id, size, url, thumb, dlink, resolution}, ...]
    """
    images = []

    for f in files:
        # 跳过文件夹（wxlist 返回字符串类型）
        isdir = str(f.get("isdir", "0"))
        if isdir == "1":
            continue

        filename = f.get("server_filename", "") or f.get("filename", "")
        if not filename:
            continue

        # 优先用 category 判断（"3" = 图片），同时也检查扩展名
        category = str(f.get("category", ""))
        ext = ""
        if "." in filename:
            ext = "." + filename.rsplit(".", 1)[-1].lower()

        is_image = (category == "3") or (ext in IMAGE_EXTENSIONS)
        if not is_image:
            continue

        # 获取缩略图 URL（优先大图 url3）
        thumbs = f.get("thumbs", {})
        thumb = None
        if thumbs:
            for key in ("url3", "url2", "url1", "icon"):
                url = thumbs.get(key)
                if url:
                    thumb = url
                    break

        # dlink 是原始下载链接（全尺寸）
        dlink = f.get("dlink")

        # 优先使用缩略图（加载快），dlink 作为备选
        display_url = thumb or dlink

        images.append({
            "name": filename,
            "fs_id": str(f.get("fs_id", "")),
            "size": int(f.get("size", 0)),
            "thumb": thumb,
            "dlink": dlink,
            "url": display_url,
            "resolution": f.get("resolution", ""),
        })

    return images


def parse_baidu_share(url, code):
    """
    完整解析百度网盘分享链接

    流程:
      1. 提取 surl + pwd
      2. 初始化会话 (获取 BAIDUID Cookie)
      3. 从分享页面提取 share_uk 和 shareid
      4. 通过 wxlist API 获取文件列表 (直接用提取码，跳过 verify)
      5. 筛选图片
      6. 返回图片列表 (URL 通过代理)

    返回: {success, images, error, title}
    """
    # 1. 提取参数
    surl, pwd = extract_surl_and_pwd(url, code)
    if not surl:
        return {"success": False, "error": "无法解析分享链接，请检查链接格式"}
    if not pwd:
        return {"success": False, "error": "缺少提取码，请填写提取码"}

    # 2. 初始化会话
    session = create_session()
    if not init_session(session):
        return {"success": False, "error": "无法连接百度网盘服务器"}

    # 3. 从分享页面提取 share_uk 和 shareid
    share_uk, shareid = get_share_info_from_page(session, surl, pwd)
    if not share_uk or not shareid:
        return {"success": False, "error": "无法获取分享信息，链接可能已失效或被删除"}

    # 4. 通过 wxlist API 获取文件列表（直接用提取码，跳过 verify）
    files, err = get_file_list_via_wxlist(session, share_uk, shareid, pwd)
    if not files:
        return {"success": False, "error": err or "无法获取文件列表"}

    # 5. 筛选图片
    images = filter_images(files)
    if not images:
        return {"success": False, "error": "分享中没有找到图片文件（仅支持 jpg/png/webp/gif 等）"}

    # 6. 构造结果（图片通过代理加载）
    result_images = []
    for img in images:
        if img["url"]:
            # 先 unquote 再 quote，避免双重编码（百度返回的 URL 已含 %3D 等）
            clean_url = unquote(img["url"])
            proxy_url = f"/api/proxy?url={quote(clean_url, safe='')}"
        else:
            proxy_url = None

        result_images.append({
            "name": img["name"],
            "url": proxy_url,
            "original_url": img["url"],
            "thumb": img["thumb"],
            "dlink": img["dlink"],
            "size": img["size"],
            "fs_id": img["fs_id"],
            "resolution": img["resolution"],
        })

    return {
        "success": True,
        "images": result_images,
        "title": f"解析成功，共 {len(result_images)} 张图片",
        "total": len(result_images),
    }


# ============================== 诊断端点 ==============================

@app.route("/api/debug/db", methods=["GET"])
def api_debug_db():
    """诊断数据库是否正常工作"""
    try:
        user = db.get_user("12356789")
        return jsonify({"success": True, "user_found": user is not None, "user": user})
    except Exception as e:
        import traceback
        return jsonify({"success": False, "error": str(e), "trace": traceback.format_exc()})


@app.route("/api/debug/login", methods=["POST", "OPTIONS"])
def api_debug_login():
    """模拟登录逻辑诊断"""
    try:
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        hp = hash_password(password)
        user = db.get_user(username)
        return jsonify({
            "success": True,
            "data_received": data,
            "username": username,
            "password_len": len(password),
            "hash_len": len(hp),
            "user": user,
        })
    except Exception as e:
        import traceback
        return jsonify({"success": False, "error": str(e), "trace": traceback.format_exc()})


# ============================== Flask 路由 ==============================

@app.route("/api/parse", methods=["POST", "OPTIONS"])
def api_parse():
    """
    解析百度网盘分享链接

    请求体:
      {"url": "https://pan.baidu.com/s/1xxx", "code": "abcd"}

    响应:
      成功: {"success": true, "images": [{name, url, size, ...}], "total": 3}
      失败: {"success": false, "error": "提取码错误"}
    """
    # 处理 CORS 预检请求
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    code = data.get("code", "").strip()

    if not url:
        return jsonify({"success": False, "error": "请输入分享链接"})

    # 简单校验
    if "pan.baidu.com" not in url and "yun.baidu.com" not in url:
        return jsonify({"success": False, "error": "请输入有效的百度网盘分享链接"})

    # 从 URL 中自动提取提取码（支持 "链接: xxx 提取码: abcd" 粘贴格式）
    if not code:
        pwd_match = re.search(r"[?&]pwd=([A-Za-z0-9]{4})", url)
        if pwd_match:
            code = pwd_match.group(1)
            # 清理 URL
            url = re.sub(r"[?&]pwd=[A-Za-z0-9]{4}", "", url)

    # 也支持用户把 "提取码: abcd" 直接粘贴在 url 字段里
    if not code:
        code_match = re.search(r"提取码[:\s：]*([A-Za-z0-9]{4})", url)
        if code_match:
            code = code_match.group(1)
            url = re.sub(r"提取码[:\s：]*[A-Za-z0-9]{4}", "", url).strip()

    # 缓存检查
    cache_key = hashlib.md5(f"{url}|{code}".encode()).hexdigest()
    cached = Config.PARSE_CACHE.get(cache_key)
    if cached and time.time() - cached["timestamp"] < Config.PARSE_CACHE_TTL:
        cached["result"]["cached"] = True
        return jsonify(cached["result"])

    # 执行解析
    result = parse_baidu_share(url, code)

    # 缓存成功结果
    if result["success"]:
        Config.PARSE_CACHE[cache_key] = {
            "result": result,
            "timestamp": time.time(),
        }

    return jsonify(result)


@app.route("/api/proxy", methods=["GET"])
def api_proxy():
    """
    图片代理接口

    代理百度网盘图片 URL，解决前端 CORS / Referer 限制

    请求: GET /api/proxy?url=https://pan.baidu.com/thumbnail/xxx
    响应: 图片二进制数据 (带缓存头)
    """
    target_url = request.args.get("url", "")
    if not target_url:
        return "Missing 'url' parameter", 400

    # SSRF 防护：只允许代理百度域名
    parsed = urlparse(target_url)
    allowed_domains = ("baidu.com", "bdimg.com", "bdstatic.com", "pcsdata.baidu.com", "d.pcs.baidu.com")
    if not any(parsed.hostname and parsed.hostname.endswith(d) for d in allowed_domains):
        return "Forbidden: only Baidu domains are allowed", 403

    try:
        session = create_session()
        resp = session.get(target_url, timeout=Config.TIMEOUT)

        if resp.status_code != 200:
            return f"Failed to fetch image: HTTP {resp.status_code}", 502

        content_type = resp.headers.get("Content-Type", "image/jpeg")
        content = resp.content  # 显式读取全部内容
        resp.close()

        if not content:
            return "Empty image data", 502

        response = Response(
            content,
            content_type=content_type,
        )
        response.headers["Cache-Control"] = f"public, max-age={Config.PROXY_CACHE_TIME}"
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
    except requests.exceptions.Timeout:
        return "Proxy timeout", 504
    except Exception as e:
        print(f"[PROXY ERROR] {e}")
        return f"Proxy error: {e}", 500


@app.route("/api/health", methods=["GET"])
def api_health():
    """健康检查"""
    return jsonify({
        "status": "ok",
        "service": "baidu-pan-parser",
        "cache_size": len(Config.PARSE_CACHE),
    })


@app.route("/api/public-ip", methods=["GET"])
def api_public_ip():
    """返回客户端公网 IP（API 请求不被 localtunnel 拦截，方便首次访问时获取 IP）"""
    client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if not client_ip:
        client_ip = request.remote_addr or ""
    return jsonify({"ip": client_ip})


# ============================== 配置 ==============================

class Config:
    BAIDU_PAN_BASE = "https://pan.baidu.com"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    PROXY_CACHE_TIME = 3600
    PARSE_CACHE = {}
    PARSE_CACHE_TTL = 1800
    TIMEOUT = 15


# 支持的图片扩展名
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp",
    ".gif", ".bmp", ".tiff", ".heic",
}

# errno 错误码映射
ERRNO_MAP = {
    -12:   "提取码错误",
    -130:  "提取码错误",
    -62:   "请求过于频繁，请稍后重试",
    2:     "分享链接已过期或已删除",
    31066: "分享文件不存在",
    31034: "分享链接已失效",
    9019:  "分享链接已过期",
}


@app.route("/api/register", methods=["POST", "OPTIONS"])
def api_register():
    """用户注册"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"success": False, "error": "请输入用户名和密码"})
    if len(username) < 2:
        return jsonify({"success": False, "error": "用户名至少 2 个字符"})
    if len(password) < 4:
        return jsonify({"success": False, "error": "密码至少 4 位"})
    if username == STAFF_ACCOUNT:
        return jsonify({"success": False, "error": "该用户名已被保留"})

    users = db.get_user(username)
    if users:
        return jsonify({"success": False, "error": "用户名已存在"})

    db.create_user(username, hash_password(password), "user")
    return jsonify({"success": True, "message": "注册成功"})


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

        return jsonify({
            "success": True,
            "role": users["role"],
            "username": users.get("name", username),
        })
    except Exception as e:
        import traceback
        return jsonify({"success": False, "error": str(e), "trace": traceback.format_exc()}), 500


def check_staff_password(data):
    """校验客服密码（图库写操作需要）"""
    pwd = data.get("password") or ""
    return pwd == STAFF_PASSWORD


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
    # 密码通过 query 参数传递
    pwd = request.args.get("password") or ""
    if pwd != STAFF_PASSWORD:
        return jsonify({"success": False, "error": "无权限：需要客服密码"})
    deleted = db.delete_gallery_item(item_id)
    if not deleted:
        return jsonify({"success": False, "error": "图片不存在"})
    return jsonify({"success": True})


# ============================== 收件箱接口 ==============================

@app.route("/api/inbox", methods=["GET"])
def api_inbox_list():
    """获取收件箱列表（客服查看用户上传）"""
    return jsonify({"success": True, "inbox": db.get_inbox()})


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


# ============================== ImgBB 上传中转 ==============================

IMGBB_WORKER_URL = os.environ.get(
    "IMGBB_WORKER_URL",
    "https://imgbb-proxy.imgbb-proxy.workers.dev"
)


@app.route("/api/upload-imgbb", methods=["POST", "OPTIONS"])
def api_upload_imgbb():
    """
    接收前端图片 → 转发到 Cloudflare Worker → ImgBB → 返回直链

    请求体: {"image": "<base64 编码的图片数据>"}
    响应:   {"success": true, "url": "https://i.ibb.co/xxx/xxx.jpg"}
            或 {"success": false, "error": "..."}
    """
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    data = request.get_json(silent=True) or {}
    image_b64 = data.get("image", "").strip()

    if not image_b64:
        return jsonify({"success": False, "error": "缺少图片数据"})

    try:
        # 通过 Cloudflare Worker 转发到 ImgBB（Worker 持有 API Key 环境变量）
        resp = requests.post(
            IMGBB_WORKER_URL,
            data={"image": image_b64},
            timeout=30,
        )
        result = resp.json()

        if result.get("success"):
            return jsonify({"success": True, "url": result["data"]["url"]})
        else:
            error_msg = result.get("error", {}).get("message", "ImgBB 上传失败")
            return jsonify({"success": False, "error": error_msg})

    except requests.exceptions.Timeout:
        return jsonify({"success": False, "error": "上传超时，图片可能过大"})
    except Exception as e:
        print(f"[IMGBB ERROR] {e}")
        return jsonify({"success": False, "error": f"上传异常: {e}"})


# ============================== 托管前端页面 ==============================

@app.route("/", methods=["GET"])
def index():
    """托管前端原型页面，手机访问 http://电脑IP:5000/ 即可打开"""
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beauty-gallery-prototype.html")
    if os.path.exists(html_path):
        return send_file(html_path)
    return "原型文件 beauty-gallery-prototype.html 未找到", 404


# ============================== 启动 ==============================

if __name__ == "__main__":
    print("=" * 55)
    print("  百度网盘分享链接解析服务")
    print("=" * 55)
    print("  API:")
    print("    GET  /                   - 前端原型页面（手机访问此地址）")
    print("    POST /api/register       - 用户注册")
    print("    POST /api/login          - 用户登录")
    print("    GET  /api/gallery        - 图库列表（多端共享）")
    print("    POST /api/gallery        - 添加图片（客服，需密码）")
    print("    DELETE /api/gallery/<id> - 删除图片（客服，需密码）")
    print("    GET  /api/inbox          - 收件箱列表")
    print("    POST /api/inbox          - 用户上传给客服")
    print("    DELETE /api/inbox        - 删除收件箱记录（客服，需密码）")
    print("    GET  /api/chat/<username>  - 获取与某用户的聊天记录")
    print("    POST /api/chat/<username>  - 发送聊天消息")
    print("    GET  /api/user-likes/<username> - 获取用户喜欢的图片")
    print("    POST /api/parse          - 解析百度网盘分享链接")
    print("    GET  /api/proxy          - 图片代理（仅限百度域名）")
    print("    POST /api/upload-imgbb   - 上传图片到 ImgBB 图床（中转）")
    print("    GET  /api/health         - 健康检查")
    print("-" * 55)
    print("  手机访问: 确保手机与电脑连同一 WiFi，浏览器打开")
    print("  http://<电脑局域网IP>:5000/  即可使用")
    print("=" * 55)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
