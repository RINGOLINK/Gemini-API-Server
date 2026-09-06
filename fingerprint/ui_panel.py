"""UI 面板浏览器 —— CDP 原生下载，类真实浏览器体验"""
import asyncio
from pathlib import Path
from patchright.async_api import async_playwright

PANEL_ID = "_ui_panel"
STORAGE_ROOT = Path(__file__).parent / "storage"


async def launch_ui_panel(port: int = 5555, app_mode: bool = True, profile_suffix: str = ""):
    pid = PANEL_ID if not profile_suffix else f"_ui_{profile_suffix}"
    profile = str(STORAGE_ROOT / pid / "userdata")

    pw = await async_playwright().start()
    args = ["--window-size=1280,800", "--explicitly-allowed-ports=4444,4445,4446"]
    if app_mode:
        args.append(f"--app=http://127.0.0.1:{port}")
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=profile, headless=False, args=args,
        no_viewport=True)
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    # CDP 原生下载：Chromium 自己命名+落盘，真实文件名
    cdp = await ctx.new_cdp_session(page)
    await cdp.send("Browser.setDownloadBehavior", {
        "behavior": "allow",
        "downloadPath": str(Path.home() / "Downloads"),
        "eventsEnabled": True,
    })

    if not app_mode:
        await page.goto(f"http://127.0.0.1:{port}")

    while True:
        await asyncio.sleep(0.5)
        try:
            if not ctx.pages or ctx.pages[0].is_closed():
                break
        except Exception:
            break

    try: await ctx.close()
    except: pass
    try: await pw.stop()
    except: pass


async def close_ui_panel(pw, ctx):
    try: await ctx.close()
    except: pass
    try: await pw.stop()
    except: pass
