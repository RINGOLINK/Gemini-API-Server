"""指纹浏览器管理服务 —— 127.0.0.1:4446(Gemini 多账号指纹浏览器系统)"""
import sys, shutil, os, json, subprocess, ctypes
from pathlib import Path
from pydantic import BaseModel
from fastapi import FastAPI, Query, HTTPException, Body, UploadFile, File, Request
import asyncio as _asyncio
from fastapi.responses import FileResponse, HTMLResponse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HTML_DIR = Path(__file__).resolve().parent / "html"
sys.path.insert(0, str(PROJECT_ROOT))
from fingerprint.browser import FingerprintBrowser, STORAGE_ROOT, MANAGE_PORT
from fingerprint import profiles
from fingerprint import cookie_manager as cm
from fingerprint import account_manager as am
from fingerprint import logo_fetcher
from fingerprint.window import is_running, window_state, launch_window, close_window, _resolve_proxy_with_failover

app = FastAPI(title="Gemini 指纹浏览器管理", version="0.1")

# 允许任意来源跨域（本机工具，页面内自动填充需跨域调 match API）
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
async def index(): return FileResponse(HTML_DIR / "index.html")

@app.get("/manifest.webmanifest")
async def pwa_manifest():
    return FileResponse(HTML_DIR / "manifest.webmanifest", media_type="application/manifest+json")

@app.get("/sw-pwa.js")
async def pwa_sw():
    return FileResponse(HTML_DIR / "sw-pwa.js", media_type="application/javascript")


@app.get("/browser")
async def browser_panel(): return FileResponse(HTML_DIR / "browser.html")

@app.get("/welcome", response_class=HTMLResponse)
async def welcome_page(): return FileResponse(HTML_DIR / "welcome.html")

# ── Profile CRUD ──
@app.get("/api/profiles")
async def list_profiles():
    return {"profiles": profiles.list_all()}

@app.post("/api/profiles")
async def create_profile(data: dict = Body({})):
    # Accept full schema in body
    return profiles.create(**data)

@app.patch("/api/profiles/{pid}")
async def update_profile(pid: str, data: dict = Body(...)):
    if is_running(pid):
        raise HTTPException(400, "运行中无法编辑，请先关闭窗口")
    result = profiles.update(pid, **data)
    if not result: raise HTTPException(404)
    return result

# ── 分组管理 ──
@app.put("/api/groups/{old_name}")
async def rename_group(old_name: str, new_name: str = Body(...)):
    new_name = (new_name or "").strip()
    if not new_name:
        raise HTTPException(400, "分组名不能为空")
    for p in profiles.list_all():
        if p.get("basic",{}).get("group") == old_name:
            profiles.update(p["id"], group=new_name)
    # 同步更新独立分组表
    profiles.rename_group_in_table(old_name, new_name)
    return {"ok": True}


@app.post("/api/groups")
async def add_group(name: str = Body(..., embed=True)):
    """添加分组。分组是逻辑概念（存于各 profile.basic.group），
    添加即登记一个可用分组名（存入独立分组表，允许空分组存在）。"""
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "分组名不能为空")
    if name == "全部":
        raise HTTPException(400, "不能使用保留名「全部」")
    profiles.add_group(name)
    return {"ok": True, "name": name}


@app.delete("/api/groups/{name}")
async def delete_group(name: str):
    """删除分组：该分组下所有窗口归入「默认」。"""
    if name in ("全部", "默认"):
        raise HTTPException(400, "不能删除该分组")
    profiles.remove_group(name)
    return {"ok": True}


@app.delete("/api/profiles/{pid}")
async def delete_profile(pid: str):
    if is_running(pid):
        raise HTTPException(400, "运行中无法删除，请先关闭窗口")
    profiles.delete(pid)
    return {"ok": True}

# ── 浏览器操作(独立窗口进程调度)──
@app.post("/api/browser/open")
async def open_browser(pid: str = Query(...), headless: bool = Query(False)):
    if not profiles.get(pid):
        raise HTTPException(404, "profile 不存在")
    return await _asyncio.to_thread(launch_window, pid)

@app.post("/api/browser/close")
async def close_browser(pid: str = Query(...)):
    return await _asyncio.to_thread(close_window, pid)

@app.post("/api/proxy/apply-all")
async def apply_proxy_all(data: dict = Body({})):
    """把主页「出口代理配置」保存的代理同步应用到所有账号窗口(服务端+窗口共用同一代理)。"""
    if not data.get("enabled"):
        proxy_data = {"mode": "direct", "host": "", "port": "", "username": "", "password": ""}
    else:
        scheme = (data.get("scheme") or "socks5h")
        mode = "socks5" if scheme in ("socks5", "socks5h") else ("http" if scheme == "http" else "direct")
        proxy_data = {"mode": mode, "host": (data.get("host") or "").strip(),
                      "port": data.get("port") or "", "username": data.get("user", ""),
                      "password": data.get("pass", "")}
    n = 0
    for p in profiles.list_all():
        try:
            profiles.update(p["id"], proxy=proxy_data)
            n += 1
        except Exception:
            pass
    return {"ok": True, "applied": n}


