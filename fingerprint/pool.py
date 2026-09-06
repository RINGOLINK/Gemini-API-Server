"""浏览器内核池管理 —— 多版本 Chromium 登记与切换"""
from pathlib import Path

POOL_DIR = Path(__file__).resolve().parent.parent / "browser"
CURRENT_FILE = POOL_DIR / "_current.txt"


def current_version() -> str:
    return CURRENT_FILE.read_text().strip() if CURRENT_FILE.exists() else ""


def list_versions() -> list[str]:
    return [d.name for d in POOL_DIR.iterdir() if d.is_dir() and d.name.startswith("chromium-")]


def register(version: str):
    d = POOL_DIR / f"chromium-{version}"
    d.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(version)
    return d


def get_current_dir() -> Path:
    v = current_version()
    return POOL_DIR / f"chromium-{v}"
