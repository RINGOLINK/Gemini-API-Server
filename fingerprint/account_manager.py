"""平台账号管理模块 —— 存储/加密/隔离

设计：
- 每个窗口的账号存于 storage/{pid}/accounts.json，与窗口指纹/cookie 同目录（天然隔离）。
- 密码用窗口指纹 seed 派生的 AES-256-GCM 密钥加密存储（无明文落盘）。
- 自动填充时由后端解密后注入页面（密钥不离开后端）。
- LOGO 缓存于 storage/{pid}/logos/{hash}.png，随窗口目录隔离。
"""
from __future__ import annotations
import json, base64, hashlib, os, time
from pathlib import Path
from typing import Any

STORAGE = Path(__file__).parent / "storage"


def _accounts_path(pid: str) -> Path:
    return STORAGE / pid / "accounts.json"


def _logos_dir(pid: str) -> Path:
    d = STORAGE / pid / "logos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _profile_seed(pid: str) -> bytes:
    """从窗口指纹配置取 seed 派生密钥；无则用 pid（保证可解密但弱）。"""
    fp_path = STORAGE / pid / "fingerprint.json"
    seed_str = pid
    if fp_path.exists():
        try:
            fp = json.loads(fp_path.read_text(encoding="utf-8"))
            # 优先用 seed 字段，否则用整个指纹的哈希作稳定熵源
            seed_str = str(fp.get("seed") or fp.get("fingerprint_seed") or
                          hashlib.sha256(fp_path.read_bytes()).hexdigest())
        except Exception:
            seed_str = pid
    return hashlib.sha256(f"openmedia-account:{seed_str}".encode()).digest()


def _get_key(pid: str) -> bytes:
    return _profile_seed(pid)


