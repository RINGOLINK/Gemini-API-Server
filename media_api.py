# -*- coding: utf-8 -*-
"""
media_api.py — 生图/生视频/生音乐三媒体生成服务

原理: Gemini 网页版支持图片(Imagein)/视频(Veo)/音乐(Lyria)生成,
gemini_webapi 2.1.1 原生解析三种媒体对象(Image / GeneratedVideo / GeneratedMedia),
视频/音乐内置 206 轮询下载(生成需 1~3 分钟,库自动每 10s 重试直到就绪)。

交付模式: 媒体经库 save() 落盘(下载需账号 Cookie 鉴权,库自动带 client_ref 会话),
再由 /v1/media/file/{media_id} 签名静态服务对外提供 ——
不把上游 URL 暴露给客户端(离开会话即失效),不做无鉴权代理转发。

磁盘策略: media_store/{job_id}/ 目录,TTL(默认 24h)后台清理。
"""
import asyncio
import base64
import hashlib
import hmac as _hmac
import os
import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse

# ───────── 配置 ─────────
MEDIA_ROOT = Path(__file__).resolve().parent / "media_store"
MEDIA_TTL_HOURS = float(os.environ.get("GEMINI_MEDIA_TTL_HOURS", "24"))
MEDIA_GEN_TIMEOUT = float(os.environ.get("GEMINI_MEDIA_TIMEOUT", "280"))    # 单次生成总预算(秒)
MEDIA_DL_TIMEOUT = float(os.environ.get("GEMINI_MEDIA_DL_TIMEOUT", "240"))  # 单媒体下载+轮询超时
MEDIA_MAX_CONCURRENT = int(os.environ.get("GEMINI_MEDIA_CONCURRENCY", "2"))

_media_sem = asyncio.Semaphore(MEDIA_MAX_CONCURRENT)
_jobs: dict = {}   # job_id -> {status, kind, prompt, created, files, error, pid, elapsed}
_jobs_max = 200
_img_exts = {".png", ".jpg", ".jpeg", ".webp"}
_vid_exts = {".mp4", ".webm"}
_aud_exts = {".mp3", ".wav"}


def _tlog():
    from main import trace_logger
    return trace_logger


# ───────── 任务表 ─────────
def _new_job(kind: str, prompt: str) -> str:
    job_id = uuid.uuid4().hex[:16]
    _jobs[job_id] = {"status": "pending", "kind": kind, "prompt": str(prompt)[:200],
                     "created": time.time(), "files": [], "error": ""}
    if len(_jobs) > _jobs_max:
        done = sorted([k for k, v in _jobs.items() if v["status"] in ("done", "failed")],
                      key=lambda k: _jobs[k]["created"])
        for k in done[: len(done) // 2]:
            _jobs.pop(k, None)
    return job_id


def _default_base() -> str:
    """媒体文件 URL 的默认基址: 优先 PUBLIC_BASE_URL,否则指向 4444 主服务(文件服务所在)。"""
    from main import PUBLIC_BASE_URL
    return str(PUBLIC_BASE_URL).rstrip("/") if PUBLIC_BASE_URL else "http://127.0.0.1:4444"


def _signed_media_url(media_id: str, request=None, ttl: int = 86400, base: str = "") -> str:
    from main import PUBLIC_BASE_URL, SIGNATURE_SECRET
    exp = int(time.time()) + ttl
    sig = _hmac.new(str(SIGNATURE_SECRET).encode(), f"{media_id}|{exp}".encode(),
                    hashlib.sha256).hexdigest()
    if not base:
        base = _default_base() if request is None else str(request.base_url).rstrip("/")
    return f"{base}/v1/media/file/{media_id}?exp={exp}&sig={sig}"


def job_snapshot(job_id: str, request=None) -> dict:
    j = _jobs.get(job_id)
    if not j:
        raise HTTPException(404, f"媒体任务不存在: {job_id}")
    out = dict(j)
    out["job_id"] = job_id
    out["files"] = [{"media_id": f["media_id"], "kind": f["kind"], "filename": f["filename"],
                     "bytes": f["bytes"], "url": _signed_media_url(f["media_id"], request)}
                    for f in j.get("files", [])]
    return out


# ───────── 文件定位 ─────────
def serve_media(media_id: str) -> tuple[Path, str]:
    """按 media_id(=文件名)定位媒体文件,防路径穿越。"""
    if media_id != Path(media_id).name or ".." in media_id or "/" in media_id or "\\" in media_id:
        raise HTTPException(400, "非法 media_id")
    if not MEDIA_ROOT.exists():
        raise HTTPException(404, "媒体文件不存在或已过期")
    hits = list(MEDIA_ROOT.glob(f"*/{media_id}"))
    if not hits:
        raise HTTPException(404, "媒体文件不存在或已过期(默认保留 24 小时)")
    f = hits[0]
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg", ".wav": "audio/wav",
            }.get(f.suffix.lower(), "application/octet-stream")
    return f, mime