@app.get("/api/browser/status")
async def status():
    pids = [p["id"] for p in profiles.list_all()]
    running, background = [], []
    for pid in pids:
        st = window_state(pid)
        if st.get("status") in ("running", "idle") and st.get("os_pid"):
            # 窗口开着(运行中)或窗口已关但内核进程后台保活(idle)都算活跃
            (running if st["status"] == "running" else background).append(pid)
    return {"active_count": len(running) + len(background), "active_pids": running,
            "background_pids": background, "storage": str(STORAGE_ROOT)}

# ── Cookie ──
# per-pid 操作锁：防止并发写 cookie 导致 Internal Server Error
_cookie_locks: dict[str, "_asyncio.Lock"] = {}


def _lock_for(pid: str) -> "_asyncio.Lock":
    if pid not in _cookie_locks:
        _cookie_locks[pid] = _asyncio.Lock()
    return _cookie_locks[pid]

import time as _time
_manage_cache: dict[str, tuple[float, dict]] = {}
_MANAGE_TTL = 30.0  # 关闭窗口结果缓存 30s


def _invalidate_manage(pid: str):
    _manage_cache.pop(pid, None)


# ── Chromium sqlite Cookies 直读写（关闭窗口快速路径）──
import sqlite3 as _sqlite3

_EPOCH_OFFSET = 11644473600


def _cookies_db_path(pid: str):
    from fingerprint.browser import STORAGE_ROOT
    return STORAGE_ROOT / pid / "userdata" / "Default" / "Network" / "Cookies"


def _unix_to_chrome(ts: float) -> int:
    return int((ts + _EPOCH_OFFSET) * 1_000_000)


def _chrome_to_unix(us: int) -> float:
    return us / 1_000_000 - _EPOCH_OFFSET


def _dpapi_decrypt(data: bytes) -> bytes | None:
    """Windows DPAPI 解密（本机同用户）。"""
    try:
        import ctypes, ctypes.wintypes as wt

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        bi = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
        bo = BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(bi), None, None, None, None, 0, ctypes.byref(bo)):
            return None
        out = ctypes.string_at(bo.pbData, bo.cbData)
        ctypes.windll.kernel32.LocalFree(bo.pbData)
        return out
    except Exception:
        return None


_aes_key_cache: dict[str, bytes] = {}


def _chrome_aes_key(pid: str) -> bytes | None:
    """从 Local State 取 os_crypt.encrypted_key（base64+DPAPI），解出 AES key。"""
    if pid in _aes_key_cache:
        return _aes_key_cache[pid]
    try:
        import json, base64
        from fingerprint.browser import STORAGE_ROOT
        ls = STORAGE_ROOT / pid / "userdata" / "Local State"
        data = json.loads(ls.read_text(encoding="utf-8"))
        enc_key = base64.b64decode(data["os_crypt"]["encrypted_key"])
        if enc_key[:5] == b"DPAPI":
            enc_key = enc_key[5:]
        key = _dpapi_decrypt(enc_key)
        if key:
            _aes_key_cache[pid] = key
        return key
    except Exception:
        return None


def _decrypt_value(enc: bytes, key: bytes | None) -> str:
    """解密 encrypted_value：v10/v11 = AES-128-GCM（nonce 12B 在 v1x 后）。"""
    if not enc:
        return ""
    if enc[:3] in (b"v10", b"v11"):
        if not key:
            return ""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = enc[3:15]
            ct = enc[15:]
            pt = AESGCM(key).decrypt(nonce, ct, None)
            # Chromium v10+ 明文带 32 字节 SHA256(host_key) 前缀，剥掉
            if len(pt) > 32:
                return pt[32:].decode("utf-8", errors="replace")
            return pt.decode("utf-8", errors="replace")
        except Exception:
            return ""
    # 旧版纯 DPAPI
    r = _dpapi_decrypt(enc)
    return r.decode("utf-8", errors="replace") if r else ""


def _read_cookies_db(pid: str) -> list:
    db = _cookies_db_path(pid)
    if not db.exists():
        return []
    key = _chrome_aes_key(pid)
    out = []
    try:
        con = _sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        cur = con.execute(
            "SELECT host_key,name,path,expires_utc,is_secure,is_httponly,samesite,encrypted_value,value FROM cookies")
        for host, name, path, exp, sec, ho, ss, enc, val in cur.fetchall():
            expires = -1 if not exp else _chrome_to_unix(exp)
            value = val or (_decrypt_value(bytes(enc), key) if enc else "")
            out.append({
                "name": name, "value": value, "domain": host, "path": path or "/",
                "expires": expires, "httpOnly": bool(ho), "secure": bool(sec),
                "sameSite": {0: "None", 1: "Lax", 2: "Strict"}.get(ss, "Lax"),
            })
        con.close()
    except Exception:
        pass
    return out


def _encrypt_value(plaintext: str, host: str, key: bytes | None) -> bytes:
    """v10 AES-GCM 加密：b'v10' + nonce(12) + AESGCM( SHA256(host) + plaintext )。"""
    if not key:
        return plaintext.encode("utf-8")
    try:
        import os, hashlib
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        prefix = hashlib.sha256(host.encode("utf-8")).digest()
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, prefix + plaintext.encode("utf-8"), None)
        return b"v10" + nonce + ct
    except Exception:
        return plaintext.encode("utf-8")


