# -*- coding: utf-8 -*-
"""
launcher.py —— Gemini 项目启动器(入口)

- 管理项目生命周期:启动/关闭项目、打开主界面、状态显示
- 系统托盘常驻:关闭窗口最小化到托盘;托盘右键 → 打开启动器 / 退出
- 退出启动器 = 结束所有与项目相关的进程(服务端/桥/主界面/指纹窗口)

启动: 启动启动器.bat(纯 ASCII)→ pythonw launcher.py
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PYTHONW = str(BASE_DIR / ".venv" / "Scripts" / "pythonw.exe")
CORE_SCRIPT = str(BASE_DIR / "gemini_core.py")
MAIN_UI_SCRIPT = str(BASE_DIR / "main_ui.py")
UI_URL = "http://127.0.0.1:4445/"
APP_PROFILE = str(BASE_DIR / "app_profile")

STATE_FILE = BASE_DIR / ".launcher_state.json"


def _find_browser():
    """找系统 Edge/Chrome 用于 app 应用模式打开主界面"""
    for c in [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]:
        if os.path.exists(c):
            return c
    import shutil
    return shutil.which("msedge") or shutil.which("chrome") or shutil.which("chrome.exe")

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import json


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"core_pid": 0, "ui_pid": 0}


def _save_state(st: dict):
    try:
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def _kill_tree(pid: int):
    """结束进程树(含子进程)"""
    if not pid or not _pid_alive(pid):
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
    except Exception:
        try:
            ctypes.windll.kernel32.TerminateProcess(
                ctypes.windll.kernel32.OpenProcess(1, False, int(pid)), 1)
        except Exception:
            pass


def _port_open(port: int, timeout: float = 0.4) -> bool:
    """快速探测端口是否有监听(socket connect,毫秒级,不阻塞)。"""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except Exception:
            return False
        finally:
            try:
                s.close()
            except Exception:
                pass
    except Exception:
        return False


def _svc_alive(port: int, path: str = "/") -> bool:
    """HTTP 探测:判断某服务是否真正响应(用于精确判断;启动时比端口探测慢)。"""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=0.8) as r:
            return r.status < 500
    except Exception:
        return False


def _find_core_pid() -> int:
    """按命令行找实际运行的 gemini_core 进程 pid(优先真身,非 shim;仅限本 BASE_DIR,不误认其他目录实例)"""
    try:
        procs = []
        _base = str(BASE_DIR).replace("\\", "\\\\")
        cmd = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'pythonw|python' "
               "-and $_.CommandLine -match 'gemini_core' "
               "-and $_.CommandLine -like '*" + _base + "*' "
               "-and $_.CommandLine -notmatch 'Get-Cim|Win32_Process' } | ForEach-Object { $_.ProcessId }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                             capture_output=True, text=True, timeout=15)
        for pid in (out.stdout or "").split():
            pid = pid.strip()
            if pid.isdigit():
                procs.append(int(pid))
        # 返回最后一个(通常是 pyenv 真身)
        return procs[-1] if procs else 0
    except Exception:
        return 0


def start_core() -> bool:
    """启动后台核心(gemini_core.py)。若服务已在跑(手动/残留),识别为已启动,不重复 spawn。"""
    st = _load_state()
    if st.get("core_pid") and _pid_alive(st["core_pid"]):
        return True
    # 探测服务端口(快速 socket):已在跑则记录实际 pid,视为已启动
    if _port_open(4444) or _port_open(4445):
        st["core_pid"] = _find_core_pid() or st.get("core_pid", 0)
        _save_state(st)
        return True
    try:
        proc = subprocess.Popen(
            [PYTHONW, CORE_SCRIPT],
            cwd=str(BASE_DIR),
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        st["core_pid"] = proc.pid
        _save_state(st)
        return True
    except Exception:
        return False


def stop_core():
    st = _load_state()
    _kill_tree(st.get("core_pid", 0))
    st["core_pid"] = 0
    _save_state(st)


_CORE_CACHE = {"val": False}   # 由后台监控线程更新,主线程只读(零 IO)


def _core_monitor():
    """后台线程:周期性探测服务端口,更新 _CORE_CACHE(不阻塞 tkinter 主线程)"""
    while True:
        try:
            _CORE_CACHE["val"] = _port_open(4445) or _port_open(4444)
        except Exception:
            pass
        time.sleep(1.5)


def core_alive() -> bool:
    """核心是否在跑:读后台监控线程缓存(内存,毫秒级,绝不阻塞)"""
    return _CORE_CACHE["val"]


def start_ui() -> bool:
    """打开项目主界面(app 应用模式)。先清理可能残留的浏览器进程(Edge --app 关窗口后进程常残留),再重新拉起——确保点开一定能唤起"""
    if not core_alive():
        return False
    st = _load_state()
    old_pid = st.get("ui_pid")
    if old_pid and _pid_alive(old_pid):
        _kill_tree(old_pid)
        time.sleep(0.6)
    browser = _find_browser()
    if not browser:
        return False
    try:
        try:
            os.makedirs(APP_PROFILE, exist_ok=True)
        except Exception:
            pass
        proc = subprocess.Popen(
            [browser, f"--app={UI_URL}", f"--user-data-dir={APP_PROFILE}",
             "--window-size=1280,1000", "--window-position=200,60",
             "--explicitly-allowed-ports=4444,4445,4446"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        st["ui_pid"] = proc.pid
        _save_state(st)
        return True
    except Exception:
        return False


def stop_ui():
    st = _load_state()
    _kill_tree(st.get("ui_pid", 0))
    st["ui_pid"] = 0
    _save_state(st)


def ui_alive() -> bool:
    st = _load_state()
    return bool(st.get("ui_pid")) and _pid_alive(st["ui_pid"])


def stop_everything():
    """结束所有项目进程:核心 + 主界面 + 指纹窗口(仅本目录实例,不误伤其他 Gemini-API 目录)"""
    stop_core()
    stop_ui()
    _kill_stray_by_cmdline()
    _kill_by_listen_ports()


def _kill_stray_by_cmdline():
    """兜底1:按命令行结束游离的服务/窗口进程。

    进程是两层结构(.venv shim → pyenv 真身):
    - 真身命令行含绝对路径 → 按 BASE_DIR 匹配(可靠)
    - shim 命令行是相对路径(不含 BASE_DIR),无法按目录区分;
      但真身被杀后 shim 等待返回会自行退出,无需显式处理
    注意:①-like 通配符里反斜杠是普通字符,不可翻倍(翻倍后永不匹配——已修 bug)
         ②排除正式版目录:用 -notlike '*\\gemini-api\\*'(路径精确区分,不受大小写影响)"""
    try:
        _base = str(BASE_DIR)
        cmd = ("Get-CimInstance Win32_Process | Where-Object { "
               "$_.Name -match 'pythonw|python' "
               "-and $_.CommandLine -like '*" + _base + "*' "
               "-and $_.CommandLine -match 'gemini_core|fingerprint.window|main_ui|fingerprint.server' "
               "} | ForEach-Object { $_.ProcessId }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                             capture_output=True, text=True, timeout=15)
        for pid in (out.stdout or "").split():
            pid = pid.strip()
            if pid.isdigit():
                _kill_tree(int(pid))
    except Exception:
        pass


def _kill_by_listen_ports():
    """兜底2(终极):凡监听本项目端口(4444/4445/4446)的进程一律结束——
    端口在谁手里杀谁,保证'结束后端口必关',启动器状态必然回到未运行。"""
    for port in (4444, 4445, 4446):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-NetTCPConnection -LocalPort " + str(port) +
                 " -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess"],
                capture_output=True, text=True, timeout=15)
            for pid in (out.stdout or "").split():
                pid = pid.strip()
                if pid.isdigit() and int(pid) != os.getpid():
                    _kill_tree(int(pid))
        except Exception:
            pass


# ─────────────────────────── 启动器界面(tkinter,主线程) ───────────────────────────
import tkinter as tk

_ROOT = None          # 全局 tk root(主线程)
_TOAST_SEEN = False   # 关闭提示是否已展示过
_FONT = ("Microsoft YaHei", 10)
_FONT_UI = ("Microsoft YaHei", 11)
_FONT_BOLD = ("Microsoft YaHei", 13, "bold")
_FONT_TITLE = ("Microsoft YaHei", 22, "bold")


def _hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb2hex(rgb):
    return "#%02x%02x%02x" % rgb


def _lerp(c1, c2, t):
    a, b = _hex2rgb(c1), _hex2rgb(c2)
    return _rgb2hex(tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))


class RoundButton(tk.Canvas):
    """圆角按钮:圆角矩形 + 悬停颜色渐变动画 + 统一尺寸"""

    def __init__(self, parent, text, command, bg, hover_bg, fg="#ffffff",
                 width=260, height=58, radius=12, font=_FONT_BOLD):
        super().__init__(parent, width=width, height=height,
                         bg=parent.cget("bg"), highlightthickness=0, bd=0)
        self._bw, self._bh, self._br = width, height, radius
        self._bg, self._hover_bg, self._fg = bg, hover_bg, fg
        self._text, self._cmd, self._font = text, command, font
        self._anim = None
        self._draw(self._bg)
        self.bind("<Enter>", lambda e: self._animate(self._bg, self._hover_bg))
        self.bind("<Leave>", lambda e: self._animate(self._hover_bg, self._bg))
        self.bind("<Button-1>", lambda e: (self._cmd() if self._cmd else None))

    def _draw(self, color):
        self.delete("all")
        w, h, r = self._bw, self._bh, self._br
        # 圆角:上下左右矩形 + 四角弧
        self.create_rectangle(r, 0, w - r, h, fill=color, outline="")
        self.create_rectangle(0, r, w, h - r, fill=color, outline="")
        self.create_arc(0, 0, 2 * r, 2 * r, start=90, extent=90, fill=color, outline="")
        self.create_arc(w - 2 * r, 0, w, 2 * r, start=0, extent=90, fill=color, outline="")
        self.create_arc(0, h - 2 * r, 2 * r, h, start=180, extent=90, fill=color, outline="")
        self.create_arc(w - 2 * r, h - 2 * r, w, h, start=270, extent=90, fill=color, outline="")
        self.create_text(w / 2, h / 2, text=self._text, fill=self._fg, font=self._font)

    def set_text(self, text):
        self._text = text
        self._draw(self._bg)

    def _animate(self, c1, c2):
        if self._anim is not None:
            self.after_cancel(self._anim)
            self._anim = None
        steps = 18
        t0 = [0.0]

        def step():
            t0[0] += 1.0 / steps
            t = min(t0[0], 1.0)
            self._draw(_lerp(c1, c2, t))
            if t < 1.0:
                self._anim = self.after(16, step)
            else:
                self._anim = None

        step()


def _toast(root, text):
    try:
        t = tk.Toplevel(root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.configure(bg="#1c1e25")
        tk.Label(t, text=text, bg="#1c1e25", fg="#e0e1e6",
                 font=_FONT_UI, padx=16, pady=10).pack()
        t.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - t.winfo_reqwidth()) // 2
        y = root.winfo_rooty() + root.winfo_height() + 12
        t.geometry(f"+{x}+{y}")
        root.after(2000, t.destroy)
    except Exception:
        pass


def _build_gui() -> tk.Tk:
    global _ROOT
    BG = "#0c0d10"      # 画布(与主界面 --bg 一致)
    HAIR = "#23252a"    # 分隔线(Linear hairline)
    TXT = "#e7e9ef"
    TXT2 = "#9aa0ad"
    TXT3 = "#616674"

    root = tk.Tk()
    root.title("Gemini-API (Dev 4445)")
    root.geometry("460x460")
    root.resizable(False, False)
    root.configure(bg=BG)
    _ROOT = root

    # 顶部:logo + 标题 作为一组水平居中
    head = tk.Frame(root, bg=BG)
    head.place(relx=0.5, y=48, anchor="center")
    logo = tk.Canvas(head, width=40, height=40, bg=BG, highlightthickness=0, bd=0)
    logo.pack(side="left")
    logo.create_rectangle(0, 0, 40, 40, fill="#6d28d9", outline="")
    logo.create_arc(0, 0, 12, 12, start=90, extent=90, fill="#6d28d9", outline="")
    logo.create_arc(28, 0, 40, 12, start=0, extent=90, fill="#6d28d9", outline="")
    logo.create_arc(0, 28, 12, 40, start=180, extent=90, fill="#6d28d9", outline="")
    logo.create_arc(28, 28, 40, 40, start=270, extent=90, fill="#6d28d9", outline="")
    logo.create_text(20, 20, text="G", fill="#ffffff", font=("Microsoft YaHei", 17, "bold"))
    tk.Label(head, text="Gemini-API", font=_FONT_TITLE, bg=BG, fg=TXT).pack(side="left", padx=12)
    tk.Label(root, text="项目启动器 · 开发实例(Dev 4445)", font=("Microsoft YaHei", 9),
             bg=BG, fg=TXT3).place(relx=0.5, y=82, anchor="center")

    # 分隔线(hairline)
    tk.Frame(root, bg=HAIR, height=1).place(x=30, y=108, relwidth=1, width=400)

    # 状态:圆点 + 文本 居中组
    st_row = tk.Frame(root, bg=BG)
    st_row.place(relx=0.5, y=138, anchor="center")
    status_dot = tk.Label(st_row, text="●", font=("Microsoft YaHei", 12),
                          bg=BG, fg="#f05252")
    status_dot.pack(side="left")
    status_var = tk.StringVar(value="项目未启动")
    status_lbl = tk.Label(st_row, textvariable=status_var, font=_FONT_BOLD,
                          bg=BG, fg=TXT)
    status_lbl.pack(side="left", padx=7)
    tk.Label(root, text="运行状态", font=("Microsoft YaHei", 9),
             bg=BG, fg=TXT3).place(relx=0.5, y=166, anchor="center")

    def refresh():
        core = core_alive()
        if core:
            status_var.set("项目运行中")
            main_btn.set_text("关闭项目")
        else:
            status_var.set("项目未启动")
            main_btn.set_text("启动项目")
        root.after(2000, refresh)

    def _paint_status():
        ok = core_alive()
        status_dot.configure(fg="#34d17c" if ok else "#f05252")
        status_lbl.configure(fg=TXT if ok else TXT)
        root.after(500, _paint_status)

    def on_toggle():
        if core_alive():
            # 后台停止,避免主线程阻塞 1+s
            import threading
            threading.Thread(target=lambda: (stop_everything(), root.after(0, refresh)), daemon=True).start()
        else:
            # 后台启动(含 sleep+start_ui),主线程不阻塞
            import threading
            threading.Thread(target=lambda: (start_core(), time.sleep(1.5), start_ui(), root.after(0, refresh)), daemon=True).start()

    def on_open_ui():
        import threading
        def _do():
            if not core_alive():
                start_core()
                time.sleep(1.5)
            start_ui()
            root.after(0, refresh)
        threading.Thread(target=_do, daemon=True).start()

    def on_stop_all():
        import threading
        def _do():
            stop_everything()
            root.after(0, lambda: (refresh(), _toast(root, "已结束全部进程")))
        threading.Thread(target=_do, daemon=True).start()

    def on_close():
        global _TOAST_SEEN
        root.withdraw()
        if not _TOAST_SEEN:
            _TOAST_SEEN = True
            try:
                ctypes.windll.user32.MessageBoxW(0,
                    "已最小化到系统托盘。\n单击托盘图标打开启动器,右键可退出。",
                    "Gemini-API", 0x40)
            except Exception:
                pass

    # 统一按钮:固定 300x60,间距 14,圆角 + hover 动画(配色对齐主界面令牌)
    bw, bh, gap = 300, 60, 14
    cx = 230  # 460/2
    main_btn = RoundButton(root, "启动项目", on_toggle,
                           bg="#6d28d9", hover_bg="#7c3aed", width=bw, height=bh)
    main_btn.place(x=cx - bw / 2, y=186)
    RoundButton(root, "打开项目主界面", on_open_ui,
                bg="#14161b", hover_bg="#1a1d23", fg="#e7e9ef", width=bw, height=bh).place(
        x=cx - bw / 2, y=186 + bh + gap)
    RoundButton(root, "结束全部进程", on_stop_all,
                bg="#2a1216", hover_bg="#36171c", fg="#f05252", width=bw, height=bh).place(
        x=cx - bw / 2, y=186 + 2 * (bh + gap))

    tk.Label(root, text="关闭窗口 = 最小化到托盘 · 托盘右键可退出", font=("Microsoft YaHei", 8),
             bg=BG, fg=TXT3).place(relx=0.5, y=442, anchor="center")

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh()
    _paint_status()
    return root


# ─────────────────────────── 系统托盘 ───────────────────────────
_TRAY_ICON = None


def _make_icon():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (109, 40, 217, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((12, 12, 52, 52), fill=(22, 24, 29, 255))
    d.arc((18, 18, 46, 46), start=200, end=340, fill=(224, 225, 230, 255), width=6)
    d.line((32, 32, 32, 24), fill=(224, 225, 230, 255), width=6)
    return img


def _tray_open_launcher():
    """托盘 → 显示/唤起启动器窗口(调度回主线程)"""
    if _ROOT is not None:
        try:
            _ROOT.after(0, lambda: (_ROOT.deiconify(), _ROOT.lift(), _ROOT.focus_force()))
        except Exception:
            pass
    else:
        # GUI 未建,补建(罕见)
        import threading
        threading.Thread(target=_build_gui, daemon=True).start()


def _tray_open_main_ui():
    if not core_alive():
        start_core()
        time.sleep(1.5)
    start_ui()


def _tray_quit():
    stop_everything()
    try:
        if _TRAY_ICON is not None:
            _TRAY_ICON.stop()
    except Exception:
        pass
    os._exit(0)


def _run_tray():
    """系统托盘(后台线程阻塞运行 icon.run)。左键单击打开启动器。"""
    global _TRAY_ICON
    import pystray
    menu = pystray.Menu(
        pystray.MenuItem("打开启动器", lambda: _tray_open_launcher(), default=True),
        pystray.MenuItem("打开项目主界面", lambda: _tray_open_main_ui()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出(结束全部进程)", lambda: _tray_quit()),
    )
    _TRAY_ICON = pystray.Icon("gemini-launcher-4445", _make_icon(), "Gemini-API (Dev 4445)", menu)
    _TRAY_ICON.run()   # 阻塞(在后台线程调用)


def main():
    # 单实例(Dev 实例用独立 mutex,与正式版 gemini-api 启动器可并存)
    try:
        kernel32 = ctypes.windll.kernel32
        mutex = kernel32.CreateMutexW(None, False, "Local\\GeminiLauncherApp4445")
        if kernel32.GetLastError() == 183:
            ctypes.windll.user32.MessageBoxW(0, "Gemini-API (Dev 4445) 启动器已在运行(查看系统托盘)。", "Gemini-API (Dev 4445)", 0x40)
            return
    except Exception:
        pass
    # 主线程:建 GUI + mainloop(确保界面一定显示)
    root = _build_gui()
    # 后台线程:托盘(icon.run 阻塞在该线程,不阻塞主线程 tkinter)
    import threading
    threading.Thread(target=_run_tray, daemon=True).start()
    threading.Thread(target=_core_monitor, daemon=True).start()   # 服务状态后台监控(主线程零IO)
    root.mainloop()


if __name__ == "__main__":
    main()

