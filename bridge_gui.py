# -*- coding: utf-8 -*-
"""
bridge_gui.py — Gemini 浏览器桥 · 登录前台 GUI

内容:
- 运行状态区(模式/内核/登录/服务端/今日额度/凭证/推送/保活/代理)
- 按钮区(打开登录/立即推送/立即保活/服务端面板/退出)
- 出口代理配置区(协议/主机/端口/账号/密码 + 保存)
- 功能开关区(思考模式/并行工具调用,实时生效+持久化)
- Agent 接入信息区(Base URL / API Key 显示复制 / 模型 / 引导)
- 日志区
"""
from __future__ import annotations

import tkinter as tk
from tkinter import scrolledtext

import cookie_bridge as cb

COLORS = {"green": "#1e8e3e", "red": "#d93025", "orange": "#f9ab00", "gray": "#5f6368"}


class BridgeGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Gemini 浏览器桥 · 登录器")
        root.geometry("780x860")
        root.resizable(True, True)

        self.bridge = cb.CookieBridge(
            status={"kernel": "启动中", "login": "检测中", "login_color": "gray",
                    "server": "未连接", "psid": "-", "psidts": "-",
                    "push_time": "从未", "push_ok": None, "keepalive_time": "从未"},
            log=self._log,
        )
        self._build_ui()
        self._poll_status()
        self.bridge.start()

    def _build_ui(self):
        frame = tk.LabelFrame(self.root, text="运行状态", padx=10, pady=8)
        frame.pack(fill="x", padx=10, pady=6)

        self.vars = {
            "mode": tk.StringVar(value="启动中"),
            "kernel": tk.StringVar(value="启动中"),
            "login": tk.StringVar(value="检测中"),
            "server": tk.StringVar(value="未连接"),
            "quota": tk.StringVar(value="未获取"),
            "psid": tk.StringVar(value="-"),
            "psidts": tk.StringVar(value="-"),
            "push": tk.StringVar(value="从未"),
            "keepalive": tk.StringVar(value="从未"),
            "proxy": tk.StringVar(value="检测中"),
        }
        rows = [
            ("运行模式", "mode"),
            ("浏览器内核", "kernel"),
            ("登录状态", "login"),
            ("服务端连接", "server"),
            ("今日额度", "quota"),
            ("1PSID(长寿凭证)", "psid"),
            ("1PSIDTS(短效token)", "psidts"),
            ("最近推送", "push"),
            ("最近保活", "keepalive"),
            ("出口代理", "proxy"),
        ]
        for i, (label, key) in enumerate(rows):
            tk.Label(frame, text=label, width=16, anchor="w").grid(row=i, column=0, sticky="w", pady=1)
            tk.Label(frame, textvariable=self.vars[key], anchor="w", fg="#202124").grid(
                row=i, column=1, sticky="w", pady=1)
        self.login_label = tk.Label(frame, text="", width=2)
        self.login_label.grid(row=2, column=2, sticky="w", padx=(8, 0))
        self.quota_label = tk.Label(frame, text="", width=2)
        self.quota_label.grid(row=4, column=2, sticky="w", padx=(8, 0))

        btns = tk.Frame(self.root)
        btns.pack(fill="x", padx=10, pady=6)
        tk.Button(btns, text="打开登录页", command=lambda: self.bridge.cmd_queue.put(("open_login", None)),
                  width=14).pack(side="left", padx=3)
        tk.Button(btns, text="立即推送 Cookie", command=lambda: self.bridge.cmd_queue.put(("push_now", None)),
                  width=16).pack(side="left", padx=3)
        tk.Button(btns, text="立即保活", command=lambda: self.bridge.cmd_queue.put(("keepalive", None)),
                  width=12).pack(side="left", padx=3)
        tk.Button(btns, text="服务端管理面板", command=self._open_admin,
                  width=14).pack(side="left", padx=3)
        tk.Button(btns, text="退出", command=self._on_close, width=8).pack(side="right", padx=3)

        proxy_frame = tk.LabelFrame(self.root, text="出口代理配置(住宅 IP)", padx=10, pady=6)
        proxy_frame.pack(fill="x", padx=10, pady=4)
        self.proxy_enabled = tk.BooleanVar(value=False)
        tk.Checkbutton(proxy_frame, text="启用出口代理", variable=self.proxy_enabled).grid(row=0, column=0, sticky="w")
        tk.Label(proxy_frame, text="当前状态:").grid(row=0, column=1, sticky="e", padx=(20, 2))
        self.proxy_status_var = tk.StringVar(value="未启用")
        tk.Label(proxy_frame, textvariable=self.proxy_status_var, width=32, anchor="w").grid(row=0, column=2, sticky="w")
        tk.Label(proxy_frame, text="协议").grid(row=1, column=0, sticky="w")
        self.proxy_scheme = tk.StringVar(value="socks5h")
        tk.OptionMenu(proxy_frame, self.proxy_scheme, "socks5h", "socks5", "http").grid(row=1, column=0, sticky="w")
        tk.Label(proxy_frame, text="主机").grid(row=1, column=1, sticky="w")
        self.proxy_host = tk.Entry(proxy_frame, width=18)
        self.proxy_host.grid(row=1, column=2, sticky="w")
        tk.Label(proxy_frame, text="端口").grid(row=1, column=3, sticky="w")
        self.proxy_port = tk.Entry(proxy_frame, width=7)
        self.proxy_port.grid(row=1, column=4, sticky="w")
        tk.Label(proxy_frame, text="用户名").grid(row=2, column=0, sticky="w")
        self.proxy_user = tk.Entry(proxy_frame, width=18)
        self.proxy_user.grid(row=2, column=2, sticky="w")
        tk.Label(proxy_frame, text="密码").grid(row=2, column=3, sticky="w")
        self.proxy_pass = tk.Entry(proxy_frame, width=14, show="*")
        self.proxy_pass.grid(row=2, column=4, sticky="w")
        tk.Button(proxy_frame, text="保存并同步服务端", command=self._save_proxy).grid(row=2, column=5, sticky="w", padx=(12, 0))
        self._load_proxy_fields()

        toggle_frame = tk.LabelFrame(self.root, text="功能开关(实时生效)", padx=10, pady=4)
        toggle_frame.pack(fill="x", padx=10, pady=4)
        env_toggle = cb.read_env()
        self.thinking_var = tk.BooleanVar(value=env_toggle.get("ENABLE_THINKING", "true").lower() == "true")
        tk.Checkbutton(
            toggle_frame, text="思考模式(推理过程)", variable=self.thinking_var,
            command=lambda: self.bridge.cmd_queue.put(("set_feature", "thinking", self.thinking_var.get())),
        ).pack(side="left", padx=6)
        self.parallel_var = tk.BooleanVar(value=env_toggle.get("PARALLEL_TOOL_CALLS", "true").lower() == "true")
        tk.Checkbutton(
            toggle_frame, text="并行工具调用", variable=self.parallel_var,
            command=lambda: self.bridge.cmd_queue.put(("set_feature", "parallelTools", self.parallel_var.get())),
        ).pack(side="left", padx=6)

        info_frame = tk.LabelFrame(self.root, text="Agent 接入信息", padx=10, pady=6)
        info_frame.pack(fill="x", padx=10, pady=4)
        env_info = cb.read_env()
        api_key = env_info.get("API_KEY", "")
        self._api_key_real = api_key
        self._api_key_visible = False
        tk.Label(info_frame, text="Base URL:").grid(row=0, column=0, sticky="w")
        self.base_url_var = tk.StringVar(value=f"{cb.SERVER_URL}/v1")
        tk.Entry(info_frame, textvariable=self.base_url_var, width=40, state="readonly").grid(row=0, column=1, sticky="w")
        tk.Button(info_frame, text="复制", command=lambda: self._copy_text(f"{cb.SERVER_URL}/v1")).grid(row=0, column=2, sticky="w", padx=(6, 0))
        tk.Label(info_frame, text="API Key:").grid(row=1, column=0, sticky="w")
        self.api_key_var = tk.StringVar(value="•" * 14)
        tk.Entry(info_frame, textvariable=self.api_key_var, width=40, state="readonly").grid(row=1, column=1, sticky="w")
        tk.Button(info_frame, text="显示/隐藏", command=self._toggle_api_key).grid(row=1, column=2, sticky="w", padx=(6, 0))
        tk.Button(info_frame, text="复制", command=lambda: self._copy_text(api_key)).grid(row=1, column=3, sticky="w", padx=(4, 0))
        tk.Label(info_frame, text="模型:").grid(row=2, column=0, sticky="w")
        tk.Label(info_frame, text="gemini-pro / gemini-flash / gemini-flash-lite", anchor="w").grid(row=2, column=1, columnspan=3, sticky="w")
        tk.Label(info_frame, text="在 OpenAI 兼容 Agent(Reasonix/DSH/Cherry Studio 等)填入上方 Base URL + API Key + 模型即可",
                 fg="#5f6368").grid(row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))

        log_frame = tk.LabelFrame(self.root, text="日志", padx=6, pady=4)
        log_frame.pack(fill="both", expand=True, padx=10, pady=6)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=14, state="disabled",
                                                  font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

    def _set_entry(self, entry, value):
        entry.delete(0, "end")
        entry.insert(0, value)

    def _load_proxy_fields(self):
        env = cb.read_env()
        url = env.get("GEMINI_PROXY", "").strip()
        parsed = cb.parse_proxy_url(url)
        if parsed:
            self.proxy_enabled.set(True)
            scheme = parsed["scheme"] if parsed["scheme"] in ("socks5h", "socks5", "http") else "socks5h"
            self.proxy_scheme.set(scheme)
            self._set_entry(self.proxy_host, parsed["host"])
            self._set_entry(self.proxy_port, str(parsed["port"]))
            self._set_entry(self.proxy_user, parsed.get("username", ""))
            self._set_entry(self.proxy_pass, parsed.get("password", ""))
            self.proxy_status_var.set("已配置(等待应用)")
        else:
            self.proxy_enabled.set(False)
            self.proxy_status_var.set("未启用")

    def _compose_proxy(self) -> str:
        if not self.proxy_enabled.get():
            return ""
        host = self.proxy_host.get().strip()
        port = self.proxy_port.get().strip()
        if not host or not port:
            return ""
        scheme = self.proxy_scheme.get()
        user = self.proxy_user.get().strip()
        pw = self.proxy_pass.get().strip()
        auth = f"{user}:{pw}@" if user else ""
        return f"{scheme}://{auth}{host}:{port}"

    def _save_proxy(self):
        url = self._compose_proxy()
        self.proxy_status_var.set("已提交,等待应用...")
        self.bridge.cmd_queue.put(("set_proxy", url))
        self._log(f"提交代理配置: {url or '(关闭)'}")

    def _toggle_api_key(self):
        self._api_key_visible = not self._api_key_visible
        self.api_key_var.set(self._api_key_real if self._api_key_visible else "•" * 14)

    def _copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._log("已复制到剪贴板")

    def _open_admin(self):
        import webbrowser
        webbrowser.open(f"{cb.SERVER_URL}/admin")

    def _log(self, msg: str):
        def _append():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{cb.now_str()}] {msg}\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(0, _append)

    def _poll_status(self):
        s = self.bridge.status
        self.vars["mode"].set(s.get("mode", "-"))
        self.vars["kernel"].set(s.get("kernel", "-"))
        self.vars["login"].set(s.get("login", "-"))
        self.vars["server"].set(s.get("server", "-"))
        self.vars["psid"].set(s.get("psid", "-"))
        self.vars["psidts"].set(s.get("psidts", "-"))
        self.vars["push"].set(f"{s.get('push_time', '从未')}{' ✔' if s.get('push_ok') else ''}"
                              f"{' ✘' if s.get('push_ok') is False else ''}")
        self.vars["keepalive"].set(s.get("keepalive_time", "从未"))
        self.vars["proxy"].set(s.get("proxy", "-"))
        if s.get("proxy"):
            self.proxy_status_var.set(s["proxy"])
        self.vars["quota"].set(s.get("quota", "未获取"))
        pct = s.get("quota_pct")
        rem = s.get("quota_remaining")
        if pct is not None:
            color = COLORS["red"] if pct >= 80 else (COLORS["orange"] if pct >= 50 else COLORS["green"])
        elif rem is not None and rem < 200:
            color = COLORS["red"]
        else:
            color = "#9e9e9e"
        self.quota_label.configure(bg=color)
        self.login_label.configure(bg=COLORS.get(s.get("login_color", "gray"), "#5f6368"))
        self.root.after(1000, self._poll_status)

    def _on_close(self):
        self.bridge.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    BridgeGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