def _write_cookies_db(pid: str, cookies: list):
    db = _cookies_db_path(pid)
    if not db.exists():
        return
    key = _chrome_aes_key(pid)
    try:
        con = _sqlite3.connect(str(db), timeout=5)
        con.execute("DELETE FROM cookies")
        now_us = _unix_to_chrome(__import__("time").time())
        for ck in cookies:
            exp = ck.get("expires", -1)
            exp_us = 0 if (not isinstance(exp, (int, float)) or exp <= 0) else _unix_to_chrome(exp)
            ss = {"None": 0, "Lax": 1, "Strict": 2}.get(ck.get("sameSite", "Lax"), 1)
            host = ck.get("domain", "")
            val = ck.get("value", "")
            enc = _encrypt_value(val, host, key)
            con.execute(
                "INSERT INTO cookies (creation_utc,host_key,top_frame_site_key,name,value,encrypted_value,path,"
                "expires_utc,is_secure,is_httponly,last_access_utc,has_expires,is_persistent,priority,samesite,"
                "source_scheme,source_port,last_update_utc,source_type,has_cross_site_ancestor) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now_us, host, "", ck.get("name", ""), "", enc,
                 ck.get("path", "/"), exp_us, 1 if ck.get("secure") else 0, 1 if ck.get("httpOnly") else 0,
                 now_us, 1 if exp_us else 0, 1 if exp_us else 0, 1, ss, 1, -1, now_us, 0, 1))
        con.commit()
        con.close()
    except Exception:
        pass


def _merge_cookies(old: list, new: list) -> list:
    key = lambda c: (c.get("name"), c.get("domain"), c.get("path"))
    merged = {key(c): c for c in old}
    for c in new:
        merged[key(c)] = c
    return list(merged.values())


class _CookieCtx:
    """cookie 操作统一上下文：运行中复用 context，关闭则临时启动浏览器，退出时正确关闭。"""

    def __init__(self, pid: str):
        self.pid = pid
        self.browser = None      # 临时启动的 FingerprintBrowser（运行中则为 None）
        self.context = None      # 可用的 context
        self.cookies = []        # 进入时读取的全部 cookie

    async def __aenter__(self):
        # 子进程窗口方案:管理服务不持有浏览器实例,一律读 Chromium sqlite Cookies 库
        # (窗口运行中 WAL 模式只读可读;读锁冲突时由调用方提示关闭窗口)
        self.cookies = _read_cookies_db(self.pid)
        return self

    async def write(self, new_cookies: list):
        """清空并写入新 cookie 列表。"""
        if self.context is not None:
            await self.context.clear_cookies()
            if new_cookies:
                await self.context.add_cookies(new_cookies)
            self.cookies = new_cookies
        else:
            _write_cookies_db(self.pid, new_cookies)
            self.cookies = new_cookies

    async def add(self, new_cookies: list):
        if not new_cookies:
            return
        if self.context is not None:
            await self.context.add_cookies(new_cookies)
            self.cookies = await self.context.cookies()
        else:
            merged = _merge_cookies(self.cookies, new_cookies)
            _write_cookies_db(self.pid, merged)
            self.cookies = merged

    async def __aexit__(self, exc_type, exc, tb):
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass
        return False


def _platform_key_of(cookie: dict) -> str:
    """登录锚点的站点 key；非登录返回 None。复用 cm 站点聚合逻辑。"""
    return cm._platform_key_of(cookie)


@app.get("/api/profiles/{pid}/cookies")
async def export_cookies(pid: str):
    async with _lock_for(pid):
        async with _CookieCtx(pid) as ctx:
            return {"cookies": ctx.cookies}


@app.post("/api/profiles/{pid}/cookies/import")
async def import_cookies(pid: str, data: dict = Body(...)):
    """统一导入：接受单条或整条列表，自动按 domain 归位（无需指定平台）。

    body: {cookies: [...]}  或 {platforms: {平台key: [cookies...]}}（导出文件格式）
    """
    if is_running(pid):
        raise HTTPException(400, "运行中无法导入，请先关闭窗口")
    to_add = []
    if "platforms" in data and isinstance(data["platforms"], dict):
        for _, arr in data["platforms"].items():
            to_add.extend(arr)
    else:
        to_add = data.get("cookies", [])
    if not to_add:
        raise HTTPException(400, "无可导入的 cookie")
    # 清洗：只保留 playwright 认可的字段
    allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    clean = [{k: v for k, v in ck.items() if k in allowed} for ck in to_add]
    async with _lock_for(pid):
        async with _CookieCtx(pid) as ctx:
            await ctx.add(clean)
        _invalidate_manage(pid)
    return {"ok": True, "count": len(clean)}


@app.delete("/api/profiles/{pid}/cookies")
async def clear_cookies(pid: str):
    if is_running(pid):
        raise HTTPException(400, "运行中无法清空 cookie")
    async with _lock_for(pid):
        async with _CookieCtx(pid) as ctx:
            await ctx.write([])
        cookie_file = STORAGE_ROOT / pid / "userdata" / "Default" / "Network" / "Cookies"
        if cookie_file.exists():
            cookie_file.unlink()
    return {"ok": True}