def encrypt_password(pid: str, plaintext: str) -> str:
    """AES-256-GCM 加密，返回 base64(nonce + ct)。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _get_key(pid)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode()


def decrypt_password(pid: str, token: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _get_key(pid)
    raw = base64.b64decode(token)
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")


def _load(pid: str) -> list[dict]:
    p = _accounts_path(pid)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(pid: str, accounts: list[dict]):
    p = _accounts_path(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")


def _norm_host(url_or_host: str) -> str:
    """归一化平台标识为主机名（去协议/www/路径/端口/userinfo）。

    安全：带协议的 URL 必须用 urlparse 提取 hostname——手工 split 会被
    userinfo 钓鱼绕过（https://douyin.com:443@evil.com/ 旧逻辑误判 douyin.com）。
    """
    from urllib.parse import urlparse
    s = (url_or_host or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        try:
            h = urlparse(s).hostname or ""
        except Exception:
            return ""
    else:
        # 无协议输入（用户手填的 platform 标识，如 "douyin.com" 或 "douyin.com/login"）
        h = s.split("/")[0].split("?")[0].split("#")[0]
        h = h.split("@")[-1].split(":")[0]  # 手填也剥掉 userinfo/端口
    if h.startswith("www."):
        h = h[4:]
    return h


def list_accounts(pid: str, with_password: bool = False) -> list[dict]:
    """返回账号列表。默认不含密码（安全）；with_password 用于自动填充。"""
    out = []
    for a in _load(pid):
        item = {
            "id": a.get("id"),
            "platform": a.get("platform"),      # 原始 URL 或主机名
            "host": a.get("host"),
            "username": a.get("username"),
            "logo": a.get("logo"),              # 相对路径 /api/profiles/{pid}/accounts/logo/{file}
            "created_at": a.get("created_at"),
            "has_totp": bool(a.get("totp_secret_enc")),   # 仅标记,密钥本身不回显
        }
        if with_password:
            try:
                item["password"] = decrypt_password(pid, a.get("password_enc", ""))
            except Exception:
                item["password"] = ""
            try:
                item["totp_secret"] = decrypt_password(pid, a.get("totp_secret_enc", ""))
            except Exception:
                item["totp_secret"] = ""
        out.append(item)
    return out


def add_account(pid: str, platform: str, username: str, password: str, logo: str = "", totp_secret: str = "") -> dict:
    accounts = _load(pid)
    host = _norm_host(platform)
    acc = {
        "id": hashlib.md5(f"{host}:{username}:{time.time()}".encode()).hexdigest()[:10],
        "platform": platform,
        "host": host,
        "username": username,
        "password_enc": encrypt_password(pid, password),
        "totp_secret_enc": encrypt_password(pid, totp_secret) if totp_secret else "",
        "logo": logo,
        "created_at": int(time.time()),
    }
    accounts.append(acc)
    _save(pid, accounts)
    # 同步写入 Chrome Login Data（原生密码填充）
    try:
        from media_core.fingerprint import login_data as ld
        ld.add_login(pid, platform, username, password)
    except Exception:
        pass
    return {k: v for k, v in acc.items() if k not in ("password_enc", "totp_secret_enc")}


def update_account(pid: str, acc_id: str, **fields) -> bool:
    accounts = _load(pid)
    for a in accounts:
        if a.get("id") == acc_id:
            if "platform" in fields:
                a["platform"] = fields["platform"]
                a["host"] = _norm_host(fields["platform"])
            if "username" in fields:
                a["username"] = fields["username"]
            if "password" in fields and fields["password"]:
                a["password_enc"] = encrypt_password(pid, fields["password"])
            if "totp_secret" in fields:
                a["totp_secret_enc"] = encrypt_password(pid, fields["totp_secret"]) if fields["totp_secret"] else ""
            if "logo" in fields:
                a["logo"] = fields["logo"]
            _save(pid, accounts)
            # 同步 Login Data：删旧记录，按最新值重写
            try:
                from media_core.fingerprint import login_data as ld
                ld.remove_login(pid, a.get("platform", ""), a.get("username", ""))
                new_pw = None
                if "password" in fields and fields["password"]:
                    new_pw = fields["password"]
                else:
                    try:
                        new_pw = decrypt_password(pid, a.get("password_enc", ""))
                    except Exception:
                        new_pw = None
                if new_pw:
                    ld.add_login(pid, a.get("platform", ""), a.get("username", ""), new_pw)
            except Exception:
                pass
            return True
    return False


def delete_account(pid: str, acc_id: str) -> bool:
    accounts = _load(pid)
    target = next((a for a in accounts if a.get("id") == acc_id), None)
    new = [a for a in accounts if a.get("id") != acc_id]
    if len(new) != len(accounts):
        _save(pid, new)
        # 同步删 Chrome Login Data
        if target:
            try:
                from media_core.fingerprint import login_data as ld
                ld.remove_login(pid, target.get("platform", ""), target.get("username", ""))
            except Exception:
                pass
        return True
    return False


def find_account_for_url(pid: str, url: str) -> dict | None:
    """根据当前页面 URL 找匹配账号（host 后缀匹配）。返回含明文密码。"""
    host = _norm_host(url)
    best = None
    for a in list_accounts(pid, with_password=True):
        ah = a.get("host", "")
        if not ah:
            continue
        # host 相等 或 当前 host 是账号 host 的子域（如 login.douyin.com 匹配 douyin.com）
        if host == ah or host.endswith("." + ah) or ah.endswith("." + host):
            # 取最长匹配（更精确）
            if best is None or len(ah) > len(best.get("host", "")):
                best = a
    return best


def save_logo(pid: str, data: bytes, ext: str = ".png") -> str:
    """缓存 LOGO，返回相对访问路径的文件名。"""
    fname = hashlib.md5(data).hexdigest()[:12] + ext
    (_logos_dir(pid) / fname).write_bytes(data)
    return fname


def logo_path(pid: str, fname: str) -> Path | None:
    p = _logos_dir(pid) / fname
    # 防路径穿越
    if p.parent != _logos_dir(pid) or not p.exists():
        return None
    return p
