# -*- coding: utf-8 -*-
"""
cookie_bridge.py — Gemini 浏览器桥(登录/保活分离 + 仅服务端出口代理)

设计:
- 常态:内核以「无头模式(headless)」在后台静默运行,只做保活、采集、推送,不显示窗口
- 登录:仅在「首次运行」或「登录失效」时,弹出有头窗口让人登录;成功后自动切回无头
- 出口代理仅作用于服务端 API(由 GUI 配置),浏览器走正常路由
- 会话失效深度检测:keep-alive 加载页检查跳转登录页 + 服务端推送失败回馈触发重登
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import queue
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser_profile"
BROWSERS_DIR = BASE_DIR / "browsers"
LOG_DIR = BASE_DIR / ".bridge-logs"
SERVER_URL = os.environ.get("GEMINI_SERVER_URL", "http://127.0.0.1:4444")
GEMINI_HOME = "https://gemini.google.com"

KEEPALIVE_MIN = 25
PUSH_MIN = 5
PROBE_MIN = 60            # 轻量探活(只读cookie,零Google流量;每小时一次,带抖动)
LOGIN_WAIT_MAX = 300

HEADLESS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def now_str() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def log_to_file(msg: str):
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / "bridge.log", "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def read_env() -> dict:
    env = {}
    f = BASE_DIR / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.lstrip("\ufeff").strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def write_env(updates: dict):
    env = read_env()
    env.update(updates)
    with open(BASE_DIR / ".env", "w", encoding="utf-8") as f:
        for k, v in env.items():
            f.write(f'{k}="{v}"\n')


def load_api_key() -> str:
    return read_env().get("API_KEY", "")


def parse_proxy_url(url: str) -> dict | None:
    if not url:
        return None
    u = urllib.parse.urlparse(url)
    if not u.hostname:
        return None
    return {
        "scheme": (u.scheme or "socks5").lower(),
        "host": u.hostname,
        "port": u.port or 1080,
        "username": u.username or "",
        "password": u.password or "",
    }


# ---------------------------------------------------------------- 服务端 API
def _http_json(method, url, payload=None, headers=None, timeout=10) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {body}") from e


class ServerClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.token: str | None = None

    def _ensure_token(self):
        if not self.token:
            self.login()

    def login(self) -> bool:
        try:
            r = _http_json("POST", f"{SERVER_URL}/admin/api/login", {"api_key": self.api_key})
            self.token = r.get("token")
            return bool(self.token)
        except Exception as e:
            log_to_file(f"管理面板登录失败: {e}")
            return False

    def push_cookies(self, psid: str, psidts: str) -> bool:
        self._ensure_token()
        if not self.token:
            return False
        try:
            r = _http_json("POST", f"{SERVER_URL}/admin/api/cookies-save-reinit",
                           {"secure_1psid": psid, "secure_1psidts": psidts},
                           {"X-Admin-Token": self.token}, timeout=90)
            # 服务端返回 success:False 表示验证失败(cookie 无效)→ 返回 False,触发重登
            return bool(r.get("success", True))
        except Exception as e:
            log_to_file(f"cookie 推送失败: {e}")
            self.token = None
            return False

    def push_proxy(self, proxy_url: str) -> bool:
        self._ensure_token()
        if not self.token:
            return False
        try:
            _http_json("POST", f"{SERVER_URL}/admin/api/proxy",
                       {"proxy": proxy_url}, {"X-Admin-Token": self.token})
            return True
        except Exception as e:
            log_to_file(f"代理推送失败: {e}")
            self.token = None
            return False

    def get_quota(self) -> dict | None:
        self._ensure_token()
        if not self.token:
            return None
        try:
            return _http_json("GET", f"{SERVER_URL}/admin/api/quota",
                              headers={"X-Admin-Token": self.token})
        except Exception as e:
            log_to_file(f"额度查询失败: {e}")
            self.token = None
            return None

    def set_feature(self, feature: str, enabled: bool) -> bool:
        self._ensure_token()
        if not self.token:
            return False
        try:
            _http_json("POST", f"{SERVER_URL}/admin/api/config",
                       {"feature": feature, "enabled": enabled},
                       {"X-Admin-Token": self.token})
            return True
        except Exception as e:
            log_to_file(f"功能开关设置失败: {e}")
            self.token = None
            return False


# ---------------------------------------------------------------- 桥主体
class CookieBridge:
    def __init__(self, status: dict, log: callable):
        self.status = status
        self.log = log
        self.cmd_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._p = None
        self._context = None
        self._page = None
        self._need_login = False
        self._last_push = 0.0
        self._last_keepalive = 0.0
        self._last_probe = 0.0
        self._last_status_check = 0.0
        self._last_quota = 0.0
        self._last_psid = ""
        self._last_psidts = ""
        self._proxy_url = ""
        self._server = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="cookie-bridge", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.cmd_queue.put(("exit", None))

    # ---------------- 主循环
    def _run(self):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)
        LOG_DIR.mkdir(exist_ok=True)
        try:
            from patchright.sync_api import sync_playwright
        except ImportError as e:
            self.status["kernel"] = f"缺依赖:{e}"
            self.log(f"错误:缺少依赖 {e}。请在本项目 venv 执行: pip install patchright")
            return

        self.status["kernel"] = "内核启动中..."
        self.status["mode"] = "启动中"
        self.log("启动专用内核(patchright Chromium 151)...")

        self._proxy_url = read_env().get("GEMINI_PROXY", "").strip()
        self.status["proxy"] = "已启用(仅服务端)" if self._proxy_url else "未启用"
        if self._proxy_url:
            self.log(f"出口代理已配置(仅服务端 API 使用): {self._proxy_url}")

        try:
            with sync_playwright() as p:
                self._p = p
                self._launch(headless=True)
                server = ServerClient(load_api_key())
                self._server = server
                self.status["server"] = "连接中..."
                connected = False
                for attempt in range(3):
                    if server.login():
                        connected = True
                        break
                    self.log(f"服务端管理面板连接重试 {attempt + 1}/3 ...")
                    time.sleep(5)
                if connected:
                    self.status["server"] = f"已连接({SERVER_URL})"
                    self.log(f"服务端管理面板连接成功: {SERVER_URL}")
                    # 立即推送已有的 cookie + 代理(快速恢复服务端,无需等慢会话检测)
                    self._initial_push(server)
                else:
                    self.status["server"] = "服务端未连接(请确认已启动)"
                    self.log("警告:服务端管理面板连接失败,请确认 Gemi2Api-Server 已启动")

                # 真实检测会话(区分"登录失效"与"网络抖动")
                alive = self._real_session_alive()
                if alive is False:
                    self.status["login"] = "登录已失效,将重新登录"
                    self.status["login_color"] = "red"
                    self._need_login = True
                elif alive is True:
                    self.status["mode"] = "后台无感运行"
                    self.status["login"] = "已登录"
                    self.status["login_color"] = "green"
                else:
                    if self._logged_in():
                        self.status["mode"] = "后台无感运行"
                    else:
                        self._need_login = True

                while not self.stop_event.is_set():
                    self._drain_commands()
                    if self._need_login or not self._logged_in():
                        self._need_login = False
                        self._login_flow(server)
                        continue
                    now = time.time()
                    if now - self._last_probe >= PROBE_MIN * 60 * random.uniform(0.9, 1.3):
                        self._last_probe = now
                        self._probe()
                    if now - self._last_keepalive >= KEEPALIVE_MIN * 60 * random.uniform(0.9, 1.3):
                        self._last_keepalive = now
                        self._keepalive()
                    if now - self._last_push >= PUSH_MIN * 60 * random.uniform(0.9, 1.3):
                        self._last_push = now
                        self._collect_and_push(server)
                    if now - self._last_status_check >= 30:
                        self._last_status_check = now
                        self._refresh_server_status(server)
                    if now - self._last_quota >= 1800 * random.uniform(0.9, 1.3):
                        self._last_quota = now
                        self._fetch_quota(server)
                    time.sleep(5)
                self.log("桥已停止")
        except Exception as e:
            self.status["kernel"] = f"内核异常:{e}"
            log_to_file(f"桥异常: {e}")
            self.log(f"桥异常: {e}")

    def _initial_push(self, server: ServerClient):
        """启动时向服务端推送一次代理信息 + 一次 cookie"""
        if self._proxy_url:
            ok = server.push_proxy(self._proxy_url)
            self.log(f"启动推送代理信息: {'成功' if ok else '失败'}")
        self._collect_and_push(server, force=True)

    # ---------------- 内核启动 / 切换
    def _launch(self, headless: bool):
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        kwargs = dict(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled",
                  "--explicitly-allowed-ports=4444,4445,4446"],
        )
        if headless:
            kwargs["viewport"] = {"width": 1280, "height": 800}
            kwargs["user_agent"] = HEADLESS_UA
        else:
            kwargs["no_viewport"] = True
            kwargs["args"] = ["--start-maximized", "--disable-blink-features=AutomationControlled",
                              "--explicitly-allowed-ports=4444,4445,4446"]
        self._context = self._p.chromium.launch_persistent_context(**kwargs)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.status["kernel"] = "运行中"
        return self._context, self._page

    def _login_flow(self, server: ServerClient):
        self.status["mode"] = "等待登录(已弹窗)"
        self.status["login"] = "未登录(请在弹出的窗口中登录)"
        self.status["login_color"] = "red"
        self.log("需要登录:弹出浏览器窗口,请在窗口中登录 Google 账号...")
        try:
            self._launch(headless=False)
            self._safe_goto(self._page, GEMINI_HOME)
        except Exception as e:
            log_to_file(f"启动有头内核失败: {e}")
            self.log(f"启动登录窗口失败: {e}")
            return
        deadline = time.time() + LOGIN_WAIT_MAX
        while time.time() < deadline and not self.stop_event.is_set():
            self._drain_commands()
            if self._logged_in():
                time.sleep(2)
                self.status["mode"] = "后台无感运行"
                self.status["login"] = "已登录"
                self.status["login_color"] = "green"
                self.log("登录成功,立即推送并切回后台无感模式")
                self._initial_push(server)
                self._launch(headless=True)
                self._safe_goto(self._page, GEMINI_HOME)
                return
            time.sleep(3)
        self.log("等待登录超时,切回后台无感模式(仍未登录)")
        self._launch(headless=True)
        self.status["mode"] = "后台无感运行"

    # ---------------- 命令处理
    def _drain_commands(self):
        while True:
            try:
                cmd = self.cmd_queue.get_nowait()
            except queue.Empty:
                return
            kind = cmd[0]
            if kind == "open_login":
                self.log("手动触发登录...")
                self._need_login = True
            elif kind == "push_now":
                self.log("立即推送 cookie...")
                if self._context is not None:
                    self._collect_and_push(self._server, force=True)
            elif kind == "keepalive":
                self.log("手动保活...")
                self._keepalive()
            elif kind == "set_proxy":
                self._apply_proxy(cmd[1])
            elif kind == "set_feature":
                self._apply_feature(cmd[1], cmd[2])
            elif kind == "exit":
                self.stop_event.set()

    def _apply_proxy(self, proxy_url: str):
        """热应用代理:更新 .env + 推送给服务端(仅服务端 API 使用;浏览器不走代理)"""
        proxy_url = (proxy_url or "").strip()
        write_env({"GEMINI_PROXY": proxy_url})
        self._proxy_url = proxy_url
        self.status["proxy"] = "已启用(仅服务端)" if proxy_url else "未启用"
        self.log(f"代理配置已更新: {proxy_url or '(关闭)'}(仅服务端 API 使用)")
        if self._server:
            ok = self._server.push_proxy(proxy_url)
            self.log(f"同步代理到服务端: {'成功' if ok else '失败'}")

    def _apply_feature(self, feature: str, enabled: bool):
        """热切换服务端功能开关(思考/并行工具等)"""
        if self._server:
            ok = self._server.set_feature(feature, enabled)
            label = {"thinking": "思考模式", "parallelTools": "并行工具调用"}.get(feature, feature)
            self.log(f"开关 [{label}] -> {'开' if enabled else '关'}: {'成功' if ok else '失败'}")

    # ---------------- 工具
    def _safe_goto(self, page, url: str):
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
        except Exception as e:
            log_to_file(f"页面加载失败 {url}: {e}")

    def _read_cookies(self) -> dict:
        if self._context is None:
            return {}
        return {c["name"]: c["value"] for c in self._context.cookies()}

    def _logged_in(self) -> bool:
        return bool(self._read_cookies().get("__Secure-1PSID"))

    def _real_session_alive(self):
        """真实检测会话:加载 gemini.google.com,若跳去账号登录页则已失效。
        返回 True=有效 / False=失效 / None=加载失败(不武断判定)。"""
        if self._context is None:
            return None
        try:
            page = self._context.new_page()
            try:
                page.goto(GEMINI_HOME, timeout=20000, wait_until="domcontentloaded")
                if page.url.startswith("https://accounts.google"):
                    return False
                return True
            except Exception:
                return None
            finally:
                page.close()
        except Exception:
            return None

    def _probe(self):
        """轻量探活:只读本地 cookie(不加载页面,零 Google 流量)。"""
        try:
            psid = self._read_cookies().get("__Secure-1PSID", "")
            psidts = self._read_cookies().get("__Secure-1PSIDTS", "")
            if not psid:
                self.status["login"] = "未登录"
                self.status["login_color"] = "red"
                self._need_login = True
            else:
                self.status["login"] = "已登录"
                self.status["login_color"] = "green" if psidts else "orange"
                self.status["psid"] = f"存在({psid[:6]}…)"
                self.status["psidts"] = f"存在({psidts[:6]}…)" if psidts else "缺失!"
            self.status["probe_time"] = now_str()
        except Exception as e:
            log_to_file(f"登录态检测异常: {e}")

    def _keepalive(self):
        """后台无感保活:headless 下静默加载一次 gemini.google.com,触发浏览器自身续期"""
        if self._context is None:
            return
        try:
            page = self._context.new_page()
            try:
                page.goto(GEMINI_HOME, timeout=45000, wait_until="domcontentloaded")
                time.sleep(random.uniform(3, 6))
                if page.url.startswith("https://accounts.google"):
                    self.log("保活时发现已登出,将触发重新登录")
                    self._need_login = True
            finally:
                page.close()
            self.status["keepalive_time"] = now_str()
            self.log(f"保活完成({now_str()})")
        except Exception as e:
            log_to_file(f"保活失败: {e}")

    def _collect_and_push(self, server: ServerClient, force: bool = False):
        try:
            cookies = self._read_cookies()
            psid = cookies.get("__Secure-1PSID", "")
            psidts = cookies.get("__Secure-1PSIDTS", "")
            if not psid:
                self.status["login_color"] = "red"
                self._need_login = True
                return
            changed = (psid != self._last_psid) or (psidts != self._last_psidts)
            if changed or force:
                ok = server.push_cookies(psid, psidts)
                self.status["push_time"] = now_str()
                self.status["push_ok"] = ok
                if ok:
                    self._last_psid, self._last_psidts = psid, psidts
                    self.log(f"cookie 已推送(1PSID {psid[:6]}… / 1PSIDTS {psidts[:6]}…)")
                else:
                    self.log("cookie 推送失败(见日志详情)")
                    if self.status.get("server", "").startswith("已连接"):
                        self._need_login = True
                        self.log("检测到 cookie 已失效,将触发重新登录")
        except Exception as e:
            log_to_file(f"采集/推送异常: {e}")

    def _refresh_server_status(self, server: ServerClient):
        if self.status.get("server", "").startswith("已连接"):
            return
        if server.login():
            self.status["server"] = f"已连接({SERVER_URL})"
            self.log(f"服务端连接成功: {SERVER_URL}")

    def _fetch_quota(self, server: ServerClient):
        q = server.get_quota()
        if not q or not q.get("available"):
            return
        usage = q.get("usage_info", {}) or {}
        daily = usage.get("daily") or usage.get("current_5h") or usage
        remaining = daily.get("remaining_credits")
        if remaining is None:
            remaining = daily.get("ai_credits_remaining")
        pct = daily.get("usage_percentage")
        reset = daily.get("reset_at", "")
        self.status["quota_remaining"] = remaining
        self.status["quota_pct"] = pct
        self.status["quota_reset"] = reset
        parts = []
        if remaining is not None:
            parts.append(f"剩余 {remaining}")
        if pct is not None:
            parts.append(f"已用 {pct}%")
        if reset:
            parts.append(f"重置 {reset[:16]}")
        self.status["quota"] = " / ".join(parts) if parts else "未获取到额度"
        if pct is not None and pct >= 80:
            self.log(f"[额度告警] 已用 {pct}%!请留意剩余额度")
        elif remaining is not None and remaining < 200:
            self.log(f"[额度提醒] 剩余 credits 不足: {remaining}")


def main():
    parser = argparse.ArgumentParser(description="Gemini 浏览器桥")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    status = {"kernel": "启动中", "login": "检测中", "login_color": "gray",
              "server": "未连接", "mode": "启动中", "proxy": "检测中"}
    bridge = CookieBridge(status, lambda m: print(f"[桥] {m}"))
    if args.headless:
        bridge._login_flow = lambda server: (bridge._launch(headless=True),
                                             bridge._safe_goto(bridge._page, GEMINI_HOME),
                                             time.sleep(5))
    bridge.start()
    try:
        while not bridge.stop_event.is_set():
            time.sleep(2)
            print(f"\r[状态] 内核={status.get('kernel')} 模式={status.get('mode')} "
                  f"登录={status.get('login')} 代理={status.get('proxy')} 推送={status.get('push_time','从未')}  ",
                  end="", flush=True)
    except KeyboardInterrupt:
        bridge.stop()


if __name__ == "__main__":
    main()