# ── Cookie 管理（登录态 + 痕迹池）──
@app.get("/api/profiles/{pid}/cookies/manage")
async def manage_cookies(pid: str, fresh: bool = False):
    """读取并分类全部 cookie（站点聚合模型）。

    缓存策略：运行中窗口每次实时读（cookie 常变）；关闭窗口按 mtime 缓存，
    fresh=true 强制刷新。任何写操作后调 _invalidate_manage(pid)。
    """
    now = _time.time()
    ent = _manage_cache.get(pid)
    running = is_running(pid)
    if not fresh and not running and ent and (now - ent[0]) < _MANAGE_TTL:
        return ent[1]
    async with _lock_for(pid):
        # 双重检查（等锁期间可能已被填充）
        ent = _manage_cache.get(pid)
        if not fresh and not running and ent and (_time.time() - ent[0]) < _MANAGE_TTL:
            return ent[1]
        try:
            async with _CookieCtx(pid) as ctx:
                result = cm.organize_cookies(ctx.cookies)
        except Exception as e:
            # 兜底：读失败时返回空结构，避免 500
            result = {"sites": [], "stats": {"total": 0, "site_count": 0, "login_site_count": 0,
                      "login_count": 0, "tracking_count": 0, "expired_count": 0, "platforms": [],
                      "platform_count": 0, "tracking_site_count": 0}, "_error": str(e)}
        _manage_cache[pid] = (_time.time(), result)
        return result


@app.delete("/api/profiles/{pid}/cookies/tracking")
async def clear_tracking_cookies(pid: str):
    """一键清空痕迹池：仅删除【无锚点站点】的 cookie，保留含锚点站点的全部 cookie（含其痕迹）。"""
    async with _lock_for(pid):
        async with _CookieCtx(pid) as ctx:
            # 先找出所有含锚点的站点 key
            login_keys = set()
            for ck in ctx.cookies:
                if cm.classify_cookie(ck)["is_login"]:
                    login_keys.add(cm.site_of(cm._domain_of(ck))[0])
            # 保留：含锚点站点的全部 cookie
            keep = [ck for ck in ctx.cookies if cm.site_of(cm._domain_of(ck))[0] in login_keys]
            removed = len(ctx.cookies) - len(keep)
            await ctx.write(keep)
        _invalidate_manage(pid)
    return {"ok": True, "removed": removed}


# ── 平台级批量操作 ──
@app.post("/api/profiles/{pid}/cookies/platform/extend")
async def platform_extend(pid: str, data: dict = Body(...)):
    """给某站点所有可延期 cookie 统一延期（整站：登录凭证 + 设备指纹）。

    body: {platform: "douyin", days: 30} 或 {platform:..., expires_ts:...}
    """
    pkey = data.get("platform")
    days = int(data.get("days", 30))
    abs_ts = data.get("expires_ts")
    updated = 0
    async with _lock_for(pid):
        async with _CookieCtx(pid) as ctx:
            for ck in ctx.cookies:
                if cm.site_of(cm._domain_of(ck))[0] == pkey:
                    if abs_ts:
                        ck["expires"] = float(abs_ts)
                    else:
                        ck["expires"] = cm.extend_cookie(ck, days)["expires"]
                    updated += 1
            if updated:
                await ctx.write(ctx.cookies)
        _invalidate_manage(pid)
    if not updated:
        raise HTTPException(404, f"站点 {pkey} 无 cookie")
    return {"ok": True, "platform": pkey, "updated": updated, "days": days}


@app.delete("/api/profiles/{pid}/cookies/platform/{pkey}")
async def platform_delete(pid: str, pkey: str):
    """删除某站点的全部 cookie（整站）。"""
    async with _lock_for(pid):
        async with _CookieCtx(pid) as ctx:
            keep = [ck for ck in ctx.cookies if cm.site_of(cm._domain_of(ck))[0] != pkey]
            removed = len(ctx.cookies) - len(keep)
            if removed:
                await ctx.write(keep)
        _invalidate_manage(pid)
    return {"ok": True, "platform": pkey, "removed": removed}


@app.get("/api/profiles/{pid}/cookies/platform/{pkey}/export")
async def platform_export(pid: str, pkey: str):
    """导出某站点的全部 cookie（整站：登录凭证 + 设备指纹，保证迁移后不被风控）。"""
    async with _lock_for(pid):
        async with _CookieCtx(pid) as ctx:
            allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
            cks = [{k: v for k, v in ck.items() if k in allowed}
                   for ck in ctx.cookies if cm.site_of(cm._domain_of(ck))[0] == pkey]
    if not cks:
        raise HTTPException(404, f"站点 {pkey} 无 cookie")
    return {"platform": pkey, "count": len(cks), "cookies": cks}


