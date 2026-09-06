"""Chromium 内核版本管理器

支持下载/管理多个 Chromium 版本，供指纹浏览器按需调用。
内核存储在 browser/ 目录下，每个版本一个子目录。
"""
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

KERNELS_DIR = Path(__file__).resolve().parent.parent / "browser"
KERNELS_DIR.mkdir(exist_ok=True)

AVAILABLE_KERNELS = {
    "152": {
        "version": "152.0.7977.54",
        "url": "https://storage.googleapis.com/chrome-for-testing-public/152.0.7977.54/win64/chrome-win64.zip",
        "ua_chrome": "152.0.0.0",
    },
    "150": {
        "version": "150.0.7871.124",
        "url": "https://storage.googleapis.com/chrome-for-testing-public/150.0.7871.124/win64/chrome-win64.zip",
        "ua_chrome": "150.0.0.0",
    },
    "149": {
        "version": "149.0.7827.55",
        "url": "https://storage.googleapis.com/chrome-for-testing-public/149.0.7827.55/win64/chrome-win64.zip",
        "ua_chrome": "149.0.0.0",
    },
}


def _kernel_dir(ver_key):
    return KERNELS_DIR / ("chromium-" + ver_key)


def list_installed():
    installed = {}
    for key, info in AVAILABLE_KERNELS.items():
        kdir = _kernel_dir(key)
        exe = kdir / "chrome-win64" / "chrome.exe"
        installed[key] = {
            "version": info["version"],
            "ua_chrome": info["ua_chrome"],
            "path": str(exe) if exe.exists() else None,
            "installed": exe.exists(),
        }
    return installed


def get_executable(ver_key):
    info = AVAILABLE_KERNELS.get(ver_key)
    if not info:
        return None
    exe = _kernel_dir(ver_key) / "chrome-win64" / "chrome.exe"
    return str(exe) if exe.exists() else None


def download_kernel(ver_key, progress_cb=None):
    info = AVAILABLE_KERNELS.get(ver_key)
    if not info:
        return {"ok": False, "error": "未知版本: " + ver_key}

    kdir = _kernel_dir(ver_key)
    exe = kdir / "chrome-win64" / "chrome.exe"
    if exe.exists():
        return {"ok": True, "already": True, "path": str(exe)}

    kdir.mkdir(parents=True, exist_ok=True)
    zip_path = kdir / "chrome.zip"

    try:
        import urllib.request
        if progress_cb:
            progress_cb(0, "开始下载...")
        urllib.request.urlretrieve(info["url"], zip_path)
        if progress_cb:
            progress_cb(50, "解压中...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(kdir)
        zip_path.unlink()
        if progress_cb:
            progress_cb(100, "完成")
        if exe.exists():
            return {"ok": True, "path": str(exe)}
        else:
            return {"ok": False, "error": "解压后未找到 chrome.exe"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        if zip_path.exists():
            zip_path.unlink()


def remove_kernel(ver_key):
    kdir = _kernel_dir(ver_key)
    if kdir.exists():
        shutil.rmtree(kdir)
        return True
    return False


def get_ua_for_kernel(ver_key, os_family="windows"):
    info = AVAILABLE_KERNELS.get(ver_key)
    if not info:
        return ""
    chrome_ver = info["ua_chrome"]
    if os_family == "windows":
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/" + chrome_ver + " Safari/537.36"
    elif os_family == "macos":
        return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/" + chrome_ver + " Safari/537.36"
    elif os_family == "linux":
        return "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/" + chrome_ver + " Safari/537.36"
    return ""
