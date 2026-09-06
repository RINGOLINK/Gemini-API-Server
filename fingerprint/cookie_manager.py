"""Cookie 管理模块 —— 站点聚合模型（v3）

核心模型（与用户确认）：
- 以【站点】为单位聚合该站产生的【所有】cookie（登录凭证 + 痕迹/设备指纹）。
- 站内排序：登录锚点凭证置顶，其余 cookie 归拢在下方。
- 含登录锚点的组 = 「登录站点」；无锚点的组 = 「痕迹站点」。
- 严格凭证制判定登录锚点（仅登录后服务器下发的强凭证）。
- 为什么：平台风控校验的是整站 cookie 生态（登录凭证 + 设备指纹/追踪），
  只迁登录凭证会被判定异常；整站迁移才能保证登录态可用。

站点归一：同一公司多个域归并（douyin+douyinstatic+feelgood → 字节跳动/抖音系）。
"""
from __future__ import annotations
import time
from typing import Any

# ─────────────────────────────────────────────────────────
# 严格登录凭证库：domain 关键词 -> (平台名, 强凭证 cookie name 列表)
# ─────────────────────────────────────────────────────────
PLATFORM_CREDENTIALS: list[tuple[str, str, list[str]]] = [
    ("douyin.com",   "抖音",   ["sessionid", "sessionid_ss", "sid_tt", "uid_tt", "sid_ucp_v1", "ssid_ucp_v1"]),
    ("tiktok.com",   "TikTok", ["sessionid", "sessionid_ss", "sid_tt", "uid_tt"]),
    ("baidu.com",    "百度",   ["BDUSS", "BDUSS_BFESS", "STOKEN", "PTOKEN"]),
    ("google.com",   "Google", ["SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID"]),
    ("github.com",   "GitHub", ["user_session"]),
    ("x.com",        "X",      ["auth_token", "ct0"]),
    ("twitter.com",  "X",      ["auth_token", "ct0"]),
    ("weibo.com",    "微博",   ["SUB", "SUBP", "ALF", "SSOLoginState"]),
    ("zhihu.com",    "知乎",   ["z_c0"]),
    ("bilibili.com", "B站",    ["SESSDATA", "bili_jct", "DedeUserID"]),
    ("xiaohongshu.com", "小红书", ["web_session"]),
    ("kuaishou.com", "快手",   ["kuaishou.server.web_st", "kuaishou.server.web_ph"]),
    ("facebook.com", "Facebook", ["c_user", "xs"]),
    ("instagram.com","Instagram", ["sessionid", "ds_user_id"]),
    ("youtube.com",  "YouTube", ["SID", "HSID", "SSID", "LOGIN_INFO", "APISID", "SAPISID"]),
    ("qq.com",       "QQ",     ["skey", "pt4_token", "p_skey"]),
    ("weixin.qq.com","微信",   ["webwx_data_ticket", "webwx_auth_ticket"]),
    ("jd.com",       "京东",   ["pt_key", "thor"]),
    ("taobao.com",   "淘宝",   ["cookie2", "sgcookie", "_tb_token_", "unb"]),
    ("tmall.com",    "天猫",   ["cookie2", "sgcookie", "_tb_token_"]),
    ("alipay.com",   "支付宝", ["ALIPAYJSESSIONID", "ctoken"]),
    ("amazon.com",   "Amazon", ["session-token", "x-main", "at-main"]),
    ("paypal.com",   "PayPal", ["nsid", "x-pp-s", "login_type"]),
    ("microsoft.com","Microsoft", ["MSPAuth", "AMCSecAuth", "RPSSecAuth"]),
    ("live.com",     "Microsoft", ["MSPAuth", "AMCSecAuth", "RPSSecAuth"]),
    ("linkedin.com", "LinkedIn", ["li_at"]),
    ("netflix.com",  "Netflix", ["NetflixId", "SecureNetflixId"]),
    ("spotify.com",  "Spotify", ["sp_dc", "sp_key"]),
    ("reddit.com",   "Reddit", ["reddit_session", "token_v2"]),
    ("discord.com",  "Discord", ["token"]),
    ("telegram.org", "Telegram", ["stel_token", "stel_ssid"]),
    ("163.com",      "网易",   ["NTES_SESS", "P_INFO"]),
    ("126.com",      "网易邮箱", ["NTES_SESS", "MAIL_SESS"]),
    ("csdn.net",     "CSDN",   ["UserToken", "AU"]),
    ("juejin.cn",    "掘金",   ["sessionid", "sessionid_ss"]),
    ("segmentfault.com", "SegmentFault", ["sfsessionid"]),
    ("v2ex.com",     "V2EX",   ["A2", "PB3_SESSION"]),
    ("steamcommunity.com", "Steam", ["steamLoginSecure"]),
    ("steampowered.com",   "Steam", ["steamLoginSecure"]),
    ("douban.com",   "豆瓣",   ["bid", "dbcl2", "ck"]),
]

