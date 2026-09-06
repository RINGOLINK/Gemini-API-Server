# -*- coding: utf-8 -*-
"""
gemini_app.py — Gemini 桌面端(单进程、无 cmd 窗口)

结构:
- 线程A: FastAPI 服务端(127.0.0.1:4444,uvicorn 进程内运行)
- 线程B: 控制台 UI 服务器(127.0.0.1:4445,提供 HTML + /api/status /api/logs /api/action /api/proxy /api/feature)
- 主线程: patchright 持久内核窗口(3 个固定标签:控制台/Gemini/管理面板)+ 桥循环(保活/采集推送/额度)

特性:
- 单实例保护(mutex):重复启动弹窗提示并退出,避免多实例抢端口/profile
- 内核自愈:浏览器窗口被关闭后自动重启并恢复登录
- 日志去重 + 级别着色,代理故障不刷屏

启动: pythonw gemini_app.py(无任何 cmd 窗口)
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# pythonw 无控制台:sys.stdout/stderr 为 None 会让 loguru(basicConfig 等)崩溃
# (gemini_webapi.set_log_level 会把 sink 挂到 sys.stderr → TypeError)。
# 替换为 devnull 流,保证无窗口运行时一切日志库正常工作。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser_profile"
BROWSERS_DIR = BASE_DIR / "browsers"
UI_PORT = int(os.environ.get("GEMINI_UI_PORT", "4445"))
SERVER_PORT = int(os.environ.get("PORT", "4444"))

import cookie_bridge as cb  # 复用 read_env/write_env/ServerClient/parse_proxy_url 等

LOG_BUFFER: list[dict] = []
LOG_LOCK = threading.Lock()

LEVEL_COLOR = {"ERROR": "err", "WARNING": "warn", "INFO": "info", "DEBUG": "info"}


def app_log(msg: str, level: str = "INFO"):
    """写日志(UI 缓冲区去重 + 落盘)"""
    with LOG_LOCK:
        if LOG_BUFFER and LOG_BUFFER[-1]["msg"] == msg:
            LOG_BUFFER[-1]["count"] = LOG_BUFFER[-1].get("count", 1) + 1
            LOG_BUFFER[-1]["time"] = cb.now_str()
        else:
            LOG_BUFFER.append({"time": cb.now_str(), "msg": msg, "level": level, "count": 1})
            if len(LOG_BUFFER) > 500:
                del LOG_BUFFER[: len(LOG_BUFFER) - 500]
    cb.log_to_file(msg)


class UILogHandler(logging.Handler):
    """把 uvicorn/main 的日志也收进 UI 缓冲区(带级别)"""

    def emit(self, record):
        try:
            app_log(f"[{record.levelname}] {record.getMessage()}", level=record.levelname)
        except Exception:
            pass


STATUS: dict = {
    "mode": "启动中", "kernel": "内核启动中...", "login": "检测中", "login_color": "gray",
    "server": "未连接", "quota": "未获取", "psid": "-", "psidts": "-",
    "push_time": "从未", "push_ok": None, "keepalive_time": "从未", "proxy": "检测中",
    "thinking": True, "parallel": True, "proxy_parsed": None, "client_mode": "unknown",
    "current_account": "",
}


def init_status_from_env():
    env = cb.read_env()
    STATUS["thinking"] = env.get("ENABLE_THINKING", "true").lower() == "true"
    STATUS["parallel"] = env.get("PARALLEL_TOOL_CALLS", "true").lower() == "true"
    proxy = env.get("GEMINI_PROXY", "").strip()
    STATUS["proxy"] = "已启用(仅服务端)" if proxy else "未启用"
    STATUS["proxy_parsed"] = cb.parse_proxy_url(proxy)
    STATUS["api_key"] = env.get("API_KEY", "")
    STATUS["base_url"] = f"http://127.0.0.1:{SERVER_PORT}/v1"


def is_already_running() -> bool:
    """单实例保护:mutex 检测(重复启动时弹窗提示并退出)"""
    try:
        kernel32 = ctypes.windll.kernel32
        mutex = kernel32.CreateMutexW(None, False, "Local\\GeminiDesktopApp4444")
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return True
        return False
    except Exception:
        return False


def show_message_box(text: str, title: str = "Gemini 桌面端"):
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


# ---------------------------------------------------------------- UI 服务器
class UIHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body, ctype: str = "application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = (BASE_DIR / "ui_console.html").read_text(encoding="utf-8")
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            with LOG_LOCK:
                self._send(200, json.dumps(STATUS, ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/logs":
            with LOG_LOCK:
                self._send(200, json.dumps({"logs": LOG_BUFFER[-200:]}, ensure_ascii=False).encode("utf-8"))
        else:
            self._send(404, b'{"detail":"Not Found"}')

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            payload = {}
        if self.path == "/api/action":
            action = payload.get("action", "")
            if action == "push_cookie":
                APP_QUEUE.put(("push_cookie", None))
                self._send(200, '{"ok":true,"message":"已提交推送"}')
            elif action == "keepalive":
                APP_QUEUE.put(("keepalive", None))
                self._send(200, '{"ok":true,"message":"已提交保活"}')
            elif action == "open_login_tab":
                APP_QUEUE.put(("open_login_tab", None))
                self._send(200, '{"ok":true,"message":"已打开登录标签"}')
            else:
                self._send(400, b'{"detail":"unknown action"}')
        elif self.path == "/api/proxy":
            enabled = payload.get("enabled")
            if enabled:
                scheme = payload.get("scheme", "socks5h")
                host = payload.get("host", "").strip()
                port = payload.get("port", "").strip()
                user = payload.get("user", "").strip()
                pw = payload.get("pass", "").strip()
                auth = f"{user}:{pw}@" if user else ""
                proxy = f"{scheme}://{auth}{host}:{port}" if host and port else ""
            else:
                proxy = ""
            cb.write_env({"GEMINI_PROXY": proxy})
            STATUS["proxy"] = "已启用(仅服务端)" if proxy else "未启用"
            STATUS["proxy_parsed"] = cb.parse_proxy_url(proxy)
            server = cb.ServerClient(cb.load_api_key())
            ok = server.push_proxy(proxy)
            app_log(f"代理已更新: {proxy or '(关闭)'} -> 服务端同步{'成功' if ok else '失败'}")
            self._send(200, json.dumps({"ok": ok, "message": f"代理已更新并同步{'成功' if ok else '失败'}"}, ensure_ascii=False).encode())
        elif self.path == "/api/feature":
            feature = payload.get("feature")
            enabled = bool(payload.get("enabled"))
            if feature in ("thinking", "parallelTools"):
                server = cb.ServerClient(cb.load_api_key())
                ok = server.set_feature(feature, enabled)
                if feature == "thinking":
                    STATUS["thinking"] = enabled
                else:
                    STATUS["parallel"] = enabled
                app_log(f"开关 {feature} -> {enabled}: {'成功' if ok else '失败'}")
                self._send(200, json.dumps({"ok": ok}, ensure_ascii=False).encode())
            else:
                self._send(400, b'{"detail":"unknown feature"}')
        else:
            self._send(404, b'{"detail":"Not Found"}')


def start_ui_server():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", UI_PORT), UIHandler)
        app_log(f"控制台 UI: http://127.0.0.1:{UI_PORT}")
        server.serve_forever()
    except OSError as e:
        app_log(f"控制台 UI 启动失败(端口 {UI_PORT} 被占用): {e}")
        STATUS["ui_error"] = f"UI 端口被占用:{e}"
    except Exception as e:
        app_log(f"控制台 UI 异常: {e}")


# ---------------------------------------------------------------- 服务端线程
def _server_alive() -> bool:
    """检测 4444 是否已被一个可用的服务端占用(复用,避免双实例)"""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{SERVER_PORT}/v1/models", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def start_api_server():
    try:
        if _server_alive():
            app_log(f"检测到已有服务端在 http://127.0.0.1:{SERVER_PORT},直接复用")
            return
        import uvicorn
        root = logging.getLogger()
        root.addHandler(UILogHandler())
        import main as server_main
        config = uvicorn.Config(server_main.app, host="127.0.0.1", port=SERVER_PORT, log_level="warning")
        uvicorn.Server(config).run()
    except Exception:
        import traceback
        err = traceback.format_exc()
        app_log(f"服务端线程异常: {err[-600:]}")
        try:
            (BASE_DIR / "_server_crash.log").write_text(err, encoding="utf-8")
        except Exception:
            pass


FP_PORT = 4446


def _fp_alive() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{FP_PORT}/api/browser/status", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def start_fingerprint_server():
    """启动指纹浏览器管理服务(4446),已运行则复用"""
    try:
        if _fp_alive():
            app_log(f"检测到已有指纹管理服务在 http://127.0.0.1:{FP_PORT},直接复用")
            return
        import uvicorn
        from fingerprint.server import app as fp_app
        root = logging.getLogger()
        root.addHandler(UILogHandler())
        config = uvicorn.Config(fp_app, host="127.0.0.1", port=FP_PORT, log_level="warning")
        uvicorn.Server(config).run()
    except Exception:
        import traceback
        err = traceback.format_exc()
        app_log(f"指纹管理服务异常: {err[-600:]}")
        try:
            (BASE_DIR / "_fp_crash.log").write_text(err, encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------- 桥循环
APP_QUEUE: queue.Queue = queue.Queue()


def read_cookies(context) -> dict:
    try:
        return {c["name"]: c["value"] for c in context.cookies()}
    except Exception:
        return {}


def refresh_psid_status(context, cookies=None):
    cks = cookies if cookies is not None else read_cookies(context)
    STATUS["psid"] = f"存在({cks.get('__Secure-1PSID', '')[:6]}…)" if cks.get("__Secure-1PSID") else "缺失!"
    STATUS["psidts"] = f"存在({cks.get('__Secure-1PSIDTS', '')[:6]}…)" if cks.get("__Secure-1PSIDTS") else "缺失!"


def ensure_browser_login(context, tab_gemini):
    """浏览器 profile 无 PSID 时,从 .env 注入(事故后 profile 为空也能直接登录)"""
    cks = read_cookies(context)
    if cks.get("__Secure-1PSID") and cks.get("__Secure-1PSIDTS"):
        STATUS["login"] = "已登录"
        STATUS["login_color"] = "green"
        return
    env = cb.read_env()
    psid = env.get("SECURE_1PSID", "")
    psidts = env.get("SECURE_1PSIDTS", "")
    if not (psid and psidts):
        return
    try:
        context.add_cookies([
            {"name": "__Secure-1PSID", "value": psid, "domain": ".google.com", "path": "/", "secure": True, "sameSite": "None"},
            {"name": "__Secure-1PSIDTS", "value": psidts, "domain": ".google.com", "path": "/", "secure": True, "sameSite": "None"},
        ])
        # 注入后全新导航(替代 reload,reload 可能丢弃 PSIDTS),并校验会话被接受
        tab_gemini.goto(cb.GEMINI_HOME, timeout=60000, wait_until="domcontentloaded")
        time.sleep(2)
        cks = read_cookies(context)
        ok_psid = bool(cks.get("__Secure-1PSID"))
        ok_psidts = bool(cks.get("__Secure-1PSIDTS"))
        if ok_psid and ok_psidts:
            STATUS["login"] = "已登录"
            STATUS["login_color"] = "green"
            app_log("已从 .env 恢复登录,浏览器标签页已登录")
        else:
            app_log(f"注入 cookie 不完整(PSID={'有' if ok_psid else '无'}, PSIDTS={'有' if ok_psidts else '无'}),请在 Gemini 标签页登录")
            STATUS["login"] = "请在 Gemini 标签页登录"
            STATUS["login_color"] = "orange"
    except Exception as e:
        app_log(f"注入 cookie 失败: {e}")


def apply_quota(q):
    """解析额度接口返回,更新 STATUS"""
    if q.get("client_mode"):
        STATUS["client_mode"] = q["client_mode"]
    if q.get("active_account"):
        STATUS["current_account"] = q["active_account"]
    if not q.get("available"):
        return
    usage = q.get("usage_info", {}) or {}
    daily = usage.get("daily") or usage.get("current_5h") or usage
    rem = daily.get("remaining_credits")
    pct = daily.get("usage_percentage")
    reset = daily.get("reset_at", "")
    parts = []
    if rem is not None:
        parts.append(f"剩余 {rem}")
    if pct is not None:
        parts.append(f"已用 {pct}%")
    if reset:
        parts.append(f"重置 {reset[:16]}")
    STATUS["quota"] = " / ".join(parts)
    STATUS["quota_pct"] = pct
    STATUS["quota_remaining"] = rem


def initial_push(context, server):
    proxy = STATUS["proxy_parsed"]
    if proxy:
        server.push_proxy(f"{proxy['scheme']}://{proxy['host']}:{proxy['port']}")
    cookies = read_cookies(context)
    refresh_psid_status(context, cookies)
    psid = cookies.get("__Secure-1PSID", "")
    psidts = cookies.get("__Secure-1PSIDTS", "")
    if psid and psidts:
        ok = server.push_cookies(psid, psidts)
        if ok:
            STATUS["push_time"] = cb.now_str()
            STATUS["push_ok"] = True
            STATUS["login"] = "已登录"
            STATUS["login_color"] = "green"
            app_log(f"启动推送 cookie 成功(1PSID {psid[:6]}…)")
            q = server.get_quota()
            if q:
                apply_quota(q)
        else:
            app_log("启动推送 cookie 未通过服务端验证(代理暂不可用或 cookie 过期,将自动重试)")
            STATUS["login"] = "验证中(代理暂不可用会自动重试)"
            STATUS["login_color"] = "orange"


def open_app_window(p):
    """启动内核窗口:3 个固定标签(控制台/Gemini/管理面板)。返回 (context, tab_gemini)"""
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        no_viewport=True,
        args=["--start-maximized", "--disable-blink-features=AutomationControlled",
              "--explicitly-allowed-ports=4444,4445,4446"],
    )
    pages = context.pages
    tab_console = pages[0] if pages else context.new_page()
    tab_gemini = context.new_page()
    tab_admin = context.new_page()
    tab_console.goto(f"http://127.0.0.1:{UI_PORT}/", timeout=45000)
    tab_gemini.goto(cb.GEMINI_HOME, timeout=60000)
    # 管理面板:服务端启动可能被代理阻塞,容错重试,失败也不影响主流程
    admin_ok = False
    for _ in range(20):
        try:
            tab_admin.goto(f"http://127.0.0.1:{SERVER_PORT}/admin", timeout=15000, wait_until="domcontentloaded")
            admin_ok = True
            break
        except Exception:
            time.sleep(3)
    if not admin_ok:
        app_log("管理面板标签页暂未加载(服务端启动慢),可在浏览器里手动刷新")
    STATUS["kernel"] = "运行中"
    STATUS["mode"] = "桌面端运行中"
    app_log("App 窗口已打开(3 个标签:控制台 / Gemini / 管理面板)")
    return context, tab_gemini


def do_keepalive(context, tab_gemini):
    """后台隐藏标签页保活(触发浏览器续期 + 检测会话)"""
    try:
        tab = context.new_page()
        try:
            tab.goto(cb.GEMINI_HOME, timeout=45000, wait_until="domcontentloaded")
            time.sleep(random.uniform(3, 6))
            if tab.url.startswith("https://accounts.google"):
                tab_gemini.bring_to_front()
                STATUS["login"] = "未登录(请在 Gemini 标签页登录)"
                STATUS["login_color"] = "red"
                app_log("保活发现会话已失效,请在 Gemini 标签页重新登录")
            else:
                STATUS["login"] = "已登录"
                STATUS["login_color"] = "green"
        finally:
            tab.close()
        STATUS["keepalive_time"] = cb.now_str()
        app_log(f"保活完成({cb.now_str()})")
    except Exception as e:
        app_log(f"保活失败: {e}")


def bridge_loop(p):
    server = cb.ServerClient(cb.load_api_key())
    now0 = time.time()
    last_keepalive = last_push = last_probe = last_quota = now0
    last_psid = last_psidts = ""
    last_success = now0  # 最近一次成功推送/额度查询时间(保活自适应依据)

    # 连接服务端(服务端启动可能被代理阻塞最长约 25s,耐心重试)
    for attempt in range(20):
        if server.login():
            STATUS["server"] = f"已连接(http://127.0.0.1:{SERVER_PORT})"
            break
        time.sleep(3)

    # 打开窗口
    context, tab_gemini = open_app_window(p)
    ensure_browser_login(context, tab_gemini)
    initial_push(context, server)
    last_success = time.time()  # 启动视为活跃,避免立刻触发保活

    while True:
        try:
            # 内核存活检测(窗口被用户关闭时自动重启恢复)
            # 注意:pages 属性与 close 事件在浏览器被外部杀死后不可靠,cookies() 真实 I/O 才会抛 TargetClosedError
            try:
                _ = context.cookies()
                kernel_alive = True
            except Exception:
                kernel_alive = False
            if not kernel_alive:
                app_log("检测到浏览器窗口已关闭,自动重启中...")
                STATUS["kernel"] = "已关闭,自动重启中..."
                STATUS["mode"] = "内核重启中"
                try:
                    context.close()
                except Exception:
                    pass
                time.sleep(3)
                context, tab_gemini = open_app_window(p)
                ensure_browser_login(context, tab_gemini)
                initial_push(context, server)
                last_success = time.time()
                continue

            # 处理 UI 动作
            try:
                cmd = APP_QUEUE.get_nowait()
            except queue.Empty:
                cmd = None
            if cmd:
                kind, _ = cmd
                if kind == "push_cookie":
                    cookies = read_cookies(context)
                    psid = cookies.get("__Secure-1PSID", "")
                    psidts = cookies.get("__Secure-1PSIDTS", "")
                    if psid and psidts:
                        ok = server.push_cookies(psid, psidts)
                        STATUS["push_time"] = cb.now_str()
                        STATUS["push_ok"] = ok
                        app_log(f"手动推送 cookie: {'成功' if ok else '未通过验证(代理暂不可用或已过期)'}")
                        if ok:
                            last_success = time.time()
                            STATUS["login"] = "已登录"
                            STATUS["login_color"] = "green"
                        else:
                            STATUS["login"] = "验证中(代理暂不可用会自动重试)"
                            STATUS["login_color"] = "orange"
                elif kind == "keepalive":
                    do_keepalive(context, tab_gemini)
                elif kind == "open_login_tab":
                    tab_gemini.bring_to_front()
                    app_log("已切换到 Gemini 标签页")

            now = time.time()
            # 探活:只读 cookie,零 Google 流量;2~4 小时一次带抖动(登录态可保持数周,无需高频)
            if now - last_probe >= 120 * 60 * random.uniform(0.9, 1.3):
                last_probe = now
                refresh_psid_status(context)
            # 保活(自适应):浏览器侧会话可保持数周;服务端库本身每 10 分钟自动旋转 SIDTS。
            # 仅当 ①距上次保活超 6~12h(抖动)且 ②最近 6h 内无成功推送(服务空闲)时才触发;
            # 服务活跃时自动跳过,避免无谓的 Google 流量。
            keepalive_period = 8 * 60 * 60 * random.uniform(0.9, 1.3)  # 432~624 分钟
            if now - last_keepalive >= keepalive_period and (now - last_success > 6 * 60 * 60 or last_success == 0):
                last_keepalive = now
                do_keepalive(context, tab_gemini)
            if now - last_push >= 5 * 60 * random.uniform(0.9, 1.3):
                last_push = now
                cookies = read_cookies(context)
                refresh_psid_status(context, cookies)
                psid = cookies.get("__Secure-1PSID", "")
                psidts = cookies.get("__Secure-1PSIDTS", "")
                if psid and (psid != last_psid or psidts != last_psidts):
                    ok = server.push_cookies(psid, psidts)
                    STATUS["push_time"] = cb.now_str()
                    STATUS["push_ok"] = ok
                    if ok:
                        last_success = now
                        last_psid, last_psidts = psid, psidts
                        STATUS["login"] = "已登录"
                        STATUS["login_color"] = "green"
                        app_log(f"cookie 已推送(1PSID {psid[:6]}…)")
                    elif STATUS["server"].startswith("已连接"):
                        STATUS["login"] = "验证中(代理暂不可用会自动重试)"
                        STATUS["login_color"] = "orange"
                        app_log("cookie 推送未通过验证(代理暂不可用或 cookie 过期,将自动重试)")
            if now - last_quota >= 30 * 60 * random.uniform(0.9, 1.3):
                last_quota = now
                q = server.get_quota()
                if q:
                    apply_quota(q)
            time.sleep(3)
        except Exception as e:
            app_log(f"桥循环异常: {e}")
            time.sleep(3)


def main():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)
    init_status_from_env()

    if is_already_running():
        show_message_box("Gemini 桌面端已在运行中。\n请直接使用已打开的窗口;若窗口无响应请先关闭旧窗口再启动。")
        return

    # 服务端线程
    t_server = threading.Thread(target=start_api_server, daemon=True)
    t_server.start()
    # 指纹浏览器管理服务线程(4446)
    t_fp = threading.Thread(target=start_fingerprint_server, daemon=True)
    t_fp.start()
    # UI 线程
    t_ui = threading.Thread(target=start_ui_server, daemon=True)
    t_ui.start()
    time.sleep(2)

    try:
        from patchright.sync_api import sync_playwright
        with sync_playwright() as p:
            bridge_loop(p)  # 主线程持有 playwright,阻塞运行
    except Exception as e:
        app_log(f"App 异常退出: {e}")
        import traceback
        traceback.print_exc()
    finally:
        app_log("App 已退出")


if __name__ == "__main__":
    main()