@app.post("/api/profiles/{pid}/cookies/export")
async def export_multi_platforms(pid: str, data: dict = Body(...)):
    """批量导出多个站点的全部 cookie。body: {platforms: [key,...]}；空列表=导出全部站点。"""
    want = data.get("platforms") or []
    async with _lock_for(pid):
        async with _CookieCtx(pid) as ctx:
            allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
            grouped: dict[str, list] = {}
            for ck in ctx.cookies:
                k, _ = cm.site_of(cm._domain_of(ck))
                if want and k not in want:
                    continue
                grouped.setdefault(k, []).append({kk: vv for kk, vv in ck.items() if kk in allowed})
    if not grouped:
        raise HTTPException(404, "无可导出的 cookie")
    total = sum(len(v) for v in grouped.values())
    return {"platforms": grouped, "platform_count": len(grouped), "total": total}


# ── 缓存 ──
@app.post("/api/profiles/{pid}/clear-cache")
async def clear_cache(pid: str):
    if is_running(pid): raise HTTPException(400, "运行中无法清缓存")
    ud = STORAGE_ROOT / pid / "userdata" / "Default"
    for sub in ["Cache","Code Cache","GPUCache","DawnWebGPUCache","DawnGraphiteCache"]:
        d = ud / sub
        if d.exists(): shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}

# ---------------- 代理池 API ----------------
class ProxyBody(BaseModel):
    scheme: str = "http"
    name: str = ""
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    remark: str = ""
    udp: bool = False
    ip_version: str = "ipv4"
    check_duplicate: bool = True

class ImportTextBody(BaseModel):
    text: str = ""
    check_duplicate: bool = True


@app.get("/api/proxies")
def api_list_proxies():
    from fingerprint import proxy_manager as pm
    return {"proxies": pm.list_proxies()}


@app.post("/api/proxies")
def api_add_proxy(body: ProxyBody):
    from fingerprint import proxy_manager as pm
    try:
        return {"ok": True, "proxy": pm.add_proxy(
            scheme=body.scheme, name=body.name, host=body.host, port=body.port,
            username=body.username, password=body.password, remark=body.remark,
            udp=body.udp, ip_version=body.ip_version, check_duplicate=body.check_duplicate)}
    except ValueError as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)


@app.put("/api/proxies/{proxy_id}")
def api_update_proxy(proxy_id: str, body: ProxyBody):
    from fingerprint import proxy_manager as pm
    r = pm.update_proxy(proxy_id, scheme=body.scheme, name=body.name, host=body.host,
                        port=body.port, username=body.username, password=body.password,
                        remark=body.remark, udp=body.udp, ip_version=body.ip_version)
    if not r:
        return JSONResponse({"detail": "代理不存在"}, status_code=404)
    return {"ok": True, "proxy": r}


@app.delete("/api/proxies/{proxy_id}")
def api_delete_proxy(proxy_id: str):
    from fingerprint import proxy_manager as pm
    return {"ok": pm.delete_proxy(proxy_id)}


@app.post("/api/proxies/import-text")
def api_import_text(body: ImportTextBody):
    from fingerprint import proxy_manager as pm
    items, errors = [], []
    for i, line in enumerate(body.text.splitlines()):
        if not line.strip():
            continue
        try:
            items.append(pm.parse_proxy_line(line))
        except ValueError as e:
            errors.append(f"第{i+1}行: {e}")
    items = [x for x in items if x]
    result = pm.import_proxies(items, check_duplicate=body.check_duplicate)
    result["errors"] = errors + result["errors"]
    return result


@app.post("/api/proxies/import-csv")
async def api_import_csv(file: UploadFile = File(...), check_duplicate: bool = True):
    from fingerprint import proxy_manager as pm
    import csv, io as _io
    raw = await file.read()
    text = raw.decode("utf-8-sig", errors="replace")
    items, errors = [], []
    reader = csv.reader(_io.StringIO(text))
    for i, row in enumerate(reader):
        if not row or all(not str(c).strip() for c in row):
            continue
        # 跳过表头
        joined = ",".join(row)
        if i == 0 and ("代理" in joined or "scheme" in joined.lower() or "host" in joined.lower() or "主机" in joined):
            continue
        try:
            items.append(pm.parse_proxy_line(joined))
        except ValueError as e:
            errors.append(f"第{i+1}行: {e}")
    items = [x for x in items if x]
    result = pm.import_proxies(items, check_duplicate=check_duplicate)
    result["errors"] = errors + result["errors"]
    return result