async def _ttl_cleanup_loop():
    """每小时清理超过 TTL 的媒体目录(磁盘防爆)。"""
    while True:
        try:
            await asyncio.sleep(3600)
            if not MEDIA_ROOT.exists():
                continue
            now = time.time()
            removed = 0
            for d in MEDIA_ROOT.iterdir():
                if d.is_dir() and now - d.stat().st_mtime > MEDIA_TTL_HOURS * 3600:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            if removed:
                _tlog().info("[media] TTL 清理: 删除 %d 个过期媒体目录", removed)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _tlog().warning("[media] TTL 清理异常: %s", e)


# ───────── 核心: 落盘 ─────────
def _media_dir(job_id: str) -> Path:
    d = MEDIA_ROOT / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _classify(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in _img_exts:
        return "image"
    if ext in _vid_exts:
        return "video"
    if ext in _aud_exts:
        return "audio"
    return "file"


async def _save_media_objects(resp, job_id: str) -> list:
    """把库返回的媒体对象落盘(目录差集法: 不假设返回形态与扩展名)。
    视频/音乐的 save() 内置 206 轮询;库自动携带账号 Cookie 鉴权下载。"""
    d = _media_dir(job_id)
    before = {q.name for q in d.iterdir()} if d.exists() else set()

    for img in (getattr(resp, "images", None) or []):
        try:
            await asyncio.wait_for(img.save(path=str(d), verbose=False), timeout=MEDIA_DL_TIMEOUT)
        except Exception as e:
            _tlog().warning("[media] 图片下载失败 job=%s: %s", job_id, e)
    for v in (getattr(resp, "videos", None) or []):
        try:
            await asyncio.wait_for(v.save(path=str(d), verbose=False), timeout=MEDIA_DL_TIMEOUT)
        except Exception as e:
            _tlog().warning("[media] 视频下载失败(可能仍在生成) job=%s: %s", job_id, e)
    for c in (getattr(resp, "candidates", None) or []):
        for md in (getattr(c, "generated_media", None) or []):
            try:
                await asyncio.wait_for(md.save(path=str(d), download_type="audio", verbose=False),
                                       timeout=MEDIA_DL_TIMEOUT)
            except Exception as e:
                _tlog().warning("[media] 音乐下载失败 job=%s: %s", job_id, e)

    files = []
    for q in sorted(d.iterdir()):
        if q.name in before or not q.is_file() or q.stat().st_size == 0:
            continue
        files.append({"media_id": q.name, "kind": _classify(q), "filename": q.name,
                      "bytes": q.stat().st_size})
    return files


# ───────── 参考输入支持 ─────────
# 图生图/图生视频/音频参考: 端点接收 image/audio 字段(base64 data-url 或 http(s) url),
# 统一落盘为临时文件后经 generate_content(files=[...]) 注入(库 upload_file → ttl_1d 资源)。

def _resolve_reference_input(refs, kind_hint: str) -> list[str]:
    """把参考输入字段解析为本地临时文件路径列表。
    refs: None / str / list[str] (每个元素为 data-url 或 http(s) url)
    返回文件路径列表;解析失败抛 400。"""
    if not refs:
        return []
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list):
        raise HTTPException(400, "参考输入(image/audio)必须是 base64 data-url 或 URL 字符串,或字符串数组")
    out = []
    for i, item in enumerate(refs):
        if not isinstance(item, str) or not item.strip():
            continue
        item = item.strip()
        ext = ".png"
        if kind_hint == "audio":
            ext = ".mp3"
        try:
            if item.startswith("data:"):
                # data:image/png;base64,xxxx
                head, _, b64 = item.partition(",")
                if "base64" not in head:
                    raise ValueError("仅支持 base64 data-url")
                m = head.split(";")[0].split(":")[1] if ":" in head else "image/png"
                if "audio" in m:
                    ext = ".mp3"
                elif "video" in m:
                    ext = ".mp4"
                elif "jpeg" in m or "jpg" in m:
                    ext = ".jpg"
                elif "webp" in m:
                    ext = ".webp"
                data = base64.b64decode(b64)
            elif item.startswith(("http://", "https://")):
                import urllib.request as _ur
                with _ur.urlopen(item, timeout=30) as r:
                    data = r.read()
                ct = r.headers.get("Content-Type", "")
                if "audio" in ct:
                    ext = ".mp3"
                elif "video" in ct:
                    ext = ".mp4"
            else:
                raise ValueError("参考输入必须是 data-url 或 http(s) URL")
            if not data:
                raise ValueError("参考输入内容为空")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"参考输入 #{i + 1} 解析失败: {str(e)[:120]}")
        tmp = MEDIA_ROOT / f"_ref_{uuid.uuid4().hex[:12]}{ext}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        out.append(str(tmp))
    return out


