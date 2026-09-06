# -*- coding: utf-8 -*-
"""
gemini_core.py —— Gemini 项目后台核心(无窗口)

- 线程A: FastAPI 服务端(127.0.0.1:4444)
- 线程B: 指纹浏览器管理服务(127.0.0.1:4446)
- 线程C: 主界面服务(127.0.0.1:4445,看板/设置/指纹管理 API + 页面)
- 主线程: 守护循环(服务状态/账号 cookie 活性统计/自动保活触发/日志收集)

说明:桥不再需要浏览器内核——gemini_webapi 库内置 10 分钟自动旋转 SIDTS(保活);
登录/保活的浏览器侧动作由各指纹账号窗口承担;本核心只做状态统计与动作调度。

由启动器(launcher.py)管理生命周期:pythonw gemini_core.py
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

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
UI_PORT = int(os.environ.get("GEMINI_UI_PORT", "4445"))
SERVER_PORT = int(os.environ.get("PORT", "4444"))
FP_PORT = 4446

import cookie_bridge as cb

LOG_BUFFER: list[dict] = []
LOG_LOCK = threading.Lock()


def app_log(msg: str, level: str = "INFO"):
    with LOG_LOCK:
        if LOG_BUFFER and LOG_BUFFER[-1]["msg"] == msg:
            LOG_BUFFER[-1]["count"] = LOG_BUFFER[-1].get("count", 1) + 1
            LOG_BUFFER[-1]["time"] = cb.now_str()
        else:
            LOG_BUFFER.append({"time": cb.now_str(), "msg": msg, "level": level, "count": 1})
            if len(LOG_BUFFER) > 500:
                del LOG_BUFFER[: len(LOG_BUFFER) - 500]
    cb.log_to_file(msg)


# 服务端进程日志(main/uvicorn 的 logger 输出,类似命令行日志;独立于桥日志)
SERVER_LOGS: list[dict] = []
SERVER_LOG_LOCK = threading.Lock()


def _server_log(level: str, msg: str):
    with SERVER_LOG_LOCK:
        SERVER_LOGS.append({"time": cb.now_str(), "msg": f"[{level}] {msg}", "level": level})
        if len(SERVER_LOGS) > 800:
            del SERVER_LOGS[: len(SERVER_LOGS) - 800]


class UILogHandler(logging.Handler):
    """捕获 main/uvicorn 的日志 → 服务端进程日志(单独存,不混入桥日志)"""
    def emit(self, record):
        try:
            _server_log(record.levelname, record.getMessage())
        except Exception:
            pass


class ServerOutputStream:
    """把服务端进程的 stdout/stderr(print + 未走 logging 的输出)重定向进 SERVER_LOGS。
    用于还原「服务端命令行日志」:Starting/Admin panel/print 等非 logger 输出也能抓到。"""
    def __init__(self):
        self._buf = ""

    def write(self, text):
        if not text:
            return 0
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.rstrip():
                _server_log("INFO", line.rstrip())
        return len(text)

    def flush(self):
        try:
            if self._buf.strip():
                _server_log("INFO", self._buf.rstrip())
                self._buf = ""
        except Exception:
            pass


STATUS: dict = {
    "mode": "后台运行中", "server": "未连接", "login": "检测中", "login_color": "gray",
    "quota": "未获取", "client_mode": "unknown", "current_account": "",
    "push_time": "从未", "push_ok": None, "keepalive_time": "从未",
    "proxy": "检测中", "proxy_parsed": None,
    "thinking": True, "parallel": True,
    "accounts_total": 0, "accounts_logged": 0, "accounts_alive": 0,
    "api_key": "", "base_url": "", "server_connected_at": 0,
}


def init_status_from_env():
    env = cb.read_env()
    STATUS["thinking"] = env.get("ENABLE_THINKING", "true").lower() == "true"
    STATUS["parallel"] = env.get("PARALLEL_TOOL_CALLS", "true").lower() == "true"
    proxy = env.get("GEMINI_PROXY", "").strip()
    STATUS["proxy"] = "已启用(服务端+浏览器窗口)" if proxy else "未启用"
    STATUS["proxy_parsed"] = cb.parse_proxy_url(proxy)
    STATUS["api_key"] = env.get("API_KEY", "")
    STATUS["base_url"] = f"http://127.0.0.1:{SERVER_PORT}/v1"


def is_already_running() -> bool:
    try:
        kernel32 = ctypes.windll.kernel32
        mutex = kernel32.CreateMutexW(None, False, "Local\\GeminiCoreApp4444")
        if kernel32.GetLastError() == 183:
            return True
        return False
    except Exception:
        return False


# ─────────────────────────────── 主界面服务 4445 ───────────────────────────────
ADMIN_TOKEN: dict = {"token": ""}
ACTION_QUEUE: queue.Queue = queue.Queue()


class DashHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            html = (BASE_DIR / "ui_dash.html").read_text(encoding="utf-8")
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/api/status":
            with LOG_LOCK:
                self._send(200, json.dumps(STATUS, ensure_ascii=False))
        elif path == "/api/logs":
            with LOG_LOCK:
                self._send(200, json.dumps({"logs": LOG_BUFFER[-200:]}))
        elif path == "/api/dashboard":
            self._send(200, json.dumps(build_dashboard(), ensure_ascii=False))
        elif path == "/api/accounts":
            self._send(200, json.dumps(proxy_admin("GET", "/admin/api/accounts"), ensure_ascii=False))
        elif path == "/api/alerts":
            self._send(200, json.dumps(proxy_admin("GET", "/admin/api/alerts"), ensure_ascii=False))
        elif path == "/api/gems":
            self._send(200, json.dumps(proxy_admin("GET", "/admin/api/gems"), ensure_ascii=False))
        elif path == "/api/proxy-health":
            self._send(200, json.dumps(proxy_admin("GET", "/admin/api/proxy-health"), ensure_ascii=False))
        elif path == "/api/alerts/config":
            self._send(200, json.dumps(proxy_admin("GET", "/admin/api/alerts/config"), ensure_ascii=False))
        else:
            self._send(404, '{"detail":"Not Found"}')

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            payload = {}
        path = self.path.split("?")[0]
        if path == "/api/action":
            action = payload.get("action", "")
            if action in ("push_cookie", "keepalive", "refresh_accounts"):
                ACTION_QUEUE.put(action)
                self._send(200, '{"ok":true}')
            else:
                self._send(400, '{"detail":"unknown action"}')
        elif path == "/api/switch-account":
            pid = payload.get("pid", "")
            r = proxy_admin("POST", f"/admin/api/accounts/{pid}/switch", {})
            self._send(200, json.dumps(r, ensure_ascii=False))
        elif path == "/api/isolate-account":
            pid = payload.get("pid", "")
            isolated = bool(payload.get("isolated"))
            r = proxy_admin("POST", f"/admin/api/accounts/{pid}/isolate", {"isolated": isolated})
            self._send(200, json.dumps(r, ensure_ascii=False))
        elif path == "/api/alerts/seen":
            r = proxy_admin("POST", "/admin/api/alerts/seen", {})
            self._send(200, json.dumps(r, ensure_ascii=False))
        elif path == "/api/accounts/refresh":
            r = proxy_admin("POST", "/admin/api/accounts/refresh", {})
            self._send(200, json.dumps(r, ensure_ascii=False))
        elif path == "/api/proxy":
            self._send(200, json.dumps(save_proxy(payload), ensure_ascii=False))
        elif path == "/api/proxy-health/test":
            r = proxy_admin("POST", "/admin/api/proxy-health/test", payload)
            self._send(200, json.dumps(r, ensure_ascii=False))
        elif path == "/api/alerts/config":
            r = proxy_admin("POST", "/admin/api/alerts/config", payload)
            self._send(200, json.dumps(r, ensure_ascii=False))
        elif path == "/api/alerts/test":
            r = proxy_admin("POST", "/admin/api/alerts/test", payload)
            self._send(200, json.dumps(r, ensure_ascii=False))
        elif path == "/api/feature":
            feature = payload.get("feature")
            enabled = bool(payload.get("enabled"))
            if feature in ("thinking", "temporary", "autoDelete", "parallelTools"):
                r = proxy_admin("POST", "/admin/api/config", {"feature": feature, "enabled": enabled})
                if feature == "thinking":
                    STATUS["thinking"] = enabled
                elif feature == "parallelTools":
                    STATUS["parallel"] = enabled
                self._send(200, json.dumps(r, ensure_ascii=False))
            else:
                self._send(400, '{"detail":"unknown feature"}')
        elif path == "/api/gems":
            # 创建/激活 Gem
            action = payload.get("action")
            if action == "create":
                self._send(200, json.dumps(proxy_admin("POST", "/admin/api/gems", payload), ensure_ascii=False))
            elif action == "activate":
                self._send(200, json.dumps(proxy_admin("POST", f"/admin/api/gems/{payload.get('gem_id')}/activate", {}), ensure_ascii=False))
            elif action == "deactivate":
                self._send(200, json.dumps(proxy_admin("POST", "/admin/api/gems/deactivate", {}), ensure_ascii=False))
            else:
                self._send(400, '{"detail":"unknown gem action"}')
        else:
            self._send(404, '{"detail":"Not Found"}')


def start_dash_server():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", UI_PORT), DashHandler)
        app_log(f"主界面服务: http://127.0.0.1:{UI_PORT}")
        server.serve_forever()
    except Exception as e:
        app_log(f"主界面服务启动失败: {e}")


# ─────────────────────────────── 服务端/管理服务线程 ───────────────────────────────
def _http_alive(port: int, path: str) -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def start_api_server():
    try:
        if _http_alive(SERVER_PORT, "/v1/models"):
            app_log(f"复用已有服务端 http://127.0.0.1:{SERVER_PORT}")
            return
        import uvicorn, logging
        root = logging.getLogger()
        # 关键:basicConfig 因已有 handler 不会设 level,需显式设 INFO,否则 INFO 被过滤
        root.setLevel(logging.INFO)
        root.addHandler(UILogHandler())
        import main as server_main
        config = uvicorn.Config(server_main.app, host="127.0.0.1", port=SERVER_PORT,
                                log_level="info", access_log=False)
        uvicorn.Server(config).run()
    except Exception:
        import traceback
        err = traceback.format_exc()
        app_log(f"服务端线程异常: {err[-500:]}")


def start_fp_server():
    try:
        if _http_alive(FP_PORT, "/api/browser/status"):
            app_log(f"复用已有指纹管理服务 http://127.0.0.1:{FP_PORT}")
            return
        import uvicorn
        root = logging.getLogger()
        root.addHandler(UILogHandler())
        from fingerprint.server import app as fp_app
        config = uvicorn.Config(fp_app, host="127.0.0.1", port=FP_PORT, log_level="warning")
        uvicorn.Server(config).run()
    except Exception:
        import traceback
        err = traceback.format_exc()
        app_log(f"指纹管理服务异常: {err[-500:]}")


# ─────────────────────────────── 管理 API 代理(cb.ServerClient 封装) ───────────────────────────────
def _ensure_admin_token() -> bool:
    if ADMIN_TOKEN["token"]:
        return True
    server = cb.ServerClient(cb.load_api_key())
    if server.login():
        ADMIN_TOKEN["token"] = server.token or ""
        return True
    return False


def proxy_admin(method: str, path: str, payload=None) -> dict:
    if not _ensure_admin_token():
        return {"ok": False, "error": "管理面板登录失败"}
    try:
        return cb._http_json(method, f"http://127.0.0.1:{SERVER_PORT}{path}",
                             payload, {"X-Admin-Token": ADMIN_TOKEN["token"]}, timeout=150)
    except Exception as e:
        ADMIN_TOKEN["token"] = ""
        return {"ok": False, "error": str(e)[:200]}


def save_proxy(payload: dict) -> dict:
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
    STATUS["proxy"] = "已启用(服务端+浏览器窗口)" if proxy else "未启用"
    STATUS["proxy_parsed"] = cb.parse_proxy_url(proxy)
    ok = proxy_admin("POST", "/admin/api/proxy", {"proxy": proxy})
    app_log(f"代理已更新: {proxy or '(关闭)'} -> 同步{'成功' if ok.get('success') else '失败'}")
    return {"ok": bool(ok.get("success")), "message": f"代理已更新并同步{'成功' if ok.get('success') else '失败'}"}


# ─────────────────────────────── 账号统计(看板) ───────────────────────────────
FP_STORAGE = BASE_DIR / "fingerprint" / "storage"


def scan_accounts() -> dict:
    """统计指纹账号:总数(x/27 上限)/已登录/cookie 活性。读 Cookies DB 与 profile。"""
    total = logged = alive = 0
    detail = []
    try:
        from fingerprint import profiles as fp_profiles
        for p in fp_profiles.list_all():
            pid = p["id"]
            total += 1
            try:
                g = read_account_cookie(pid)
                if g and g.get("logged"):
                    logged += 1
                    if g.get("alive"):
                        alive += 1
                    detail.append({"pid": pid, "name": p.get("basic", {}).get("name") or pid, **g})
                else:
                    detail.append({"pid": pid, "name": p.get("basic", {}).get("name") or pid, "logged": False})
            except Exception:
                detail.append({"pid": pid, "name": p.get("basic", {}).get("name") or pid, "logged": False})
    except Exception as e:
        app_log(f"账号统计失败: {e}")
    STATUS["accounts_total"] = total
    STATUS["accounts_logged"] = logged
    STATUS["accounts_alive"] = alive
    return {"total": total, "max": 27, "logged": logged, "alive": alive, "detail": detail}


def read_account_cookie(pid: str) -> dict | None:
    """读账号 cookie 活性(明文文件优先,其次 Cookies DB)。返回 {logged, alive, psid6}"""
    try:
        from fingerprint.account_cookies import read_gemini_cookies
        g = read_gemini_cookies(pid)
        if not g:
            return {"logged": False, "alive": False}
        return {"logged": True, "alive": True, "psid6": g["psid"][:6], "read_at": g.get("read_at")}
    except Exception:
        return {"logged": False, "alive": False}


# ─────────────────────────────── 看板聚合 ───────────────────────────────
def build_dashboard() -> dict:
    server_status = proxy_admin("GET", "/admin/api/status")
    accounts = scan_accounts()
    quota = proxy_admin("GET", "/admin/api/quota")
    with LOG_LOCK:
        bridge_logs = list(LOG_BUFFER[-100:])
    # 服务端日志 = admin web ui 的「最近日志」(请求日志:GET /v1/models 200)
    server_logs = (proxy_admin("GET", "/admin/api/logs").get("logs") or [])[-60:]
    if quota.get("available") and quota.get("active_account"):
        STATUS["current_account"] = quota["active_account"]
    if quota.get("client_mode"):
        STATUS["client_mode"] = quota["client_mode"]
    # 服务核心连接时长
    dur = ""
    if STATUS.get("server_connected_at"):
        secs = int(time.time() - STATUS["server_connected_at"])
        dur = f"{secs // 3600}h{secs % 3600 // 60}m" if secs >= 3600 else f"{secs // 60}m"
    # Cookie 推送记录 + 服务就绪(有推送 = 服务端可用 = 已连接)
    try:
        import main as _m
        _push = getattr(_m, "COOKIE_PUSH_TIME", "") or ""
        STATUS["push_time"] = _push or "从未"
        STATUS["server_ready"] = bool(_push)
    except Exception:
        pass
    return {
        "server": server_status if server_status.get("running") else {"running": False},
        "bridge": dict(STATUS),
        "server_duration": dur,
        "accounts": accounts,
        "logs": {"bridge": bridge_logs, "server": server_logs},
        "now": time.time(),
    }


# ─────────────────────────────── 守护循环 ───────────────────────────────
def guard_loop():
    now0 = time.time()
    last_scan = last_keepalive = last_quota = last_push = now0
    last_success = now0

    while True:
        try:
            # 动作队列
            try:
                act = ACTION_QUEUE.get_nowait()
            except queue.Empty:
                act = None
            if act == "push_cookie":
                last_push = time.time()
                r = proxy_admin("POST", "/admin/api/reinit", {})
                STATUS["push_time"] = cb.now_str()
                STATUS["push_ok"] = bool(r.get("success"))
                app_log(f"手动推送/刷新 cookie: {'成功' if r.get('success') else '失败'}")
            elif act == "keepalive":
                last_keepalive = time.time()
                r = proxy_admin("POST", "/admin/api/reinit", {})
                STATUS["keepalive_time"] = cb.now_str()
                app_log(f"手动保活(触发令牌旋转): {'成功' if r.get('success') else '失败'}")
            elif act == "refresh_accounts":
                scan_accounts()

            now = time.time()
            # 服务状态
            if not STATUS["server"].startswith("已连接"):
                if _ensure_admin_token():
                    STATUS["server"] = f"已连接(http://127.0.0.1:{SERVER_PORT})"
                    if not STATUS["server_connected_at"]:
                        STATUS["server_connected_at"] = time.time()
                elif now - last_success > 10:
                    STATUS["server"] = "未连接"
            # 账号统计 2~4h
            if now - last_scan >= 120 * 60 * random.uniform(0.9, 1.3):
                last_scan = now
                scan_accounts()
            # 自动保活:6~12h 自适应(仅当服务端无成功动作时触发令牌旋转)
            period = 8 * 60 * 60 * random.uniform(0.9, 1.3)
            if now - last_keepalive >= period and (now - last_success > 6 * 60 * 60 or last_success == 0):
                last_keepalive = now
                r = proxy_admin("POST", "/admin/api/reinit", {})
                STATUS["keepalive_time"] = cb.now_str()
                if r.get("success"):
                    last_success = now
                app_log(f"自动保活完成(触发令牌旋转): {'成功' if r.get('success') else '失败(将自动重试)'}")
            # 额度 30min
            if now - last_quota >= 30 * 60 * random.uniform(0.9, 1.3):
                last_quota = now
                q = proxy_admin("GET", "/admin/api/quota")
                if q.get("available"):
                    usage = q.get("usage_info", {}) or {}
                    daily = usage.get("daily") or usage.get("current_5h") or usage
                    parts = []
                    if daily.get("remaining_credits") is not None:
                        parts.append(f"剩余 {daily['remaining_credits']}")
                    if daily.get("usage_percentage") is not None:
                        parts.append(f"已用 {daily['usage_percentage']}%")
                    if daily.get("reset_at"):
                        parts.append(f"重置 {daily['reset_at'][:16]}")
                    STATUS["quota"] = " / ".join(parts)
                    if q.get("active_account"):
                        STATUS["current_account"] = q["active_account"]
                    if q.get("client_mode"):
                        STATUS["client_mode"] = q["client_mode"]
            time.sleep(3)
        except Exception as e:
            app_log(f"守护循环异常: {e}")
            time.sleep(3)


def main():
    init_status_from_env()
    if is_already_running():
        return
    threading.Thread(target=start_api_server, daemon=True).start()
    threading.Thread(target=start_fp_server, daemon=True).start()
    threading.Thread(target=start_dash_server, daemon=True).start()
    time.sleep(2)
    guard_loop()


if __name__ == "__main__":
    main()