@app.get("/api/proxies/csv-template")
def api_csv_template():
    from fastapi.responses import Response
    content = chr(10).join(["代理模式,代理名称,主机,端口,用户名,密码,备注,UDP", "Socks5,示例代理,www.daili.com,1080,work,password,演示,true", "http,示例HTTP,1.2.3.4,8080,,,,false", ""])
    return Response(content=content.encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=proxy_template.csv"})


@app.get("/api/profiles/{pid}/proxies")
def api_window_proxies(pid: str):
    from fingerprint import proxy_manager as pm
    return {"proxies": pm.proxies_for_window(pid)}


@app.get("/api/groups")
async def list_groups(): return {"groups": profiles.list_groups()}

@app.get("/api/avatars")
async def list_avatars(): return {"avatars": profiles.list_avatars()}


# ── IP 查询 ──
from fingerprint import netprobe

@app.post("/api/profiles/{pid}/ip-query")
async def query_profile_ip(pid: str):
    meta = profiles.get(pid)
    if not meta: raise HTTPException(404)
    args = netprobe.resolve_proxy_config(meta)
    result = netprobe.query_ip(*args)
    if result:
        profiles.update(pid, ip_info=result)
    return {"ip_info": result}

@app.post("/api/utils/ip-query-live")
async def query_ip_live(payload: dict = Body(...)):
    """用前端传入的临时代理参数查询 IP（无需先保存 profile）。"""
    mode = str(payload.get("mode", "direct"))
    # 绑定代理：按 proxy_id 从代理池取真实参数
    if mode.startswith("bound:") or payload.get("proxy_id"):
        from fingerprint import proxy_manager as pm
        px_id = payload.get("proxy_id") or mode[6:]
        px = pm.get_proxy(px_id)
        if not px:
            raise HTTPException(404, "绑定代理不存在")
        result = netprobe.query_ip(px["scheme"], px["host"], int(px["port"]),
                                   px.get("username", ""), px.get("password", ""))
        return {"ip_info": result}
    if mode not in ("direct", "system", "socks5", "http", "https"):
        raise HTTPException(400, "invalid proxy mode")
    port_raw = payload.get("port") or 0
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 0
    result = netprobe.query_ip(
        mode,
        str(payload.get("host", "") or ""),
        port,
        str(payload.get("username", "") or ""),
        str(payload.get("password", "") or ""),
    )
    return {"ip_info": result}

@app.post("/api/utils/open-downloads")
async def open_downloads(payload: dict = Body(default={})):
    """在资源管理器中打开下载目录。
    payload: {pid: "窗口ID"} 打开该窗口的下载目录;{which: "browser"} 打开浏览器下载根目录。"""
    pid = str(payload.get("pid") or "")
    which = str(payload.get("which") or "agent")
    if which == "browser":
        root = profiles.get_settings().get("browser_download_root") or profiles.DEFAULT_BROWSER_DL_ROOT
    else:
        root = str(Path.home() / "Downloads" / "Gemini-API Download")
    d = Path(root) / pid if pid else Path(root)
    d.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(d))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(d)])
    else:
        subprocess.Popen(["xdg-open", str(d)])
    return {"ok": True, "dir": str(d)}

@app.get("/api/profiles/{pid}/welcome-data")
async def welcome_data(pid: str):
    """欢迎页数据：窗口信息 + 当前代理出口 IP 检测。"""
    meta = profiles.get(pid)
    if not meta:
        raise HTTPException(404, "profile 不存在")
    # 解析最终代理（含绑定代理故障转移），再查出口 IP
    proxy = _resolve_proxy_with_failover(pid, meta)
    mode = proxy.get("mode", "direct")
    if mode.startswith("bound:"):
        ip_info = netprobe.query_ip("http", proxy.get("host",""), int(proxy.get("port") or 0),
                                    proxy.get("username",""), proxy.get("password",""))
        # 用代理实际 scheme 更准确
        from fingerprint import proxy_manager as pm
        px = pm.get_proxy(mode[6:])
        if px:
            ip_info = netprobe.query_ip(px["scheme"], px["host"], int(px["port"]),
                                        px.get("username",""), px.get("password",""))
    else:
        args = netprobe.resolve_proxy_config({"proxy": proxy})
        ip_info = netprobe.query_ip(*args)
    if ip_info:
        profiles.update(pid, ip_info=ip_info)
    # 代理展示信息（不泄露密码）
    proxy_disp = {"mode": mode}
    if mode.startswith("bound:"):
        from fingerprint import proxy_manager as pm
        px = pm.get_proxy(mode[6:])
        proxy_disp["label"] = (px["name"] + " · " + px["scheme"] + "://" + px["host"] + ":" + str(px["port"])) if px else mode
    elif mode in ("socks5","http","https"):
        proxy_disp["label"] = mode + "://" + str(proxy.get("host","")) + ":" + str(proxy.get("port",""))
    elif mode == "system":
        proxy_disp["label"] = "跟随系统代理"
    else:
        proxy_disp["label"] = "直连"
    basic = meta.get("basic") or {}
    os_info = meta.get("os") or {}
    # 头像：emoji 或上传图片
    avatar = basic.get("avatar") or ""
    avatar_img = ""
    if avatar.startswith("/avatars/"):
        avatar_img = avatar
        avatar = ""
    else:
        # 检查是否有上传的头像文件
        for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            if (AVATARS_DIR / (pid + ext)).exists():
                avatar_img = "/avatars/" + pid + ext
                break
    kv = meta.get("kernel_version") or ""
    kernel_disp = ("Chrome " + kv) if kv else "系统默认"
    return {
        "pid": pid,
        "seq": meta.get("seq"),
        "name": basic.get("name") or pid,
        "group": basic.get("group") or "默认",
        "remark": basic.get("note") or "",
        "os": (os_info.get("family") or "windows"),
        "kernel": kernel_disp,
        "ua": meta.get("ua") or "",
        "avatar": avatar,
        "avatar_img": avatar_img,
        "launch_time": window_state(pid).get("started_at") or "",
        "proxy": proxy_disp,
        "ip_info": ip_info,
    }

