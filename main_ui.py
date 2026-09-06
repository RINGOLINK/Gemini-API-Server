# -*- coding: utf-8 -*-
"""
main_ui.py —— 项目主界面(web app 窗口)

- 独立浏览器内核(patchright,profile: app_profile/),与指纹浏览器内核隔离
- 打开 http://127.0.0.1:4445(看板/设置/指纹管理)
- 窗口被用户关闭 → 进程退出(不再自动唤起;由启动器重新打开)

启动: pythonw main_ui.py(由启动器"打开项目主界面"调用)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
UI_URL = f"http://127.0.0.1:{os.environ.get('GEMINI_UI_PORT', '4445')}/"
APP_PROFILE = BASE_DIR / "app_profile"


def _find_browser():
    """找系统 Edge/Chrome(用于 app 应用模式窗口,无地址栏)"""
    for c in [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files\Microsoft\Edge\Application\chrome.exe"]:
        if os.path.exists(c):
            return c
    import shutil
    return shutil.which("msedge") or shutil.which("chrome") or shutil.which("chrome.exe")


def main():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BASE_DIR / "browsers")
    # 等待主界面服务就绪(最多 40s)
    import urllib.request
    for _ in range(40):
        try:
            with urllib.request.urlopen(UI_URL, timeout=3) as r:
                if r.status == 200:
                    break
        except Exception:
            pass
        time.sleep(1)
    # 用系统浏览器的 app 应用模式打开主界面(无地址栏,独立窗口)
    import subprocess
    browser = _find_browser()
    if not browser:
        try:
            import webbrowser
            webbrowser.open(UI_URL)
        except Exception:
            pass
        return
    try:
        APP_PROFILE.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        proc = subprocess.Popen([browser, f"--app={UI_URL}", f"--user-data-dir={APP_PROFILE}",
                                 "--window-size=1280,1000", "--window-position=200,60",
                                 "--explicitly-allowed-ports=4444,4445,4446"])
        # 窗口被用户关闭 → 进程退出
        proc.wait()
    except Exception as e:
        try:
            (BASE_DIR / "_main_ui_crash.log").write_text(str(e), encoding="utf-8")
        except Exception:
            pass


if __name__ == "__main__":
    main()
