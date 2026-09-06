"""账号 profile 元数据管理 —— Schema v2（序号 + 随机ID 双标识）"""
import json, shutil, time, secrets, string
from pathlib import Path
from datetime import datetime
from copy import deepcopy

_ID_ALPHABET = string.ascii_lowercase + string.digits  # 8位乱码ID：小写字母+数字


def _gen_id(length: int = 8) -> str:
    """生成不重复的随机乱码 ID（作为存储目录名 + 唯一标识，永不变）。"""
    for _ in range(100):
        pid = "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))
        if not _path(pid).exists():
            return pid
    # 几乎不可能到这；兜底加长时间戳
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length)) + str(int(time.time()))[-4:]


def _assign_seq(metas: list) -> list:
    """按 created_at 排序动态分配序号（1,2,3...）。纯展示层，删除后自动缩进。"""
    def _key(m):
        return m.get("created_at") or ""
    ordered = sorted(metas, key=_key)
    for i, m in enumerate(ordered, 1):
        m["seq"] = i
    return metas

STORAGE = Path(__file__).parent / "storage"
AVATARS = ["🎬","🎯","🎨","🎵","📊","🚀","💡","🔥","🌟","📱","🎮","💎","🌍","🛒","📰"]

DEFAULT_PROFILE = {
    "basic": {"name": "", "group": "默认", "avatar": "🎬", "note": "", "startup_urls": [], "clear_cache_on_start": False, "hw_accel": True},
    "proxy": {"mode": "direct", "host": "", "port": 0, "username": "", "password": "", "ssh": {"host": "", "port": 22, "username": "", "password": ""}},
    "ip_info": None,
    "os": {"family": "windows", "version": "win10"},
    "ua": "",
    "kernel_version": "",
    "language": {"follow_ip": True, "locale": "zh-CN", "timezone": "Asia/Shanghai"},
    "webrtc": "private",
    "ignore_https_errors": False,
    "geolocation": {"mode": "ask", "based_on_ip": True, "lat": 0.0, "lon": 0.0, "accuracy": 100},
    "webgl": {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11)"},
    "device": {"name": "", "host_ip": "", "mac": ""},
    "hardware": {"cpu_cores": 8, "memory_gb": 16},
    "seed": "",
    "created_at": "",
    "updated_at": "",
}


def _path(pid): return STORAGE / pid
def _meta_path(pid): return _path(pid) / "profile.json"

def _read(pid):
    p = _meta_path(pid)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

def _write(pid, data):
    p = _meta_path(pid)
    data["updated_at"] = datetime.now().isoformat()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)

def _cookie_status(pid):
    c = _path(pid) / "userdata" / "Default" / "Network" / "Cookies"
    if not c.exists(): return {"has_cookies": False}
    st = c.stat()
    return {"has_cookies": True, "cookie_size": st.st_size, "cookie_updated": datetime.fromtimestamp(st.st_mtime).isoformat()}

def _migrate(data):
    """旧 schema → v2 迁移"""
    v2 = deepcopy(DEFAULT_PROFILE)
    if "name" in data:  # old flat schema
        v2["basic"]["name"] = data.get("name", "")
        v2["basic"]["group"] = data.get("group", "默认")
        v2["basic"]["avatar"] = data.get("avatar", "🎬")
        v2["basic"]["note"] = data.get("note", "")
        v2["proxy"] = data.get("proxy") or v2["proxy"]
        v2["os"]["family"] = data.get("os_type", "windows")
        v2["created_at"] = data.get("created_at", "")
        v2["seed"] = data.get("seed", "")
        return v2
    # Already v2 or partial: merge defaults
    for section in ["basic", "os", "language", "geolocation", "webgl", "device", "hardware"]:
        if section not in data:
            data[section] = deepcopy(v2[section])
        else:
            for k, v in v2[section].items():
                if k not in data[section]:
                    data[section][k] = v
    for key in ["proxy", "ip_info", "ua", "kernel_version", "webrtc", "ignore_https_errors", "seed", "created_at", "updated_at"]:
        if key not in data:
            data[key] = deepcopy(v2[key])
    return data


def list_all():
    result = []
    if not STORAGE.exists(): return result
    for d in sorted(STORAGE.iterdir()):
        if not d.is_dir() or d.name.startswith("_"): continue
        meta = _read(d.name) or {}
        meta = _migrate(meta)
        meta["id"] = d.name
        meta["cookie"] = _cookie_status(d.name)
        result.append(meta)
    return _assign_seq(result)

def get(pid):
    m = _read(pid)
    if not m: return None
    m = _migrate(m)
    m["id"] = pid
    m["cookie"] = _cookie_status(pid)
    # 序号基于全量排序得出，保证与列表一致
    for p in list_all():
        if p["id"] == pid:
            m["seq"] = p.get("seq")
            break
    return m