PLATFORM_GROUP: dict[str, str] = {
    "抖音": "douyin", "TikTok": "tiktok", "百度": "baidu", "Google": "google",
    "GitHub": "github", "X": "x", "微博": "weibo", "知乎": "zhihu", "B站": "bilibili",
    "小红书": "xiaohongshu", "快手": "kuaishou", "Facebook": "facebook",
    "Instagram": "instagram", "YouTube": "youtube", "QQ": "qq", "微信": "weixin",
    "京东": "jd", "淘宝": "taobao", "天猫": "taobao", "支付宝": "alipay",
    "Amazon": "amazon", "PayPal": "paypal", "Microsoft": "microsoft",
    "LinkedIn": "linkedin", "Netflix": "netflix", "Spotify": "spotify",
    "Reddit": "reddit", "Discord": "discord", "Telegram": "telegram",
    "网易": "netease", "网易邮箱": "netease", "CSDN": "csdn", "掘金": "juejin",
    "SegmentFault": "segmentfault", "V2EX": "v2ex", "Steam": "steam", "豆瓣": "douban",
}

# 站点归一：同公司多个域 -> (站点key, 显示名)
SITE_UNIFY: list[tuple[list[str], str, str]] = [
    (["douyin.com", "douyinstatic.com", "douyinvod.com", "bytecdn.cn", "bytedance.com", "bytedance.org", "bytetos.com", "feelgood.cn", "ixigua.com", "toutiao.com", "snssdk.com"], "douyin", "抖音"),
    (["tiktok.com", "tiktokcdn.com", "tiktokv.com", "musical.ly", "byteoversea.com"], "tiktok", "TikTok"),
    (["google.com", "googleapis.com", "gstatic.com", "googlevideo.com", "ggpht.com", "withgoogle.com", "googlesyndication.com", "doubleclick.net", "googletagmanager.com", "google-analytics.com"], "google", "Google"),
    (["youtube.com", "ytimg.com"], "youtube", "YouTube"),
    (["x.com", "twitter.com", "twimg.com", "t.co"], "x", "X"),
    (["baidu.com", "baidustatic.com", "bdstatic.com", "baidupcs.com"], "baidu", "百度"),
    (["github.com", "githubusercontent.com", "githubassets.com", "github.io"], "github", "GitHub"),
    (["facebook.com", "fbcdn.net", "meta.com", "messenger.com"], "facebook", "Facebook"),
    (["instagram.com", "cdninstagram.com"], "instagram", "Instagram"),
    (["microsoft.com", "live.com", "msn.com", "bing.com", "clarity.ms", "office.com", "outlook.com", "windows.com", "azure.com"], "microsoft", "Microsoft"),
    (["bilibili.com", "bilivideo.com", "biliapi.net", "hdslb.com", "acgvideo.com"], "bilibili", "B站"),
    (["qq.com", "tencent.com", "qpic.cn", "gtimg.cn"], "qq", "腾讯"),
    (["weixin.qq.com", "wechat.com"], "weixin", "微信"),
    (["weibo.com", "weibocdn.com", "sinaimg.cn", "sina.com.cn"], "weibo", "微博"),
    (["zhihu.com", "zhimg.com"], "zhihu", "知乎"),
    (["xiaohongshu.com", "xhscdn.com"], "xiaohongshu", "小红书"),
    (["kuaishou.com", "kuaishoucdn.com", "yximgs.com"], "kuaishou", "快手"),
    (["jd.com", "360buyimg.com", "jdpay.com"], "jd", "京东"),
    (["taobao.com", "tmall.com", "alipay.com", "alibaba.com", "alicdn.com", "aliyun.com", "tbcdn.cn"], "taobao", "阿里"),
    (["amazon.com", "amazonaws.com", "media-amazon.com", "ssl-images-amazon.com"], "amazon", "Amazon"),
    (["netflix.com", "nflxvideo.net", "nflximg.net", "nflxext.com"], "netflix", "Netflix"),
    (["spotify.com", "scdn.co", "spotilocal.com"], "spotify", "Spotify"),
    (["steampowered.com", "steamcommunity.com", "steamstatic.com", "steamcontent.com"], "steam", "Steam"),
    (["apple.com", "icloud.com", "mzstatic.com", "cdn-apple.com"], "apple", "Apple"),
    (["cloudflare.com", "cloudflareinsights.com"], "cloudflare", "Cloudflare"),
    (["paypal.com", "paypalobjects.com"], "paypal", "PayPal"),
    (["reddit.com", "redd.it", "redditstatic.com"], "reddit", "Reddit"),
    (["discord.com", "discordapp.com", "discord.gg"], "discord", "Discord"),
    (["telegram.org", "t.me", "telegram.me"], "telegram", "Telegram"),
    (["163.com", "126.com", "netease.com"], "netease", "网易"),
    (["csdn.net", "csdnimg.cn"], "csdn", "CSDN"),
    (["juejin.cn", "juejin.im"], "juejin", "掘金"),
    (["douban.com", "doubanio.com"], "douban", "豆瓣"),
    (["zhihu.com"], "zhihu", "知乎"),
]

