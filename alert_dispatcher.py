# -*- coding: utf-8 -*-
"""
alert_dispatcher.py — 外部告警通道分发引擎 (方向一)
支持: 通用 Webhook / Bark (iOS) / Server酱 (微信) / Telegram Bot
特性: 异步非阻塞、同类告警智能防抖去重(静默窗口)、测试连通性
"""

import os
import time
import json
import asyncio
import logging
from typing import Dict, Any, Optional
import httpx

logger = logging.getLogger("alert_dispatcher")

# 默认配置路径与缓存
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "secrets", "alert_config.json")
_LOCK = asyncio.Lock()

# 默认配置结构
DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "channel": "webhook",  # webhook | bark | serverchan | telegram
    "webhook_url": "",
    "webhook_headers": "",  # 逗号分隔 Key: Value
    "bark_key": "",
    "bark_server": "https://api.day.app",
    "serverchan_key": "",
    "telegram_token": "",
    "telegram_chat_id": "",
    "cooldown_seconds": 300,  # 同类告警 5 分钟内不重复轰炸
    "min_level": "WARNING",    # INFO | WARNING | ERROR
}

_config_cache: Optional[Dict[str, Any]] = None
_recent_dispatches: Dict[str, float] = {}  # hash -> last_sent_ts


def load_alert_config() -> Dict[str, Any]:
    """读取告警配置，若无则初始化默认值"""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                merged = dict(DEFAULT_CONFIG)
                merged.update(data)
                _config_cache = merged
                return _config_cache
        except Exception as e:
            logger.warning(f"读取告警配置失败: {e}")
    _config_cache = dict(DEFAULT_CONFIG)
    return _config_cache


def save_alert_config(new_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """保存告警配置并更新缓存"""
    global _config_cache
    merged = dict(DEFAULT_CONFIG)
    merged.update(new_cfg)
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        _config_cache = merged
        return _config_cache
    except Exception as e:
        logger.error(f"保存告警配置失败: {e}")
        raise


def _level_weight(level: str) -> int:
    levels = {"INFO": 10, "WARNING": 20, "ERROR": 30, "CRITICAL": 40}
    return levels.get(level.upper(), 0)


async def send_alert_async(title: str, message: str, level: str = "WARNING", category: str = "default") -> bool:
    """异步分发告警至配置的外部通道"""
    cfg = load_alert_config()
    if not cfg.get("enabled"):
        return False

    # 级别过滤
    if _level_weight(level) < _level_weight(cfg.get("min_level", "WARNING")):
        return False

    # 防抖与冷却：同一类别/内容的告警在 cooldown 内静默
    cooldown = float(cfg.get("cooldown_seconds", 300))
    now = time.time()
    dedup_key = f"{category}:{level}:{title}:{message[:50]}"
    last_sent = _recent_dispatches.get(dedup_key, 0.0)
    if now - last_sent < cooldown:
        return False

    _recent_dispatches[dedup_key] = now
    # 清理陈旧的防抖缓存 (只保留最近 100 条)
    if len(_recent_dispatches) > 200:
        for k in list(_recent_dispatches.keys())[:-100]:
            _recent_dispatches.pop(k, None)

    channel = cfg.get("channel", "webhook").lower()
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            if channel == "bark":
                return await _send_bark(client, cfg, title, message, level)
            elif channel == "serverchan":
                return await _send_serverchan(client, cfg, title, message, level)
            elif channel == "telegram":
                return await _send_telegram(client, cfg, title, message, level)
            else:
                return await _send_webhook(client, cfg, title, message, level)
    except Exception as e:
        logger.warning(f"[alert_dispatcher] 告警分发异常 ({channel}): {e}")
        return False


async def _send_webhook(client: httpx.AsyncClient, cfg: dict, title: str, message: str, level: str) -> bool:
    url = cfg.get("webhook_url", "").strip()
    if not url:
        return False
    headers = {"Content-Type": "application/json"}
    raw_hdr = cfg.get("webhook_headers", "").strip()
    if raw_hdr:
        for line in raw_hdr.split(","):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()

    payload = {
        "msg_type": "text",
        "title": f"[{level}] {title}",
        "content": f"【Gemini-API 运维告警】\n级别: {level}\n标题: {title}\n详情: {message}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "text": {
            "content": f"[{level}] {title}\n{message}"
        },
        "markdown": {
            "content": f"### [{level}] {title}\n> {message}\n*时间: {time.strftime('%Y-%m-%d %H:%M:%S')}*"
        }
    }
    res = await client.post(url, json=payload, headers=headers)
    return res.status_code < 400


async def _send_bark(client: httpx.AsyncClient, cfg: dict, title: str, message: str, level: str) -> bool:
    key = cfg.get("bark_key", "").strip()
    if not key:
        return False
    server = cfg.get("bark_server", "https://api.day.app").rstrip("/")
    url = f"{server}/{key}"
    level_group = "Gemini-API-Error" if level in ("ERROR", "CRITICAL") else "Gemini-API-Warn"
    payload = {
        "title": f"[{level}] {title}",
        "body": message,
        "group": level_group,
        "icon": "https://www.gstatic.com/images/branding/product/2x/googleg_48dp.png"
    }
    res = await client.post(url, json=payload)
    return res.status_code < 400


async def _send_serverchan(client: httpx.AsyncClient, cfg: dict, title: str, message: str, level: str) -> bool:
    key = cfg.get("serverchan_key", "").strip()
    if not key:
        return False
    url = f"https://sctapi.ftqq.com/{key}.send"
    payload = {
        "title": f"[{level}] {title}"[:32],
        "desp": f"### Gemini-API 运行告警\n- **等级**: {level}\n- **详情**: {message}\n- **时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    }
    res = await client.post(url, data=payload)
    return res.status_code < 400


async def _send_telegram(client: httpx.AsyncClient, cfg: dict, title: str, message: str, level: str) -> bool:
    token = cfg.get("telegram_token", "").strip()
    chat_id = cfg.get("telegram_chat_id", "").strip()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = f"🚨 *[{level}] {title}*\n\n{message}\n\n🕒 `{time.strftime('%Y-%m-%d %H:%M:%S')}`"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    res = await client.post(url, json=payload)
    return res.status_code < 400


async def test_alert_channel(temp_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """立即发送一条测试告警，返回详细发送结果"""
    channel = temp_cfg.get("channel", "webhook").lower()
    test_title = "测试告警通知"
    test_message = f"这是一条来自 Gemini-API 控制面板的测试消息 (通道: {channel})，收到说明配置成功。"
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=12.0, trust_env=False) as client:
            if channel == "bark":
                ok = await _send_bark(client, temp_cfg, test_title, test_message, "INFO")
            elif channel == "serverchan":
                ok = await _send_serverchan(client, temp_cfg, test_title, test_message, "INFO")
            elif channel == "telegram":
                ok = await _send_telegram(client, temp_cfg, test_title, test_message, "INFO")
            else:
                ok = await _send_webhook(client, temp_cfg, test_title, test_message, "INFO")
        elapsed_ms = round((time.time() - t0) * 1000)
        if ok:
            return {"ok": True, "message": f"测试成功！耗时 {elapsed_ms}ms", "elapsed_ms": elapsed_ms}
        else:
            return {"ok": False, "error": "通道返回异常状态码或未响应，请检查参数配置与网络连通性。"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)}"}
