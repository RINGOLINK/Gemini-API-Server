# -*- coding: utf-8 -*-
"""
fingerprint/account_cookies.py —— 从指纹账号窗口读取 Gemini 登录 cookie

复用 fingerprint.server 的 Chromium Cookies DB 解密逻辑(DPAPI + AES)。
服务端多账号池据此为每个账号构建 GeminiClient。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

STORAGE_ROOT = Path(__file__).resolve().parent / "storage"


def read_gemini_cookies(pid: str) -> dict | None:
    """读取账号窗口的 Gemini 登录 cookie。返回 {psid, psidts} 或 None(未登录/读失败)。

    优先读明文导出文件(窗口进程/桥写入,最新);其次读 Chromium Cookies DB(解密)。
    """
    # 1) 明文导出文件优先(内容最新)
    try:
        f = STORAGE_ROOT / pid / "gemini_cookies.json"
        if f.exists():
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("psid") and d.get("psidts"):
                return {"psid": d["psid"], "psidts": d["psidts"], "read_at": time.time(), "src": "file"}
    except Exception:
        pass
    # 2) Chromium Cookies DB(DPAPI 解密)
    try:
        from fingerprint.server import _read_cookies_db
        cookies = _read_cookies_db(pid)
        psid = ""
        psidts = ""
        for c in cookies or []:
            name = c.get("name", "")
            if name == "__Secure-1PSID":
                psid = c.get("value", "")
            elif name == "__Secure-1PSIDTS":
                psidts = c.get("value", "")
        if psid and psidts:
            return {"psid": psid, "psidts": psidts, "read_at": time.time(), "src": "db"}
    except Exception:
        pass
    return None


def save_gemini_cookies(pid: str, psid: str, psidts: str):
    """窗口进程/桥在检测到登录后,可导出明文 cookie 供服务端读取(兜底路径)。"""
    try:
        d = STORAGE_ROOT / pid
        d.mkdir(parents=True, exist_ok=True)
        (d / "gemini_cookies.json").write_text(
            json.dumps({"pid": pid, "psid": psid, "psidts": psidts, "saved_at": time.time()},
                       ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def account_proxy(pid: str, meta: dict | None = None) -> str | None:
    """账号代理(profile.proxy 配置) → 代理 URL 字符串(服务端 GeminiClient 用)。"""
    if meta is None:
        from fingerprint import profiles
        meta = profiles.get(pid) or {}
    proxy = (meta.get("proxy") or {})
    mode = proxy.get("mode") or "direct"
    if mode in ("direct", "system", ""):
        return None
    if mode.startswith("bound:"):
        from fingerprint import proxy_manager as pm
        px = pm.get_proxy(mode[6:])
        if not px:
            return None
        auth = f"{px.get('username','')}:{px.get('password','')}@" if px.get("username") else ""
        return f"{px['scheme']}://{auth}{px['host']}:{px['port']}"
    host = (proxy.get("host") or "").strip()
    if not host:
        return None
    auth = f"{proxy.get('username','')}:{proxy.get('password','')}@" if proxy.get("username") else ""
    return f"{mode}://{auth}{host}:{proxy.get('port') or 1080}"