GENERIC_LOGIN_KEYWORDS = ["sessionid", "session_id", "auth_token", "access_token", "refresh_token"]

TRACKING_HINTS = [
    "_ga", "_gid", "_gcl", "_ym", "_fbp", "_clck", "_clsk", "_octo", "_uetsid", "_uetvid",
    "utm", "referrer", "trace", "track", "analytics", "metric",
    "tuuid", "zuuid", "uuid", "visitor", "guest", "anon",
    "abtest", "ab_test", "variant", "banner", "campaign",
]


def _domain_of(cookie: dict) -> str:
    return (cookie.get("domain") or "").lower()


def match_platform(domain: str, name: str) -> str | None:
    """严格凭证制：命中强凭证返回平台名。"""
    d = domain.lstrip(".")
    n = name.lower()
    for dom_key, platform, creds in PLATFORM_CREDENTIALS:
        if dom_key in d and n in [x.lower() for x in creds]:
            return platform
    return None


def is_tracking_cookie(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in TRACKING_HINTS)


def classify_cookie(cookie: dict) -> dict[str, Any]:
    """判定：是否登录锚点 + 状态。"""
    domain = _domain_of(cookie)
    name = cookie.get("name") or ""
    expires = cookie.get("expires", -1)
    now = time.time()

    if expires is None or expires == -1 or expires == 0:
        status, expires_ts, days_left = "session", -1, None
    elif expires < now:
        status, expires_ts, days_left = "expired", expires, 0
    else:
        status, expires_ts = "valid", expires
        days_left = round((expires - now) / 86400, 1)

    platform = match_platform(domain, name)
    if platform:
        is_login = True
    elif name.lower() in GENERIC_LOGIN_KEYWORDS:
        is_login = True
        platform = None
    else:
        is_login = False
        platform = None

    return {
        "is_login": is_login,          # 是否登录锚点
        "platform": platform,
        "status": status,
        "expires_ts": expires_ts,
        "days_left": days_left,
        # 兼容旧字段
        "category": "login" if is_login else "tracking",
    }


