# -*- coding: utf-8 -*-
"""
fingerprint/window.py —— 指纹浏览器独立窗口进程 + 进程管理

每个账号窗口 = 一个独立 pythonw 子进程:
    pythonw -m fingerprint.window <账号ID>

- 独立 user_data_dir → 账号天然隔离(可独立登录 Google 账号)
- 独立指纹注入 / 独立代理 / 可选内核版本
- 窗口被用户关闭 → 进程自动退出并更新状态文件
- 管理服务(server.py)通过本模块的 launch/close/is_running 调度窗口
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_ROOT = Path(__file__).resolve().parent / "storage"
PYTHONW = str(PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe")

# pythonw 无控制台:替换 stdout/stderr 防止日志库崩溃
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _state_path(pid: str) -> Path:
    return STORAGE_ROOT / pid / "window_state.json"


def _sig_dir() -> Path:
    d = STORAGE_ROOT / "_signals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _launch_sig(pid: str) -> Path:  # 管理页「打开」→ 重开窗口
    return _sig_dir() / f"{pid}.launch"


def _exit_sig(pid: str) -> Path:  # 管理页「关闭」→ 彻底关闭进程
    return _sig_dir() / f"{pid}.exit"


def _test_close_sig(pid: str) -> Path:  # 优雅关闭测试(模拟用户点 X):触发 context.on("close")
    return _sig_dir() / f"{pid}.testclose"


def _rm(p: Path):
    try:
        p.unlink(missing_ok=True)
    except Exception:
        pass


def _write_state(pid: str, status: str):
    state = {"pid": pid, "os_pid": os.getpid(),
             "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "status": status}
    try:
        _state_path(pid).parent.mkdir(parents=True, exist_ok=True)
        _state_path(pid).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _kill_userdata_chrome(pid: str):
    """杀掉占用该账号 user-data-dir 的残留 Chrome(解决启动/重开被锁)"""
    try:
        import psutil
        marker = ("fingerprint" + os.sep + "storage" + os.sep + pid).lower()
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                cl = " ".join(p.info["cmdline"] or [])
                if p.info["name"] and p.info["name"].lower() == "chrome.exe" and marker in cl.lower():
                    p.kill()
            except Exception:
                pass
    except Exception:
        pass


def _process_alive(pid: int) -> bool:
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


def window_state(pid: str) -> dict:
    """读取窗口状态。返回 {pid, os_pid, started_at, status} 或 {status: 'closed'}"""
    f = _state_path(pid)
    st = {}
    if f.exists():
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            st = {}
    if st.get("os_pid") and not _process_alive(st["os_pid"]):
        st["status"] = "closed"
        st.pop("os_pid", None)
    elif not st.get("os_pid"):
        st["status"] = "closed"
    return st


def is_running(pid: str) -> bool:
    st = window_state(pid)
    return st.get("status") == "running" and bool(st.get("os_pid"))


def launch_window(pid: str) -> dict:
    """窗口/进程拆分:进程在 → 窗口已开返回 already_open,窗口已关(idle 后台)发重开信号;
    进程不在 → 启动独立进程。返回 {ok, status, os_pid?, error?}"""
    st = window_state(pid)
    os_pid = st.get("os_pid")
    if os_pid and _process_alive(os_pid):
        if st.get("status") == "running":
            return {"ok": True, "status": "already_open", "pid": pid, "os_pid": os_pid}
        # 窗口已关闭但内核进程保活(idle)→ 发重开信号,并等待窗口真正重开(而非假成功)
        try:
            _launch_sig(pid).touch()
        except Exception:
            pass
        deadline = time.time() + 20
        while time.time() < deadline:
            st2 = window_state(pid)
            if st2.get("status") == "running":
                return {"ok": True, "status": "opened", "pid": pid, "os_pid": os_pid}
            if not _process_alive(os_pid):
                break
            time.sleep(0.5)
        return {"ok": False, "status": "reopen_failed", "pid": pid,
                "error": "内核进程在,但窗口重开失败(查看 window.log,常见是残留进程占用 user-data-dir)"}
    # 清理残留状态文件
    try:
        _state_path(pid).unlink(missing_ok=True)
    except Exception:
        pass
    try:
        proc = subprocess.Popen(
            [PYTHONW, "-m", "fingerprint.window", pid],
            cwd=str(PROJECT_ROOT),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # 等待状态文件出现(最多 20s:内核+指纹注入+页面加载)
        deadline = time.time() + 20
        while time.time() < deadline:
            st2 = window_state(pid)
            if st2.get("status") == "running":
                return {"ok": True, "status": "opened", "pid": pid, "os_pid": st2.get("os_pid")}
            if not _process_alive(proc.pid):
                break
            time.sleep(0.5)
        # 启动失败或超时
        if _process_alive(proc.pid):
            return {"ok": True, "status": "starting", "pid": pid, "os_pid": proc.pid}
        return {"ok": False, "status": "failed", "pid": pid, "error": "窗口进程启动失败(检查 storage/{pid} 配置与日志)"}
    except Exception as e:
        return {"ok": False, "status": "failed", "pid": pid, "error": str(e)}


def close_window(pid: str) -> dict:
    """关闭账号窗口:先优雅关浏览器(有 HTTP 状态口),再杀进程。"""
    st = window_state(pid)
    os_pid = st.get("os_pid")
    if os_pid and _process_alive(os_pid):
        # 尝试优雅关闭:通过浏览器窗口的关闭按钮做不到,直接结束进程树
        try:
            subprocess.run(["taskkill", "/PID", str(os_pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        except Exception:
            try:
                ctypes.windll.kernel32.TerminateProcess(
                    ctypes.windll.kernel32.OpenProcess(1, False, int(os_pid)), 1)
            except Exception:
                pass
    try:
        _state_path(pid).unlink(missing_ok=True)
    except Exception:
        pass
    return {"status": "closed", "pid": pid}


def _resolve_proxy_with_failover(pid: str, meta: dict) -> dict:
    """代理故障转移:绑定代理(bound:)按绑定顺序测试,选第一个可用;全不可用回退直连。"""
    proxy = dict(meta.get("proxy") or {})
    mode = (proxy.get("mode") or "direct")
    if not mode.startswith("bound:"):
        return proxy
    from . import proxy_manager as pm
    from . import netprobe
    bound = pm.proxies_for_window(pid)
    if not bound:
        proxy["mode"] = "direct"
        return proxy
    cur_id = mode[6:]
    ordered = ([x for x in bound if x["id"] == cur_id] + [x for x in bound if x["id"] != cur_id])
    for px in ordered:
        try:
            info = netprobe.query_ip(proxy_type=px["scheme"], proxy_host=px["host"],
                                     proxy_port=int(px["port"]), proxy_user=px.get("username", ""),
                                     proxy_pass=px.get("password", ""))
            if info and info.get("ip"):
                pm.set_status(px["id"], f"可用 · {info['ip']}")
                return {"mode": "bound:" + px["id"], "host": px["host"], "port": px["port"],
                        "username": px.get("username", ""), "password": px.get("password", "")}
            pm.set_status(px["id"], "不可用")
        except Exception:
            pm.set_status(px["id"], "不可用")
    return {"mode": "direct"}


# ─────────────────────────── 窗口进程主体 ───────────────────────────
async def _write_2fa(context, pid: str):
    """把账号的 2FA 密钥写入内置扩展(每窗口独立 storage,天然隔离)"""
    try:
        from . import account_manager as am
        from . import profiles as fp_profiles
        from urllib.parse import quote
        accs = am.list_accounts(pid, with_password=True)
        if not accs:
            return
        meta = fp_profiles.get(pid) or {}
        note = (meta.get("basic") or {}).get("note") or ""
        # 通过 CDP 找扩展 ID
        pages = context.pages
        if not pages:
            return
        ext_id = ""
        try:
            cdp = await pages[0].context.new_cdp_session(pages[0])
            t = await cdp.send("Target.getTargets")
            for ti in t.get("targetInfos", []):
                u = ti.get("url", "")
                if u.startswith("chrome-extension://"):
                    ext_id = u.split("/")[2]
                    break
            try:
                await cdp.detach()
            except Exception:
                pass
        except Exception:
            pass
        if not ext_id:
            print(f"[window:{pid}] 未找到 2FA 扩展")
            return
        for a in accs:
            secret = a.get("totp_secret") or ""
            if not secret:
                continue
            url = (f"chrome-extension://{ext_id}/inject.html"
                   f"?platform=google&account={quote(a.get('username',''))}"
                   f"&secret={quote(secret)}&note={quote(note)}")
            pg = await context.new_page()
            try:
                await pg.goto(url, timeout=15000, wait_until="domcontentloaded")
                await _asyncio.sleep(0.8)
                txt = ""
                try:
                    txt = await pg.evaluate("document.getElementById('st')?.textContent || ''")
                except Exception:
                    pass
                print(f"[window:{pid}] 2FA 写入 {a.get('username','')}: {txt}")
            finally:
                await pg.close()
    except BaseException as e:
        print(f"[window:{pid}] 2FA 写入失败: {e}")


async def _run_window(pid: str):
    # 窗口进程日志写文件(pythonw 无 stderr,便于诊断关窗后台行为)
    try:
        (STORAGE_ROOT / pid).mkdir(parents=True, exist_ok=True)
        _logf = (STORAGE_ROOT / pid / "window.log").open("a", encoding="utf-8")
        _logf.write("\n" + "=" * 36 + f" {time.strftime('%Y-%m-%d %H:%M:%S')} window:{pid} start " + "=" * 36 + "\n")
        _logf.flush()
        sys.stdout = _logf
        sys.stderr = _logf
        sys.stdout.reconfigure(line_buffering=True)  # 行缓冲,print 即时落盘
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    from .browser import FingerprintBrowser
    from . import profiles

    meta = profiles.get(pid)
    if not meta:
        print(f"[window:{pid}] profile 不存在")
        return 1
    proxy = _resolve_proxy_with_failover(pid, meta)
    browser = FingerprintBrowser(pid, proxy=proxy, os_type="windows")
    try:
        await browser.launch(headless=False)
    except BaseException as e:
        print(f"[window:{pid}] 启动失败(清理残留后重试): {type(e).__name__}: {e}")
        _kill_userdata_chrome(pid)
        await _asyncio.sleep(1.2)
        try:
            browser = FingerprintBrowser(pid, proxy=proxy, os_type="windows")
            await browser.launch(headless=False)
        except BaseException as e2:
            print(f"[window:{pid}] 启动失败(重试): {type(e2).__name__}: {e2}")
            return 1

    # 写入账号 2FA 密钥到内置扩展(后台任务,不阻塞窗口打开)
    try:
        _asyncio.get_event_loop().create_task(_write_2fa(browser._context, pid))
    except Exception:
        pass

    # 写运行状态
    state = {
        "pid": pid,
        "os_pid": os.getpid(),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "running",
    }
    try:
        _state_path(pid).parent.mkdir(parents=True, exist_ok=True)
        _state_path(pid).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    print(f"[window:{pid}] 已启动 os_pid={os.getpid()}")

    # 窗口/进程拆分:用户关闭窗口 → 只关窗口,内核进程保活(后台 idle);
    # 管理页「打开」→ 重开窗口;「关闭」→ 彻底关闭进程。期间每 30s 导出 Google 登录 cookie。
    closed = _asyncio.Event()
    from .account_cookies import save_gemini_cookies
    def _on_close():
        closed.set()
        _write_state(pid, "idle")
        print(f"[window:{pid}] 窗口已关闭(进程保活)")
    try:
        browser._context.on("close", _on_close)
    except Exception:
        pass
    last_psid = last_psidts = ""
    _write_state(pid, "running")
    while True:
        try:
            # 优雅关闭测试信号(模拟用户点 X 关闭窗口):优雅 close context → 触发 on("close")
            if _test_close_sig(pid).exists():
                _rm(_test_close_sig(pid))
                try:
                    await browser._context.close()
                except BaseException:
                    pass
            # 彻底关闭信号(管理页「关闭」按钮)
            if _exit_sig(pid).exists():
                _rm(_exit_sig(pid))
                print(f"[window:{pid}] 收到关闭信号,退出进程")
                break
            # 重开窗口信号(管理页「打开」按钮)
            if _launch_sig(pid).exists():
                _rm(_launch_sig(pid))
                if closed.is_set():
                    try:
                        await browser.close()
                    except BaseException:
                        pass
                    # 重建实例重新 launch(复用已关闭对象会因旧 driver context 失败);等旧 Chromium 释放 user-data-dir
                    await _asyncio.sleep(1.2)
                    try:
                        browser = FingerprintBrowser(pid, proxy=proxy, os_type="windows")
                        await browser.launch(headless=False)
                        browser._context.on("close", _on_close)
                        closed.clear()
                        _write_state(pid, "running")
                        print(f"[window:{pid}] 窗口已重新打开")
                    except BaseException as e:
                        print(f"[window:{pid}] 重开窗口失败: {e}")
            # 窗口还开着 → 导出 cookie
            if not closed.is_set():
                try:
                    cks = {c["name"]: c["value"] for c in await browser._context.cookies()}
                    psid = cks.get("__Secure-1PSID", "")
                    psidts = cks.get("__Secure-1PSIDTS", "")
                    if psid and psidts and (psid != last_psid or psidts != last_psidts):
                        save_gemini_cookies(pid, psid, psidts)
                        last_psid, last_psidts = psid, psidts
                        print(f"[window:{pid}] Google 登录 cookie 已导出")
                except BaseException:
                    pass
        except BaseException as e:
            # 捕获一切(含 CancelledError/transport 关闭错误),关窗后进程绝不停
            try:
                print(f"[window:{pid}] 循环异常(进程保活): {type(e).__name__}: {e}")
            except Exception:
                pass
        await _asyncio.sleep(1)

    # 清理状态
    _rm(_state_path(pid))
    _rm(_launch_sig(pid))
    _rm(_exit_sig(pid))
    try:
        await browser.close()
    except BaseException:
        pass
    print(f"[window:{pid}] 已退出")
    return 0


import asyncio as _asyncio


def main():
    import os as _os
    _os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PROJECT_ROOT / "browsers")
    pid = sys.argv[1] if len(sys.argv) > 1 else ""
    if not pid:
        print("usage: pythonw -m fingerprint.window <账号ID>")
        return 1
    try:
        rc = _asyncio.run(_run_window(pid))
    except BaseException:
        import traceback as _tb
        try:
            _tb.print_exc()
        except Exception:
            pass
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