@app.post("/api/profiles/{pid}/randomize")
async def randomize_fingerprint(pid: str):
    if is_running(pid): raise HTTPException(400, "运行中无法随机化")
    from fingerprint.fingerprint_gen import randomize_v2
    randomize_v2(pid)
    return {"ok": True}

@app.get("/api/profiles/{pid}")
async def get_profile(pid: str):
    m = profiles.get(pid)
    if not m: raise HTTPException(404)
    return m

# ── 兼容旧 API ──
@app.get("/api/browser/accounts")
async def legacy_accounts():
    pl = profiles.list_all()
    return {"accounts": [{"id": p["id"], "has_fingerprint": True, "active": is_running(p["id"])} for p in pl]}


def start(port: int = 4446):
    import uvicorn
    print(f"指纹浏览器管理服务 → http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

# ── 内核管理 ──
@app.get("/api/kernels")
async def list_kernels():
    from fingerprint.kernel_manager import list_installed
    return {"kernels": list_installed()}

@app.post("/api/kernels/{ver}/download")
async def download_kernel_api(ver: str):
    from fingerprint.kernel_manager import download_kernel
    result = download_kernel(ver)
    return result

@app.delete("/api/kernels/{ver}")
async def remove_kernel_api(ver: str):
    from fingerprint.kernel_manager import remove_kernel
    ok = remove_kernel(ver)
    return {"ok": ok}

@app.get("/api/kernels/{ver}/ua")
async def get_kernel_ua(ver: str, os_family: str = "windows"):
    from fingerprint.kernel_manager import get_ua_for_kernel
    return {"ua": get_ua_for_kernel(ver, os_family)}

# ── 头像上传 ──
AVATARS_DIR = Path(__file__).parent.parent.parent / "html" / "avatars"
AVATARS_DIR.mkdir(parents=True, exist_ok=True)

@app.post("/api/profiles/{pid}/avatar")
async def upload_avatar(pid: str, file: UploadFile = File(...)):
    ext = Path(file.filename).suffix or ".png"
    dest = AVATARS_DIR / (pid + ext)
    content = await file.read()
    dest.write_bytes(content)
    return {"avatar": "/avatars/" + dest.name}

ICONS_DIR = HTML_DIR / "icons"

@app.get("/icons/{filename}")
async def serve_icon(filename: str):
    f = ICONS_DIR / filename
    if f.exists() and f.is_file():
        return FileResponse(f)
    raise HTTPException(404)

@app.get("/avatars/{filename}")
async def serve_avatar(filename: str):
    from fastapi.responses import FileResponse
    f = AVATARS_DIR / filename
    if f.exists():
        return FileResponse(f)
    raise HTTPException(404)

# ── Cookie 机器人（批量预热指定网址，产生真实访问记录）──

# ── 平台账号管理 ──
async def _login_hosts_of(pid: str) -> set:
    """读取该窗口 Cookie 分组，返回 has_login 站点的 host 集合（读失败返回空集，不 500）。"""
    try:
        async with _CookieCtx(pid) as ctx:
            org = cm.organize_cookies(ctx.cookies)
        hosts = set()
        for site in org.get("sites", []):
            if site.get("has_login"):
                h = (site.get("name") or "").strip().lower()
                if h:
                    hosts.add(h)
        return hosts
    except Exception:
        return set()


def _host_in_login(account_host, login_hosts) -> bool:
    """账号 host 是否命中登录站点（后缀匹配，复用 am 安全归一）。"""
    ah = am._norm_host(account_host) if account_host else ""
    if not ah:
        return False
    for lh in login_hosts:
        lh2 = am._norm_host(lh)
        if not lh2:
            continue
        if ah == lh2 or ah.endswith("." + lh2) or lh2.endswith("." + ah):
            return True
    return False


@app.get("/api/profiles/{pid}/accounts")
async def list_accounts(pid: str):
    """账号列表（不含密码），附带 login_status: logged_in / not_logged。"""
    accounts = am.list_accounts(pid)
    try:
        login_hosts = await _login_hosts_of(pid)
        for a in accounts:
            a["login_status"] = "logged_in" if _host_in_login(a.get("host"), login_hosts) else "not_logged"
    except Exception:
        for a in accounts:
            a["login_status"] = "not_logged"
    return {"accounts": accounts}


@app.post("/api/profiles/{pid}/accounts")
async def add_account(pid: str, data: dict = Body(...)):
    platform = (data.get("platform") or "").strip()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    logo = data.get("logo") or ""
    totp_secret = (data.get("totp_secret") or "").strip()
    if not platform or not username or not password:
        raise HTTPException(400, "平台、用户名、密码均不能为空")
    acc = am.add_account(pid, platform, username, password, logo, totp_secret)
    return {"ok": True, "account": acc}


@app.patch("/api/profiles/{pid}/accounts/{acc_id}")
async def update_account(pid: str, acc_id: str, data: dict = Body(...)):
    ok = am.update_account(pid, acc_id, **data)
    if not ok:
        raise HTTPException(404, "账号不存在")
    return {"ok": True}


@app.delete("/api/profiles/{pid}/accounts/{acc_id}")
async def delete_account(pid: str, acc_id: str):
    ok = am.delete_account(pid, acc_id)
    if not ok:
        raise HTTPException(404, "账号不存在")
    return {"ok": True}


@app.post("/api/profiles/{pid}/accounts/fetch-logo")
async def fetch_logo_api(pid: str, data: dict = Body(...)):
    """抓取平台 LOGO 并缓存，返回 {ok, logo, host}。"""
    url = (data.get("platform") or "").strip()
    if not url:
        raise HTTPException(400, "平台地址不能为空")
    result = await logo_fetcher.fetch_logo(pid, url)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "获取失败"))
    return result