def _registrable_site(domain: str) -> str:
    d = domain.lstrip(".")
    if not d:
        return "(未知)"
    parts = d.split(".")
    if len(parts) <= 2:
        return d
    compound = {"co.uk", "org.uk", "com.cn", "net.cn", "org.cn", "com.tw", "com.hk", "co.jp", "com.au"}
    tail2 = ".".join(parts[-2:])
    if tail2 in compound and len(parts) >= 3:
        return ".".join(parts[-3:])
    return tail2


def site_of(domain: str) -> tuple[str, str]:
    """返回 (site_key, 显示名)。优先公司归一，否则主站名。"""
    d = domain.lstrip(".")
    for domains, key, name in SITE_UNIFY:
        for dom in domains:
            if d == dom or d.endswith("." + dom):
                return key, name
    site = _registrable_site(domain)
    return site, site


def _platform_key_of(cookie: dict) -> str | None:
    """登录锚点的站点 key；非登录返回 None。供 server 复用。"""
    info = classify_cookie(cookie)
    if not info["is_login"]:
        return None
    key, _ = site_of(_domain_of(cookie))
    return key


def organize_cookies(cookies: list[dict]) -> dict[str, Any]:
    """站点聚合模型：每组 = 一个站点的全部 cookie，登录锚点置顶。

    返回 {
        sites: [ {key, name, cookies:[...](锚点在前), login_count, has_login, expired_count} ],
        stats: {...},
    }
    """
    groups: dict[str, dict] = {}
    for c in cookies:
        info = classify_cookie(c)
        merged = {**c, "_meta": info}
        key, name = site_of(_domain_of(c))
        g = groups.setdefault(key, {"key": key, "name": name, "cookies": [],
                                    "login_count": 0, "has_login": False, "expired_count": 0})
        g["cookies"].append(merged)
        if info["is_login"]:
            g["login_count"] += 1
            g["has_login"] = True
        if info["status"] == "expired":
            g["expired_count"] += 1

    # 站内排序：登录锚点在前，再按状态(valid>session>expired)、name
    def sort_key(ck):
        m = ck["_meta"]
        return (0 if m["is_login"] else 1,
                {"valid": 0, "session": 1, "expired": 2}[m["status"]],
                ck.get("name", ""))
    for g in groups.values():
        g["cookies"].sort(key=sort_key)

    # 组排序：含登录锚点的在前（按 login_count 降序），痕迹站点按 cookie 数降序
    site_list = sorted(groups.values(),
                       key=lambda g: (0 if g["has_login"] else 1, -g["login_count"], -len(g["cookies"])))

    login_sites = [g for g in site_list if g["has_login"]]
    total = len(cookies)
    login_count = sum(g["login_count"] for g in site_list)
    expired = sum(g["expired_count"] for g in site_list)

    tracking_sites = [g for g in site_list if not g["has_login"]]
    return {
        "sites": site_list,
        "login_sites": login_sites,        # 含锚点站点（可导出/延期/删除）
        "tracking_sites": tracking_sites,  # 无锚点站点（仅可展开查看/单个删/全删）
        "stats": {
            "total": total,
            "site_count": len(site_list),
            "login_site_count": len(login_sites),
            "login_count": login_count,
            "tracking_count": total - login_count,
            "expired_count": expired,
            "platforms": [g["name"] for g in login_sites],
            # 兼容旧字段
            "platform_count": len(login_sites),
            "tracking_site_count": len(tracking_sites),
        },
    }


def extend_cookie(cookie: dict, days: int = 30) -> dict:
    c = dict(cookie)
    now = time.time()
    base = c.get("expires", -1)
    if not isinstance(base, (int, float)) or base < now:
        base = now
    c["expires"] = base + days * 86400
    return c


def cookies_for_domain(all_cookies: list[dict], site: str) -> list[dict]:
    return [c for c in all_cookies if _registrable_site(_domain_of(c)) == site]


def cookies_for_tracking_group(all_cookies: list[dict], display_name: str) -> list[dict]:
    out = []
    for c in all_cookies:
        _, name = site_of(_domain_of(c))
        if name == display_name:
            out.append(c)
    return out


def cookies_for_site(all_cookies: list[dict], site_key: str) -> list[dict]:
    """某站点的全部 cookie。"""
    return [c for c in all_cookies if site_of(_domain_of(c))[0] == site_key]
