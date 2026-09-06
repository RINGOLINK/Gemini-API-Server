"""代理与 IP 查询服务"""
import httpx
from datetime import datetime

def query_ip(proxy_type: str = "direct", proxy_host: str = "", proxy_port: int = 0,
             proxy_user: str = "", proxy_pass: str = "") -> dict | None:
    """查询 IP 信息（ip-api.com）

    三种模式：
    - direct: 完全绕过任何代理（含系统代理）
    - system: 跟随系统代理设置（有则走，无则直连）
    - socks5/http/https: 使用指定代理
    """
    try:
        client_kwargs = {"timeout": 10}

        if proxy_type == "direct":
            # 显式禁用所有代理，包括环境变量中的系统代理
            client_kwargs["trust_env"] = False
        elif proxy_type == "system":
            # 跟随系统代理：直接读注册表，与浏览器保持同源
            client_kwargs["trust_env"] = False
            sys_proxy = get_system_proxy()
            if sys_proxy:
                client_kwargs["proxy"] = sys_proxy
        elif proxy_host:
            # 指定代理
            client_kwargs["trust_env"] = False
            if proxy_user:
                proxy_url = f"{proxy_type}://{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
            else:
                proxy_url = f"{proxy_type}://{proxy_host}:{proxy_port}"
            client_kwargs["proxy"] = proxy_url
        else:
            client_kwargs["trust_env"] = False

        with httpx.Client(**client_kwargs) as client:
            # 先试 ipinfo.io(实测多数网络可达、稳定),失败再回退 ip-api.com
            return _fetch_ip(client)
    except Exception:
        pass
    return None


def _fetch_ip(client) -> dict | None:
    urls = [
        "https://ipinfo.io/json",
        "http://ip-api.com/json/?fields=status,message,country,countryCode,regionName,city,lat,lon,query",
    ]
    for url in urls:
        try:
            r = client.get(url, timeout=10)
            if r.status_code != 200:
                continue
            try:
                data = r.json()
            except Exception:
                continue
            if "ip" in data:
                loc = data.get("loc", "")
                lat, lon = (loc.split(",")[:2] + ["0", "0"])[:2]
                return {
                    "ip": data.get("ip"),
                    "country": data.get("country"),
                    "country_code": data.get("countryCode") or data.get("country_code"),
                    "region": data.get("region") or data.get("regionName"),
                    "city": data.get("city"),
                    "lat": float(lat) if str(lat).replace(".", "", 1).isdigit() else 0,
                    "lon": float(lon) if str(lon).replace(".", "", 1).isdigit() else 0,
                    "org": data.get("org", ""),
                    "queried_at": datetime.now().isoformat(),
                }
            if data.get("status") == "success" and data.get("query"):
                return {
                    "ip": data.get("query"), "country": data.get("country"),
                    "country_code": data.get("countryCode"), "region": data.get("regionName"),
                    "city": data.get("city"), "lat": data.get("lat", 0), "lon": data.get("lon", 0),
                    "queried_at": datetime.now().isoformat(),
                }
        except Exception:
            continue
    return None


def resolve_proxy_config(profile_config: dict) -> tuple:
    """从 profile 配置中解析代理参数"""
    proxy = profile_config.get("proxy", {})
    if not proxy:
        return ("direct", "", 0, "", "")
    mode = proxy.get("mode", "direct")
    if mode == "direct":
        return ("direct", "", 0, "", "")
    if mode == "system":
        return ("system", "", 0, "", "")
    return (
        proxy.get("mode", "direct"),
        proxy.get("host", ""),
        proxy.get("port", 0),
        proxy.get("username", ""),
        proxy.get("password", ""),
    )

def get_system_proxy() -> str:
    """读取 Windows 系统代理设置，返回 http://host:port 或 ""。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        try:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
        except FileNotFoundError:
            return ""
        if not enabled:
            return ""
        try:
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
        except FileNotFoundError:
            return ""
        if not server:
            return ""
        # ProxyServer 可能是 "host:port" 或 "http=host:port;https=host:port"
        if "=" in server:
            parts = dict(
                seg.split("=", 1) for seg in server.split(";") if "=" in seg
            )
            server = parts.get("http") or parts.get("https") or ""
        if not server:
            return ""
        if "://" not in server:
            server = "http://" + server
        return server
    except Exception:
        return ""