@app.get("/api/profiles/{pid}/accounts/logo/{fname}")
async def get_logo(pid: str, fname: str):
    path = am.logo_path(pid, fname)
    if not path:
        raise HTTPException(404, "LOGO 不存在")
    return FileResponse(path)


@app.get("/api/profiles/{pid}/accounts/match")
async def match_account(pid: str, url: str = Query(...)):
    """根据 URL 匹配账号（含明文密码，供自动填充）。"""
    acc = am.find_account_for_url(pid, url)
    if not acc:
        return {"found": False}
    return {"found": True, "account": acc}


# ─────────────────────────── Cookie 机器人（批量预热指定网址，产生真实访问记录）───────────────────────────
# 参考 OpenMedia media_core/fingerprint/server.py 的 cookie-robot 实现，适配本项目的窗口子进程架构：
# Gemini-API_old 的账号窗口是独立 pythonw 子进程（server 内无 _active 注册表），
# 因此对「运行中」窗口跳过并提示；对「已关闭」窗口临时 headless 启动 FingerprintBrowser 预热，跑完自动关闭。
_robot_jobs: dict[str, dict] = {}


async def _robot_warm(pid: str, urls: list[str], dwell: float, job: dict):
    """单个窗口的预热协程：临时 headless 启动 → 依次访问各网址并模拟真人滚动停留。结果写回 job。"""
    browser = None
    try:
        if is_running(pid):
            job[pid] = {"status": "skipped_running", "done": 0, "total": len(urls), "errors": [],
                        "note": "窗口运行中,跳过(请先关闭窗口再预热)"}
            return
        browser = FingerprintBrowser(pid)
        await browser.launch(headless=True)
        job[pid] = {"status": "running", "done": 0, "total": len(urls), "errors": []}
        for i, url in enumerate(urls):
            try:
                pg = await browser.new_page()
                await pg.goto(url, wait_until="domcontentloaded", timeout=30000)
                # 模拟真人:滚动 + 停留(上/下两段,更接近真实浏览)
                try:
                    await pg.evaluate("window.scrollTo(0, document.body.scrollHeight/3)")
                    await _asyncio.sleep(dwell / 2)
                    await pg.evaluate("window.scrollTo(0, document.body.scrollHeight*2/3)")
                    await _asyncio.sleep(dwell / 2)
                except Exception:
                    await _asyncio.sleep(dwell)
                await pg.close()
                job[pid]["done"] = i + 1
            except Exception as e:
                job[pid]["errors"].append(f"{url}: {str(e)[:80]}")
                job[pid]["done"] = i + 1
        job[pid]["status"] = "done" if not job[pid]["errors"] else "done_with_errors"
    except Exception as e:
        job[pid] = {"status": "failed", "done": 0, "total": len(urls), "errors": [str(e)[:120]]}
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


@app.post("/api/cookie-robot/run")
async def cookie_robot_run(data: dict = Body(...)):
    """启动 Cookie 机器人。

    body: {pids: [...], urls: [...], dwell: 秒(默认4, 每站停留)}
    返回 job_id，前端轮询 /api/cookie-robot/status/{job_id}。
    """
    import uuid as _uuid
    pids = [p for p in (data.get("pids") or []) if p]
    urls = [u.strip() for u in (data.get("urls") or []) if u.strip()]
    dwell = float(data.get("dwell", 4))
    if not pids:
        raise HTTPException(400, "未选择窗口")
    if not urls:
        raise HTTPException(400, "未填写网址")
    dwell = max(1.0, min(dwell, 60.0))
    # 补协议
    urls = [u if u.startswith(("http://", "https://")) else "https://" + u for u in urls]

    job_id = _uuid.uuid4().hex[:8]
    job: dict = {"_meta": {"pids": pids, "urls": urls, "dwell": dwell, "status": "running"}}
    _robot_jobs[job_id] = job

    async def _runner():
        # 串行跑各窗口（避免瞬间起多个浏览器占资源）
        for pid in pids:
            await _robot_warm(pid, urls, dwell, job)
        job["_meta"]["status"] = "done"

    _asyncio.create_task(_runner())
    return {"job_id": job_id, "pids": pids, "url_count": len(urls)}


@app.get("/api/cookie-robot/status/{job_id}")
async def cookie_robot_status(job_id: str):
    job = _robot_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job 不存在")
    return job

