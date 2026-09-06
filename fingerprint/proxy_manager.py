"""代理池管理：代理 CRUD、连通性测试、窗口绑定、批量导入。

存储: storage/proxies.json
  {
    "proxies": [ {id, name, scheme, ip_version, host, port, username, password, remark, udp, status, bound_pids, created_at} ],
  }
代理 scheme: http / https / socks5
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

STORAGE = Path(__file__).parent / "storage"
PROXIES_FILE = STORAGE / "proxies.json"

SCHEMES = {"http", "https", "socks5"}
IP_VERSIONS = {"ipv4", "ipv6"}


def _load() -> dict:
    if not PROXIES_FILE.exists():
        return {"proxies": []}
    try:
        return json.loads(PROXIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"proxies": []}


def _save(data: dict) -> None:
    STORAGE.mkdir(parents=True, exist_ok=True)
    PROXIES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _public(p: dict) -> dict:
    """返回给前端的代理信息（密码原样返回，前端控制显隐）。"""
    return {
        "id": p["id"],
        "name": p["name"],
        "scheme": p["scheme"],
        "ip_version": p.get("ip_version", "ipv4"),
        "host": p["host"],
        "port": p["port"],
        "username": p.get("username", ""),
        "password": p.get("password", ""),
        "remark": p.get("remark", ""),
        "udp": bool(p.get("udp", False)),
        "status": p.get("status", "未测试"),
        "bound_pids": p.get("bound_pids", []),
        "created_at": p.get("created_at", 0),
    }


def list_proxies() -> list:
    return [_public(p) for p in _load()["proxies"]]


def get_proxy(proxy_id: str) -> dict | None:
    for p in _load()["proxies"]:
        if p["id"] == proxy_id:
            return p
    return None


def _is_duplicate(data: dict, scheme: str, host: str, port: int, username: str, exclude_id: str = "") -> bool:
    for p in data["proxies"]:
        if exclude_id and p["id"] == exclude_id:
            continue
        if (p["scheme"] == scheme and p["host"] == host and int(p["port"]) == int(port)
                and p.get("username", "") == username):
            return True
    return False


def add_proxy(scheme: str, name: str, host: str, port: int, username: str = "", password: str = "",
              remark: str = "", udp: bool = False, ip_version: str = "ipv4",
              check_duplicate: bool = True) -> dict:
    scheme = (scheme or "").lower().strip()
    if scheme not in SCHEMES:
        raise ValueError(f"不支持的代理类型: {scheme}（仅 http/https/socks5）")
    if ip_version not in IP_VERSIONS:
        ip_version = "ipv4"
    if not host:
        raise ValueError("主机不能为空")
    port = int(port)
    if not (1 <= port <= 65535):
        raise ValueError("端口必须在 1-65535")
    if not name:
        name = f"{scheme}://{host}:{port}"
    # UDP 仅 socks5 有意义
    if scheme != "socks5":
        udp = False

    data = _load()
    if check_duplicate and _is_duplicate(data, scheme, host, port, username):
        raise ValueError(f"重复代理：已存在相同的 {scheme}://{host}:{port}")

    p = {
        "id": secrets.token_hex(5),
        "name": name,
        "scheme": scheme,
        "ip_version": ip_version,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "remark": remark,
        "udp": bool(udp),
        "status": "未测试",
        "bound_pids": [],
        "created_at": int(time.time()),
    }
    data["proxies"].append(p)
    _save(data)
    return _public(p)


def update_proxy(proxy_id: str, **fields) -> dict | None:
    data = _load()
    for p in data["proxies"]:
        if p["id"] == proxy_id:
            for k in ["name", "scheme", "ip_version", "host", "port", "username", "password", "remark", "udp"]:
                if k in fields and fields[k] is not None:
                    p[k] = fields[k]
            p["scheme"] = (p.get("scheme") or "http").lower()
            if p["scheme"] != "socks5":
                p["udp"] = False
            p["port"] = int(p["port"])
            _save(data)
            return _public(p)
    return None


def delete_proxy(proxy_id: str) -> bool:
    data = _load()
    before = len(data["proxies"])
    data["proxies"] = [p for p in data["proxies"] if p["id"] != proxy_id]
    if len(data["proxies"]) < before:
        _save(data)
        return True
    return False


def set_status(proxy_id: str, status: str) -> None:
    data = _load()
    for p in data["proxies"]:
        if p["id"] == proxy_id:
            p["status"] = status
            _save(data)
            return


def bind_windows(proxy_id: str, pids: list) -> dict | None:
    """绑定多个窗口（追加，去重，保持绑定先后顺序）。"""
    data = _load()
    for p in data["proxies"]:
        if p["id"] == proxy_id:
            cur = p.get("bound_pids", [])
            for pid in pids:
                if pid not in cur:
                    cur.append(pid)
            p["bound_pids"] = cur
            _save(data)
            return _public(p)
    return None


def unbind_window(proxy_id: str, pid: str) -> None:
    data = _load()
    for p in data["proxies"]:
        if p["id"] == proxy_id:
            p["bound_pids"] = [x for x in p.get("bound_pids", []) if x != pid]
            _save(data)
            return


def proxies_for_window(pid: str) -> list:
    """返回绑定到指定窗口的代理列表，按绑定先后（created_at 顺序已在 bound_pids 中保持）。"""
    result = []
    for p in _load()["proxies"]:
        if pid in p.get("bound_pids", []):
            result.append(_public(p))
    return result


def test_proxy_connectivity(proxy_id: str) -> dict:
    """测试代理连通性：经代理查询 IP，返回出口信息或失败。"""
    p = get_proxy(proxy_id)
    if not p:
        return {"ok": False, "msg": "代理不存在"}
    try:
        from media_core.fingerprint import netprobe
        info = netprobe.query_ip(
            proxy_type=p["scheme"], proxy_host=p["host"], proxy_port=int(p["port"]),
            proxy_user=p.get("username", ""), proxy_pass=p.get("password", ""))
        if info and info.get("ip"):
            status = f"可用 · {info['ip']}"
            if info.get("country"):
                status += f" ({info['country']})"
            set_status(proxy_id, status)
            return {"ok": True, "status": status, "info": info}
        set_status(proxy_id, "不可用")
        return {"ok": False, "status": "不可用", "msg": "无法通过代理获取IP"}
    except Exception as e:
        set_status(proxy_id, "不可用")
        return {"ok": False, "status": "不可用", "msg": str(e)[:200]}


def parse_proxy_line(line: str) -> dict | None:
    """解析一行手动粘贴的代理：模式,名称,主机,端口,用户名,密码,备注,UDP。
    支持全角/半角逗号，模式大小写不敏感，备注/UDP 可省略。"""
    line = line.strip()
    if not line:
        return None
    # 全角逗号 → 半角
    line = line.replace("，", ",").replace("、", ",")
    parts = [x.strip() for x in line.split(",")]
    if len(parts) < 4:
        raise ValueError(f"参数不足（至少4项: 模式,名称,主机,端口）: {line[:50]}")
    scheme = parts[0].lower()
    name = parts[1]
    host = parts[2]
    try:
        port = int(parts[3])
    except ValueError:
        raise ValueError(f"端口无效: {parts[3]}")
    username = parts[4] if len(parts) > 4 else ""
    password = parts[5] if len(parts) > 5 else ""
    remark = parts[6] if len(parts) > 6 else ""
    udp_s = (parts[7] if len(parts) > 7 else "").lower()
    udp = udp_s in ("true", "1", "yes", "是")
    return {"scheme": scheme, "name": name, "host": host, "port": port,
            "username": username, "password": password, "remark": remark, "udp": udp}


def import_proxies(items: list, check_duplicate: bool = True) -> dict:
    """批量导入。items 为 parse_proxy_line 解析后的字典列表。返回 {added, skipped, errors}。"""
    added, skipped, errors = 0, 0, []
    for i, it in enumerate(items):
        try:
            add_proxy(check_duplicate=check_duplicate, **it)
            added += 1
        except ValueError as e:
            if "重复代理" in str(e):
                skipped += 1
            else:
                errors.append(f"第{i+1}条: {e}")
        except Exception as e:
            errors.append(f"第{i+1}条: {e}")
    return {"added": added, "skipped": skipped, "errors": errors}