def _cleanup_refs(paths: list) -> None:
    for p in paths or []:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass


def _probe_proxy() -> str | None:
    """探测可用上传代理(用于 content-push 上传;直连 multipart POST 常被运营商掐断)。
    优先环境变量 GEMINI_UPLOAD_PROXY,其次常用本地端口;不可达返回 None。"""
    import socket as _sk
    cands = []
    env = (os.environ.get("GEMINI_UPLOAD_PROXY") or "").strip()
    if env:
        cands.append(env)
    for _port in (12000, 7890, 10809, 1080):
        s = _sk.socket(); s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", _port)); cands.append(f"http://127.0.0.1:{_port}")
        except Exception:
            pass
        finally:
            try: s.close()
            except Exception: pass
    return cands[0] if cands else None


async def _upload_refs_proxy(ref_files: list, client, proxy: str) -> list:
    """用独立代理会话上传参考文件到 content-push,返回 [[[rid], filename], ...]。
    与直连会话解耦: 上传走代理(绕被掐),生成仍用直连会话(避代理 IP 风控)。"""
    from gemini_webapi.utils.upload_file import upload_file
    from curl_cffi.requests import AsyncSession as _AS
    out = []
    async with _AS(impersonate="chrome", proxy=proxy, allow_redirects=True) as up:
        for fp in ref_files:
            fname = Path(fp).name
            rid = await asyncio.wait_for(
                upload_file(fp, client=up, push_id=client.push_id, filename=fname), timeout=60)
            out.append([[rid], fname])
    return out


async def generate_media(kind: str, prompt: str, wait: bool = True,
                         refs: list | None = None, ref_kind: str = "image") -> dict:
    """媒体生成主入口。kind: image | video | music。
    refs: 参考输入(图生图/图生视频的 image,或音频参考的 audio),base64 data-url 或 URL。"""
    if kind not in ("image", "video", "music"):
        raise HTTPException(400, f"未知媒体类型: {kind}")
    if not prompt or not str(prompt).strip():
        raise HTTPException(400, "prompt 不能为空")
    job_id = _new_job(kind, str(prompt))
    if not wait:
        asyncio.get_running_loop().create_task(_do_generate(job_id, kind, str(prompt), refs, ref_kind))
        return {"ok": True, "job_id": job_id, "status": "pending"}
    return await _do_generate(job_id, kind, str(prompt), refs, ref_kind)