def create(**fields):
    pid = _gen_id(8)
    _path(pid).mkdir(parents=True, exist_ok=True)
    data = deepcopy(DEFAULT_PROFILE)
    # 支持嵌套 basic(前端提交 {basic:{name,note,avatar,...}})与扁平字段
    b = fields.get("basic") or {}
    data["basic"]["name"] = b.get("name", fields.get("name", pid))
    data["basic"]["group"] = b.get("group", fields.get("group", "默认"))
    data["basic"]["avatar"] = b.get("avatar", fields.get("avatar", "🎬"))
    data["basic"]["note"] = b.get("note", fields.get("note", ""))
    data["basic"]["startup_urls"] = b.get("startup_urls", fields.get("startup_urls", []))
    data["basic"]["clear_cache_on_start"] = b.get("clear_cache_on_start", fields.get("clear_cache_on_start", False))
    data["basic"]["hw_accel"] = b.get("hw_accel", fields.get("hw_accel", True))
    data["proxy"] = fields.get("proxy") or deepcopy(DEFAULT_PROFILE["proxy"])
    data["os"] = fields.get("os") or deepcopy(DEFAULT_PROFILE["os"])
    data["ua"] = fields.get("ua", "")
    data["kernel_version"] = fields.get("kernel_version", "")
    data["language"] = fields.get("language") or deepcopy(DEFAULT_PROFILE["language"])
    data["webrtc"] = fields.get("webrtc", "private")
    data["ignore_https_errors"] = fields.get("ignore_https_errors", False)
    data["geolocation"] = fields.get("geolocation") or deepcopy(DEFAULT_PROFILE["geolocation"])
    data["webgl"] = fields.get("webgl") or deepcopy(DEFAULT_PROFILE["webgl"])
    data["device"] = fields.get("device") or deepcopy(DEFAULT_PROFILE["device"])
    data["hardware"] = fields.get("hardware") or deepcopy(DEFAULT_PROFILE["hardware"])
    data["seed"] = fields.get("seed", pid)
    data["created_at"] = datetime.now().isoformat()
    _write(pid, data)
    return {"id": pid, **data}

def update(pid, **fields):
    meta = _read(pid) or {}
    meta = _migrate(meta)
    if "basic" in fields:
        for k, v in fields["basic"].items():
            if k in meta.get("basic", {}): meta["basic"][k] = v
    if "proxy" in fields: meta["proxy"] = fields["proxy"]
    if "os" in fields: meta["os"] = fields["os"]
    if "ua" in fields: meta["ua"] = fields["ua"]
    if "kernel_version" in fields: meta["kernel_version"] = fields["kernel_version"]
    if "language" in fields: meta["language"] = fields["language"]
    if "webrtc" in fields: meta["webrtc"] = fields["webrtc"]
    if "ignore_https_errors" in fields: meta["ignore_https_errors"] = fields["ignore_https_errors"]
    if "geolocation" in fields: meta["geolocation"] = fields["geolocation"]
    if "webgl" in fields: meta["webgl"] = fields["webgl"]
    if "device" in fields: meta["device"] = fields["device"]
    if "hardware" in fields: meta["hardware"] = fields["hardware"]
    if "seed" in fields: meta["seed"] = fields["seed"]
    if "startup_urls" in fields: meta["basic"]["startup_urls"] = fields["startup_urls"]
    if "clear_cache_on_start" in fields: meta["basic"]["clear_cache_on_start"] = fields["clear_cache_on_start"]
    if "hw_accel" in fields: meta["basic"]["hw_accel"] = fields["hw_accel"]
    if "name" in fields: meta["basic"]["name"] = fields["name"]
    if "group" in fields: meta["basic"]["group"] = fields["group"]
    if "avatar" in fields: meta["basic"]["avatar"] = fields["avatar"]
    if "note" in fields: meta["basic"]["note"] = fields["note"]
    meta["created_at"] = meta.get("created_at", datetime.now().isoformat())
    _write(pid, meta)
    return {**meta, "id": pid}

def delete(pid):
    p = _path(pid)
    if p.exists(): shutil.rmtree(p, ignore_errors=True)

GROUPS_FILE = "groups.json"
SETTINGS_FILE = "settings.json"
DEFAULT_BROWSER_DL_ROOT = str(Path.home() / "Downloads" / "OpenMedia Download")


def get_settings() -> dict:
    """全局设置（storage/settings.json）。含浏览器下载根目录。"""
    f = STORAGE / SETTINGS_FILE
    s = {}
    if f.exists():
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            s = {}
    if not s.get("browser_download_root"):
        s["browser_download_root"] = DEFAULT_BROWSER_DL_ROOT
    return s


def save_settings(s: dict):
    (STORAGE / SETTINGS_FILE).write_text(
        json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")



def _groups_path() -> Path:
    return STORAGE / GROUPS_FILE


def _load_groups() -> list:
    """读取独立分组表（含空分组）。默认始终含「默认」。"""
    f = _groups_path()
    gs = []
    if f.exists():
        try:
            gs = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            gs = []
    if "默认" not in gs:
        gs.insert(0, "默认")
    # 合并各窗口实际用到但不在表里的分组（兼容旧数据）
    for p in list_all():
        g = p.get("basic", {}).get("group", "默认")
        if g not in gs:
            gs.append(g)
    return gs


def _save_groups(gs: list):
    STORAGE.mkdir(parents=True, exist_ok=True)
    _groups_path().write_text(json.dumps(gs, ensure_ascii=False, indent=2), encoding="utf-8")


def list_groups():
    return sorted(_load_groups(), key=lambda g: (g != "默认", g))


def add_group(name: str):
    gs = _load_groups()
    if name not in gs:
        gs.append(name)
        _save_groups(gs)


def rename_group_in_table(old: str, new: str):
    gs = _load_groups()
    if old in gs:
        gs[gs.index(old)] = new
        _save_groups(gs)
    elif new not in gs:
        gs.append(new)
        _save_groups(gs)


def remove_group(name: str):
    # 该分组下所有窗口归入「默认」
    for p in list_all():
        if p.get("basic", {}).get("group") == name:
            update(p["id"], group="默认")
    gs = [g for g in _load_groups() if g != name]
    _save_groups(gs)

def list_avatars():
    return AVATARS
