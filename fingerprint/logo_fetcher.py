"""平台 LOGO 获取 —— 抓 favicon 并缓存

策略（按优先级）：
1. 访问平台首页，解析 <link rel="icon"/"shortcut icon"/"apple-touch-icon"> 取 href
2. 退化到 /favicon.ico
3. 退化到 Google favicon 服务（https://www.google.com/s2/favicons?domain=...&sz=128）
缓存到 storage/{pid}/logos/，返回文件名。
"""
from __future__ import annotations
import re, asyncio
from urllib.parse import urljoin, urlparse

import httpx

from . import account_manager as am

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _norm(url_or_host: str) -> tuple[str, str]:
    """返回 (scheme://host, host)。"""
    s = (url_or_host or "").strip()
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    p = urlparse(s)
    host = p.netloc or p.path.split("/")[0]
    return f"{p.scheme or 'https'}://{host}", host


def _ext_from(content_type: str, url: str) -> str:
    ct = (content_type or "").lower()
    if "svg" in ct or url.endswith(".svg"):
        return ".svg"
    if "png" in ct or url.endswith(".png"):
        return ".png"
    if "jpeg" in ct or "jpg" in ct or url.endswith((".jpg", ".jpeg")):
        return ".jpg"
    if "ico" in ct or url.endswith(".ico"):
        return ".ico"
    if "webp" in ct or url.endswith(".webp"):
        return ".webp"
    return ".png"


async def fetch_logo(pid: str, url_or_host: str) -> dict:
    """抓取并缓存 LOGO，返回 {ok, logo(文件名), host} 或 {ok:False, error}。"""
    base, host = _norm(url_or_host)
    if not host:
        return {"ok": False, "error": "无效的平台地址"}

    candidates: list[str] = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                 headers={"User-Agent": _UA}, verify=False) as cli:
        # 1. 解析首页 <link rel=...icon...>
        try:
            r = await cli.get(base)
            if r.status_code < 400:
                html = r.text
                # 匹配 rel 含 icon 的 link，取 href
                for m in re.finditer(r'<link[^>]+>', html, re.I):
                    tag = m.group(0)
                    if re.search(r'rel=["\'][^"\']*icon', tag, re.I):
                        hm = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
                        if hm:
                            candidates.append(urljoin(base, hm.group(1)))
                # apple-touch-icon 通常更清晰，放前面
                candidates.sort(key=lambda u: 0 if "apple" in u.lower() else 1)
        except Exception:
            pass

        # 2. /favicon.ico
        candidates.append(f"{base}/favicon.ico")
        # 3. Google favicon 服务兜底
        candidates.append(f"https://www.google.com/s2/favicons?domain={host}&sz=128")

        for url in candidates:
            try:
                ir = await cli.get(url)
                if ir.status_code == 200 and ir.content and len(ir.content) > 100:
                    ext = _ext_from(ir.headers.get("content-type", ""), url)
                    fname = am.save_logo(pid, ir.content, ext)
                    return {"ok": True, "logo": fname, "host": host, "source": url}
            except Exception:
                continue

    return {"ok": False, "error": "未能获取到 LOGO"}