async def _do_generate(job_id: str, kind: str, prompt: str,
                       refs: list | None = None, ref_kind: str = "image") -> dict:
    j = _jobs[job_id]
    ref_files = []
    async with _media_sem:
        j["status"] = "generating"
        t0 = time.time()
        try:
            # 参考输入 → 临时文件(图生图/图生视频/音频参考)
            try:
                ref_files = _resolve_reference_input(refs, ref_kind)
                if ref_files:
                    j["ref_count"] = len(ref_files)
                    _tlog().info("[media] %s 携带参考输入 %d 个 job=%s", kind, len(ref_files), job_id)
            except HTTPException as e:
                j["status"] = "failed"
                j["error"] = str(e.detail)
                return job_snapshot(job_id)
            # 正规取号(与业务请求同路径): 粘性/健康路由 + 惰性初始化 + 信号量限流
            from main import acquire_client, release_client, _affinity_key
            client = await acquire_client(affinity_key="")
            pid = ""
            try:
                from main import _client_checkout, asyncio as _aio
                cur = _aio.current_task()
                chk = _client_checkout.get(cur)
                pid = chk["pid"] if chk else ""
            except Exception:
                pass
            j["pid"] = pid
            _tlog().info("[media] %s 生成开始 pid=%s job=%s prompt=%r refs=%d",
                         kind, pid, job_id, prompt[:60], len(ref_files))
            try:
                gen_kwargs = {}
                used_proxy_upload = False
                if ref_files:
                    # 直连 content-push multipart POST 常被运营商掐(21s timeout);
                    # 有可用本地代理时改走"代理上传 + 直连生成"(req_file_data 引用已上传资源)。
                    _pxy = _probe_proxy()
                    if _pxy and getattr(client, "push_id", None):
                        try:
                            req_data = await _upload_refs_proxy(ref_files, client, _pxy)
                            if not req_data:
                                raise RuntimeError("代理上传返回空")
                            used_proxy_upload = True
                            _tlog().info("[media] 参考图经代理上传 %d 个(proxy=%s)", len(req_data), _pxy)
                            resp = None
                            async for _out in client._generate(prompt, req_file_data=req_data,
                                                               temporary=True):
                                resp = _out
                            files = await _save_media_objects(resp, job_id)
                        except Exception as _ue:
                            if used_proxy_upload:
                                # 上传已成功,失败发生在生成阶段 → 直接报错,绝不回落重传(浪费3分钟)
                                raise
                            _tlog().warning("[media] 代理上传阶段失败,回落直连 files=: %s", str(_ue)[:100])
                            used_proxy_upload = False
                if not used_proxy_upload:
                    gen_kwargs = {}
                    if ref_files:
                        gen_kwargs["files"] = ref_files
                    resp = await asyncio.wait_for(client.generate_content(prompt, **gen_kwargs),
                                                  timeout=MEDIA_GEN_TIMEOUT)
                    files = await _save_media_objects(resp, job_id)
            finally:
                # 生成完成立即归还(媒体下载已由 save 内部完成,无需长期占用会话)
                await release_client()
            if not files:
                txt = (getattr(resp, "text", "") or "").strip()
                j["status"] = "failed"
                j["error"] = f"模型未返回媒体文件。模型回复: {txt[:150] or '(空)'}"
                _tlog().warning("[media] %s 未产出文件 job=%s text=%r", kind, job_id, txt[:100])
                return job_snapshot(job_id)
            j["status"] = "done"
            j["files"] = files
            j["elapsed"] = round(time.time() - t0, 1)
            _tlog().info("[media] %s 完成 job=%s files=%d %.1fs", kind, job_id, len(files), time.time() - t0)
            return job_snapshot(job_id)
        except HTTPException:
            raise
        except asyncio.TimeoutError:
            j["status"] = "failed"
            j["error"] = f"生成超时({MEDIA_GEN_TIMEOUT:.0f}s)——视频类生成较慢,建议 wait=false 异步模式"
            return job_snapshot(job_id)
        except Exception as e:
            j["status"] = "failed"
            j["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            _tlog().warning("[media] %s 生成失败 job=%s %s", kind, job_id, j["error"])
            return job_snapshot(job_id)
        finally:
            _cleanup_refs(ref_files)


# ═════════ FastAPI 路由 ═════════
router = APIRouter()


async def _require_api_key(authorization: str = Header(None)):
    """生成类端点鉴权: 复用主 API_KEY 校验(文件服务走签名 URL,不在此列)。"""
    from main import verify_api_key
    return await verify_api_key(authorization)


def _with_urls(snap: dict, request=None) -> dict:
    snap["files"] = [{"media_id": f["media_id"], "kind": f["kind"], "filename": f["filename"],
                      "bytes": f["bytes"], "url": _signed_media_url(f["media_id"], request)}
                     for f in snap.get("files", [])]
    return snap


@router.get("/v1/media/file/{media_id}")
async def media_file(media_id: str, exp: int = 0, sig: str = ""):
    """签名静态媒体服务(HMAC(media_id|exp),与图片代理同安全模型)。"""
    if not exp or not sig:
        raise HTTPException(403, "缺少访问签名")
    if exp < time.time():
        raise HTTPException(403, "链接已过期")
    from main import SIGNATURE_SECRET
    want = _hmac.new(str(SIGNATURE_SECRET).encode(), f"{media_id}|{exp}".encode(),
                     hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(want, sig):
        raise HTTPException(403, "签名无效")
    path, mime = serve_media(media_id)
    return FileResponse(path, media_type=mime, filename=media_id)


@router.post("/v1/images/generations", dependencies=[Depends(_require_api_key)])
async def images_generations(body: dict, request: Request):
    """OpenAI 兼容生图端点。body: {prompt, n=1, response_format: url|b64_json, size(忽略), image: 参考图(图生图, base64 data-url 或 URL 或数组)}"""
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    try:
        n = max(1, min(int(body.get("n") or 1), 4))
    except Exception:
        n = 1
    rf = str(body.get("response_format") or "url").lower()
    refs = body.get("image") or body.get("images")
    snap = await generate_media("image", prompt, wait=True, refs=refs, ref_kind="image")
    if snap.get("status") != "done":
        err = snap.get("error") or "生图失败"
        el = err.lower()
        if "limit" in el or "额度" in err or "配额" in err:
            raise HTTPException(429, err)
        raise HTTPException(422, err)
    imgs = [f for f in snap.get("files", []) if f["kind"] == "image"]
    if not imgs:
        raise HTTPException(422, "模型未返回图片文件")
    data = []
    for f in imgs[:n]:
        if rf == "b64_json":
            raw = (MEDIA_ROOT / snap["job_id"] / f["filename"]).read_bytes()
            data.append({"b64_json": base64.b64encode(raw).decode()})
        else:
            data.append({"url": _signed_media_url(f["media_id"], request)})
    return {"created": int(time.time()), "data": data}


@router.post("/v1/media/video", dependencies=[Depends(_require_api_key)])
async def media_video(body: dict, request: Request):
    """视频生成(Veo)。body: {prompt, wait=true, image: 参考图(图生视频首帧, base64 data-url 或 URL 或数组)}"""
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    wait = bool(body.get("wait", True))
    refs = body.get("image") or body.get("images")
    snap = await generate_media("video", prompt, wait=wait, refs=refs, ref_kind="image")
    if wait:
        snap = _with_urls(snap, request)
        if snap.get("status") != "done":
            err = snap.get("error") or ""
            if "limit" in err.lower() or "额度" in err:
                raise HTTPException(429, err)
        return snap
    return snap


@router.post("/v1/media/music", dependencies=[Depends(_require_api_key)])
async def media_music(body: dict, request: Request):
    """音乐生成(Lyria)。body: {prompt, wait=true, audio: 音频参考(可选)}"""
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    wait = bool(body.get("wait", True))
    refs = body.get("audio")
    snap = await generate_media("music", prompt, wait=wait, refs=refs, ref_kind="audio")
    if wait:
        snap = _with_urls(snap, request)
        if snap.get("status") != "done":
            err = snap.get("error") or ""
            if "limit" in err.lower() or "额度" in err:
                raise HTTPException(429, err)
        return snap
    return snap


@router.post("/v1/media/image", dependencies=[Depends(_require_api_key)])
async def media_image(body: dict, request: Request):
    """图片生成原生形状(与 /v1/images/generations 同能力,返回文件明细)。"""
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    refs = body.get("image") or body.get("images")
    snap = await generate_media("image", prompt, wait=True, refs=refs, ref_kind="image")
    return _with_urls(snap, request)


@router.get("/v1/media/jobs/{job_id}", dependencies=[Depends(_require_api_key)])
async def media_job(job_id: str, request: Request):
    """异步任务状态轮询。"""
    return _with_urls(job_snapshot(job_id), request)


def jobs_list(limit: int = 50, base: str = "") -> list:
    """最近任务清单(看板用),新→旧。"""
    out = []
    if not base:
        base = _default_base()
    for jid, j in sorted(_jobs.items(), key=lambda kv: kv[1]["created"], reverse=True)[:limit]:
        e = dict(j)
        e["job_id"] = jid
        e["files"] = [{"media_id": f["media_id"], "kind": f["kind"], "filename": f["filename"],
                       "bytes": f["bytes"],
                       "url": _signed_media_url(f["media_id"], ttl=86400, base=base)}
                      for f in j.get("files", [])]
        out.append(e)
    return out

def delete_job(job_id: str) -> bool:
    """删除一个媒体任务及其磁盘文件(看板删除用)。"""
    if job_id not in _jobs:
        return False
    _jobs.pop(job_id, None)
    shutil.rmtree(MEDIA_ROOT / job_id, ignore_errors=True)
    return True