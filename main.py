import asyncio
import base64
from collections import deque
import hashlib
import hmac
import importlib.metadata
import io
import json
import logging
import os
import re
import secrets
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote, urlparse

from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv(Path(__file__).parent / ".env")

import httpx
import numpy as np
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from gemini_webapi import GeminiClient, set_log_level
from gemini_webapi.constants import Model
from PIL import Image
from pydantic import BaseModel

import tools_shim  # webTools 模拟工具调用
# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
set_log_level("INFO")

# ============ 上游调用追踪(TTFB / queueing 误读取证) ============
# 背景: 2026-09-05 三次 502 的根因定位 —— 库内 _generate 的停滞看门狗在
# `is_thinking or is_queueing` 时阈值从 min(timeout, watchdog=150) 延长为 timeout(300s),
# 而 queueing 的判定(part[5] 为非空 list)存在误读前科(e=4 错误帧的 [5]=[3] 即触发)。
# 误读后: 库静等 300s → 重连 → recovery 轮询(150~300s) → 正是 334/467/634s 的构成。
# 本模块把库内部 DEBUG 现场与代理侧 TTFB 观测落盘到 logs/upstream_trace.log,下次挂起直接取证。
from logging.handlers import RotatingFileHandler
from contextvars import ContextVar

TRACE_LOG_PATH = Path(__file__).resolve().parent / "logs" / "upstream_trace.log"
TRACE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
_TRACE_FILE_MAX = 8 * 1024 * 1024

trace_logger = logging.getLogger("gemini_trace")
trace_logger.setLevel(logging.DEBUG)
trace_logger.propagate = False
if not trace_logger.handlers:
	_th = RotatingFileHandler(TRACE_LOG_PATH, maxBytes=_TRACE_FILE_MAX, backupCount=3, encoding="utf-8")
	_th.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
	trace_logger.addHandler(_th)

# 库内部日志(gemini_webapi):DEBUG 全量落盘(含 queueing/watchdog/Stream suspended 现场),
# 但阻断向控制台传播(set_log_level 的控制台输出保持原状,避免刷屏)
_lib_logger = logging.getLogger("gemini_webapi")
_lib_logger.setLevel(logging.DEBUG)
_lib_logger.propagate = False
for _h in list(_lib_logger.handlers):
	if isinstance(_h, logging.StreamHandler) and not isinstance(_h, RotatingFileHandler):
		_h.setLevel(logging.WARNING)  # 控制台只保留 WARNING+
if not any(isinstance(_h, RotatingFileHandler) for _h in _lib_logger.handlers):
	_lh = RotatingFileHandler(TRACE_LOG_PATH, maxBytes=_TRACE_FILE_MAX, backupCount=3, encoding="utf-8")
	_lh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
	_lh.setLevel(logging.DEBUG)
	_lib_logger.addHandler(_lh)

# 挂起判定阈值:健康时 Gemini 首块 <10s 到达;超过此值视为 stall(可用环境变量调)
GEMINI_STALL_TTFB = float(os.environ.get("GEMINI_STALL_TTFB", "60"))

# 每请求追踪上下文:端点写入请求元数据,观测器回填 TTFB/chunks(同 client 并发复用时按任务上下文隔离)
_trace_ctx: ContextVar = ContextVar("gemini_req_trace", default=None)


# ───────── 账号健康分 + 自适应熔断(Step1: Health-Weighted Router) ─────────
# 数据源: 观测器每次成功回填 TTFB / 失败与 stall 记录错误链(账号级,按 pid 落到 _pool 条目)。
# 打分: TTFB 40% + 配额 30% + 近期停滞 20% + 连续错误 10% → 0~100。
# 熔断: 连续 _CIRCUIT_ERR_TRIP 次失败 → open(阶梯退避 30s→2m→10m);
#       退避到期后由选择器作为"半开探针"最后手段放行,成功即关闭熔断。
_HS_TTFB_GOOD = 3.0            # ≤3s 首块视为优秀(该维度满分)
_HS_TTFB_BAD = 30.0            # ≥30s 视为瘫痪(该维度 0 分)
_HS_QUOTA_FULL = 1000.0        # 剩余配额 ≥1000 时配额维度满分
_CIRCUIT_ERR_TRIP = 3          # 连续失败多少次打开熔断
_CIRCUIT_LADDERS = (30, 120, 600)  # 阶梯退避秒数(第 1/2/3+ 次开断)
_HS_WINDOW = 10                # 滚动 TTFB 样本窗口


def _health_init(st: dict) -> dict:
	"""为账号状态补健康监控字段(幂等;兼容热代码升级后的旧条目)。"""
	st.setdefault("_ttfbs", deque(maxlen=_HS_WINDOW))
	st.setdefault("_err_streak", 0)
	st.setdefault("_stalls_recent", 0)
	st.setdefault("_circuit", "closed")
	st.setdefault("_circuit_until", 0.0)
	st.setdefault("_circuit_opens", 0)
	st.setdefault("_circuit_reason", "")
	st.setdefault("_last_ttfb", None)
	st.setdefault("_ttfb_history", deque(maxlen=20))
	st.setdefault("isolated", False)  # Step4: 手动隔离(看板按钮),隔离账号不参与路由
	return st


def _mark_account_stall(pid: str):
	"""记录一次账号级 stall:累计计数(展示) + 健康衰减(stall 计入错误链与近期停滞)。"""
	if not pid:
		return
	st = _pool.get(pid)
	if st is not None:
		st["stall_count"] = (st.get("stall_count") or 0) + 1
		st["last_stall_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
		_account_record_failure(pid, "stall")


def _record_account_error(pid: str, kind: str = "error"):
	"""兼容旧调用名,委托统一失败记录。"""
	_account_record_failure(pid, kind)


def _account_ttfb_avg(st: dict):
	q = _health_init(st)["_ttfbs"]
	return (sum(q) / len(q)) if q else None


def _account_health_score(st: dict) -> float:
	"""综合健康分 0~100。无 TTFB 样本的新账号该维度给 0.8 中性值(不垫底也不虚高)。"""
	st = _health_init(st)
	avg = _account_ttfb_avg(st)
	if avg is None:
		ttfb_s = 0.8
	else:
		ttfb_s = max(0.0, 1.0 - max(avg - _HS_TTFB_GOOD, 0.0) / (_HS_TTFB_BAD - _HS_TTFB_GOOD))
	rem = (st.get("quota") or {}).get("remaining")
	if rem is None:
		quota_s = 0.6 if st.get("status") == "ok" else 0.0
	else:
		quota_s = max(0.0, min(1.0, (rem - POOL_QUOTA_MIN) / (_HS_QUOTA_FULL - POOL_QUOTA_MIN)))
	stall_s = max(0.0, 1.0 - 0.5 * (st.get("_stalls_recent") or 0))
	err_s = max(0.0, 1.0 - 0.4 * (st.get("_err_streak") or 0))
	return round(40 * ttfb_s + 30 * quota_s + 20 * stall_s + 10 * err_s, 1)


def _circuit_label(st: dict) -> str:
	"""closed | open | half_open(退避到期,可作探针放行)。"""
	st = _health_init(st)
	if st.get("_circuit") != "open":
		return "closed"
	return "half_open" if time.time() >= (st.get("_circuit_until") or 0) else "open"


def _circuit_is_open(st: dict) -> bool:
	"""是否处于熔断拦截期(open 且未到期)。到期后视为可探针,交由选择器最后手段放行。"""
	st = _health_init(st)
	if st.get("_circuit") != "open":
		return False
	return time.time() < (st.get("_circuit_until") or 0)


def _circuit_open(st: dict, reason: str):
	st = _health_init(st)
	opens = st.get("_circuit_opens") or 0
	ladder = _CIRCUIT_LADDERS[min(opens, len(_CIRCUIT_LADDERS) - 1)]
	st["_circuit"] = "open"
	st["_circuit_until"] = time.time() + ladder
	st["_circuit_opens"] = opens + 1
	st["_circuit_reason"] = f"{reason}×{st.get('_err_streak')}"[:120]
	_log_bridge(f"[熔断] 账号 {st['pid']} 打开熔断 {ladder}s(第 {opens + 1} 级, 原因 {reason[:60]})", "WARNING")
	_push_alert(f"账号 {st['pid']} 打开熔断 {ladder}s(第 {opens + 1} 级: {reason[:40]})", "WARNING", st["pid"])


def _circuit_close(st: dict, pid: str):
	st = _health_init(st)
	if st.get("_circuit") == "open":
		_log_bridge(f"[熔断] 账号 {pid} 探针成功,熔断关闭", "SUCCESS")
	st["_circuit"] = "closed"
	st["_circuit_until"] = 0.0
	st["_circuit_opens"] = 0
	st["_circuit_reason"] = ""


def _account_record_success(pid, ttfb=None):
	"""一次成功生成:清空错误链与近期停滞、回填 TTFB、若有熔断则关闭。"""
	st = _pool.get(pid)
	if not st:
		return
	st = _health_init(st)
	st["_err_streak"] = 0
	st["_stalls_recent"] = 0
	if ttfb is not None:
		st["_last_ttfb"] = round(ttfb, 2)
		st["_ttfbs"].append(round(ttfb, 2))
		# TTFB 走势(带时间戳,环形缓冲,供看板画图)
		st.setdefault("_ttfb_history", deque(maxlen=20)).append(
			{"t": time.time(), "v": round(ttfb, 2)})
	if st.get("_circuit") == "open":
		_circuit_close(st, pid)


def _account_record_failure(pid, kind="error"):
	"""一次生成失败:错误链 +1(stall 另计近期停滞);达阈值打开熔断。
	kind: error | stall | cancel —— cancel 是客户端断连,不算账号故障。"""
	if not pid:
		return
	st = _pool.get(pid)
	if not st:
		return
	st = _health_init(st)
	if kind == "cancel":
		return
	# 代理判死期间的失败是"代理的锅": 不累积账号错误链(否则代理一抖,健康账号全被熔断阶梯误杀)
	if kind == "error" and _proxy_down(_account_proxy_url(st)):
		trace_logger.info("[代理] 账号 %s 失败归因代理判死,不计错误链(kind=%s)", st.get("pid"), kind)
		return
	st["_err_streak"] = (st.get("_err_streak") or 0) + 1
	if kind == "stall":
		st["_stalls_recent"] = (st.get("_stalls_recent") or 0) + 1
	# 达阈值: 未熔断 → 打开;已熔断但退避已到期(半开探针失败)→ 升级重开(下一级阶梯)
	if st.get("_err_streak", 0) >= _CIRCUIT_ERR_TRIP:
		if st.get("_circuit") != "open" or time.time() >= (st.get("_circuit_until") or 0):
			_circuit_open(st, kind)


def _install_generate_observer():
	"""给 GeminiClient._generate 包一层 TTFB 观测器(类级一次性安装)。
	- 每个上游调用的首块到达时间(TTFB)写入 _trace_ctx
	- TTFB 超阈值 → 记 stall(账号级计数 + 追踪日志)
	- 异常与取消均记录(取消 = 客户端断连,用于核对僵尸工作消灭效果)"""
	if getattr(GeminiClient, "_trace_installed", False):
		return
	_orig_generate = GeminiClient._generate

	async def _observed_generate(self, *args, **kwargs):
		t0 = time.time()
		# 追踪状态优先从 kwargs 取(端点经 gen_kwargs 注入,确定性传递;
		# pop 掉防止泄漏到库内部的 stream 调用),ContextVar 作为兜底
		state = kwargs.pop("_trace_state", None) or _trace_ctx.get()
		first_at = None
		n = 0
		errored = False
		try:
			async for chunk in _orig_generate(self, *args, **kwargs):
				now = time.time() - t0
				if first_at is None:
					first_at = now
					if state is not None:
						state.setdefault("ttfb", []).append(round(first_at, 2))
						if first_at > GEMINI_STALL_TTFB:
							state["stall"] = True
							trace_logger.warning(
								"[stall] TTFB %.1fs > %.0fs (chars=%s pid=%s) —— 上游首块严重迟到",
								first_at, GEMINI_STALL_TTFB, state.get("chars"), state.get("pid"),
							)
							_mark_account_stall(state.get("pid"))
				# 思考/内容拆分:thinking 块先行 → first_content_at 即"思考结束、正文开始"时刻
				if state is not None:
					is_think = bool(getattr(chunk, "thoughts_delta", None)) or bool(getattr(chunk, "thinking", None))
					if is_think:
						state["think_chunks"] = state.get("think_chunks", 0) + 1
					elif state.get("first_content_at") is None:
						state["first_content_at"] = round(now, 2)
				n += 1
				yield chunk
		except BaseException as e:
			errored = True
			if state is not None:
				state["error"] = f"{type(e).__name__}: {str(e)[:180]}"
			if isinstance(e, asyncio.CancelledError) and state is not None:
				state["cancelled"] = True
			# 健康记录: 客户端断连/消费方提前停止(cancel)不罚账号;其余视为账号级失败
			if state is not None and state.get("pid"):
				if isinstance(e, (asyncio.CancelledError, GeneratorExit)):
					_account_record_failure(state["pid"], "cancel")
				else:
					_account_record_failure(state["pid"], "error")
			raise
		finally:
			if state is not None:
				state["chunks"] = state.get("chunks", 0) + n
				state.setdefault("gen_seconds", []).append(round(time.time() - t0, 2))
			# 正常完成(未抛异常且有首块)→ 记成功并回填 TTFB(健康分数据源)。
			# 放在 finally 内而非其后: 库若提前 break/close 迭代器,finally 仍会执行。
			if (not errored and state is not None and state.get("pid")
					and n > 0 and first_at is not None):
				_account_record_success(state["pid"], first_at)

	GeminiClient._generate = _observed_generate
	GeminiClient._trace_installed = True


_install_generate_observer()
# ============ 追踪模块结束 ============

gemini_client = None
gemini_client_lock = asyncio.Lock()
# 初始化失败冷却:避免 cookie 过期时每个请求都触发 30s 重新初始化
_init_failure_ts = 0.0
_INIT_RETRY_COOLDOWN = 60
# 客户端实际连接模式: proxy / direct / unknown(供 UI 显示,admin /api/quota 返回)
CLIENT_MODE = "unknown"

# ───────── 每账号单例会话(根治 cookie 旋转互踩) ─────────
# 背景: Google 的 __Secure-1PSIDTS 是"最新有效"语义——同一账号下任何一方 rotate
# (浏览器窗口 / gemini_webapi auto_refresh / 新建会话),其他方手里的值立即进入失效
# 倒计时。此前 3 副本各自 auto_refresh + init 触发 rotate,导致副本间、与服务端和
# 浏览器窗口之间互相顶掉 → 间歇 UNAUTHENTICATED 连坐(两个客户端一起挂)。
# 架构: 每账号 1 个常驻 client(auto_refresh=False,库不再自发 rotate);
# rotate 权威交给指纹窗口浏览器(它持续导出最新 cookie 文件);
# _pool_scan 发现 psidts 变化 → 该账号 client 热重建;同账号并发用信号量限流。
_PER_ACCT_CONCURRENCY = int(os.environ.get("GEMINI_PER_ACCOUNT_CONCURRENCY", "8"))
_ACQUIRE_WAIT = float(os.environ.get("GEMINI_ACQUIRE_WAIT", "60"))  # F13: 取号排队上限(旧值 300s 太长)
_account_clients: dict = {}          # pid -> {"client","psid","psidts"} 或 None(待重建)
_account_build_locks: dict = {}      # pid -> asyncio.Lock(重建串行)
_account_recovering: dict = {}       # pid -> 自愈进行中的时间戳(去重:并发失败方快速 503,不排队等 32s 自愈)
_ACCOUNT_RECOVER_COOLDOWN = 120      # 自愈去重窗口(秒):期间同账号请求直接快速 503
_account_init_locks: dict = {}       # pid -> asyncio.Lock(账号初始化互斥,并行启动防重复建连)
_client_checkout = {}                # id(asyncio.current_task) -> {"pid","client","sem"}

# ───────── 静默体检员(Step2):后台巡逻,提前发现并刷新快过期账号 ─────────
# 痛点: 账号 cookie 过期是被动发现的——用户请求撞上失效才现场 headless 抢救(白等 30s)。
# 方案: 后台每 PATROL_INTERVAL 巡逻一次,对每个存活账号发一次 1s 级 quota 探针;
#       连续 PATROL_PROBE_FAIL_LIMIT 次探针失败 → 提前 headless 刷新会话(复用自愈路径),
#       在用户撞上之前就把账号修好。刷新有冷却去重,不与业务请求抢号。
PATROL_INTERVAL = float(os.environ.get("GEMINI_PATROL_INTERVAL", "900"))       # 巡逻周期(秒),默认 15 分钟
PATROL_PROBE_FAIL_LIMIT = int(os.environ.get("GEMINI_PATROL_PROBE_FAIL_LIMIT", "2"))  # 连续探针失败几次触发自愈
_patrol_suspects: dict = {}          # pid -> 连续探针失败次数
_patrol_last_run = 0.0               # 最近一次巡逻时间戳(供看板展示)
# Token 用量累计(上行 prompt / 下行 completion / 总量)
# 口径: chars/4(与 DSH 估算对齐,空格分词对中文严重低估);持久化到 logs/token_usage.json,
# 代理重启(admin restart)不清零 —— "从打开项目起累计,调用一次统计一次"
TOKEN_USAGE = {"prompt": 0, "completion": 0, "total": 0, "since": 0}
_TOKEN_USAGE_PATH = Path(__file__).resolve().parent / "logs" / "token_usage.json"


def _load_token_usage():
	try:
		d = json.loads(_TOKEN_USAGE_PATH.read_text(encoding="utf-8"))
		if isinstance(d, dict):
			for k in ("prompt", "completion", "total"):
				TOKEN_USAGE[k] = int(d.get(k, 0) or 0)
			TOKEN_USAGE["since"] = float(d.get("since") or time.time())
			return
	except Exception:
		pass
	TOKEN_USAGE["since"] = time.time()


_load_token_usage()


def _count_tokens(text: str) -> int:
	"""chars/4 估算(与 DSH 客户端口径一致)"""
	return max(1, len(text or "") // 4)


def _enforce_json_output(text: str) -> str:
	"""F9: response_format=json_object 的兜底 —— 模型输出不保证是纯 JSON,
	提取首个平衡的 {...} 块返回;提取失败则返回原始文本(尽力而为,不虚构)。"""
	if not text or not text.strip():
		return text
	t = text.strip()
	# 已是合法 JSON → 原样
	try:
		json.loads(t)
		return t
	except (json.JSONDecodeError, ValueError):
		pass
	# 提取首个平衡花括号块(跳过字符串内的括号)
	start = t.find("{")
	if start == -1:
		return text
	depth = 0
	in_str = False
	esc = False
	for i in range(start, len(t)):
		ch = t[i]
		if in_str:
			if esc:
				esc = False
			elif ch == "\\":
				esc = True
			elif ch == '"':
				in_str = False
			continue
		if ch == '"':
			in_str = True
		elif ch == "{":
			depth += 1
		elif ch == "}":
			depth -= 1
			if depth == 0:
				cand = t[start:i + 1]
				try:
					json.loads(cand)
					return cand
				except (json.JSONDecodeError, ValueError):
					return text
	return text


def _bump_token_usage(pt: int, ct: int):
	"""累计 token 用量并落盘(原子写,失败静默不影响业务)"""
	TOKEN_USAGE["prompt"] += pt
	TOKEN_USAGE["completion"] += ct
	TOKEN_USAGE["total"] += pt + ct
	try:
		_TOKEN_USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
		_tmp = _TOKEN_USAGE_PATH.with_suffix(".tmp")
		_tmp.write_text(json.dumps(TOKEN_USAGE), encoding="utf-8")
		_tmp.replace(_TOKEN_USAGE_PATH)
	except Exception:
		pass


# 自动切换 Cookie(额度不足切换其他账号)开关
AUTO_SWITCH_COOKIE = os.environ.get("AUTO_SWITCH_COOKIE", "true").lower() == "true"
# Cookie 推送记录(最近一次把窗口 cookie 成功推送到服务端的时间,启动自动推送 / 手动切换都算)
COOKIE_PUSH_TIME = ""

# 服务配置（从环境变量读取，修改后需重启服务生效）
HOST = os.environ.get("HOST", "0.0.0.0")
try:
	PORT = int(os.environ.get("PORT", "4444"))
except ValueError:
	raise ValueError(f"Invalid PORT environment variable: '{os.environ.get('PORT')}' must be an integer")


async def _init_gemini_client_background():
	"""Background task: initialize the Gemini client without blocking startup.
	启动提速: 先把池内所有账号【并行轻量初始化】(跳过生成探针, 8 RPC 校验项齐全),
	先就绪先服务;原串行逐账号 × (300s×3 重试 + 生成探针) 的分钟级等待不复存在。"""
	try:
		_pool_scan()
		if len(_pool) > 1:
			import asyncio as _aio
			_targets = [st for st in _pool.values() if st.get("client") is None and (st.get("psid") or "")]
			if _targets:
				logger.info("[startup] 并行初始化 %d 个账号(轻量模式)...", len(_targets))
				await _aio.gather(*(_init_pool_account(st, light=True) for st in _targets),
								  return_exceptions=True)
		await get_gemini_client()
		logger.info("Gemini client initialized successfully in background")
	except Exception as e:
		logger.warning(f"Gemini client init failed (service running without API access): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
	"""Start Gemini client init in background; do not block FastAPI startup."""
	init_task = asyncio.create_task(_init_gemini_client_background())
	# 静默体检员(Step2): 后台巡逻账号健康,提前刷新快过期会话
	patrol_task = asyncio.create_task(_account_patrol_loop())
	# 代理池健康探测(方向二): 后台周期探测出口代理,判死联动路由排除/告警/免误杀
	proxy_task = asyncio.create_task(_proxy_health_loop())
	# 媒体生成 TTL 清理(media_store 24h 自动清理)
	import media_api
	media_ttl_task = asyncio.create_task(media_api._ttl_cleanup_loop())
	try:
		yield
	finally:
		# Wait for background init to finish before cleaning up (or cancel if still running)
		if not init_task.done():
			init_task.cancel()
			try:
				await init_task
			except asyncio.CancelledError:
				pass
		patrol_task.cancel()
		try:
			await patrol_task
		except asyncio.CancelledError:
			pass
		proxy_task.cancel()
		try:
			await proxy_task
		except asyncio.CancelledError:
			pass
		media_ttl_task.cancel()
		try:
			await media_ttl_task
		except asyncio.CancelledError:
			pass
		global gemini_client
		if gemini_client is not None:
			try:
				await gemini_client.close()
			except Exception as e:
				logger.warning(f"Failed to close Gemini client during shutdown: {e}")
			finally:
				gemini_client = None


app = FastAPI(title="Gemini API FastAPI Server", lifespan=lifespan)

# 管理面板集成（必须在 app 初始化之后，admin.py 依赖 main 模块的配置和 app 实例）
from admin import router as admin_router, setup_middleware, request_chars_var
app.include_router(admin_router)
setup_middleware(app)

# 媒体生成服务(生图/生视频/生音乐): 路由定义见 media_api.py,
# 在 verify_api_key 定义之后挂载(见文件后段 include_router)


def get_gemini_webapi_version() -> str:
	"""Return the installed gemini-webapi package version for runtime diagnostics."""
	try:
		return importlib.metadata.version("gemini-webapi")
	except importlib.metadata.PackageNotFoundError:
		return "unknown"


def get_cached_1psidts_path(psid: str) -> str:
	"""Return the cache path for a rotated 1PSIDTS value."""
	if not psid or not re.match("^[\\w\\-\\.]+$", psid):
		return ""
	return os.path.join(GEMINI_COOKIE_PATH, f".cached_1psidts_{psid}.txt")


def load_cached_1psidts(psid: str) -> str:
	"""Load a cached rotated 1PSIDTS value for the given 1PSID."""
	cached_file_path = get_cached_1psidts_path(psid)
	if not cached_file_path:
		return ""

	if os.path.exists(cached_file_path):
		try:
			content = Path(cached_file_path).read_text().strip()
			if content:
				return content
		except Exception as e:
			logger.warning(f"Error reading cache file {cached_file_path}: {e}")

	return ""


def get_cookie_value(cookies, name: str) -> str:
	"""Safely read a cookie value from an httpx cookie jar or mapping."""
	if not cookies:
		return ""

	for domain in (".google.com", ".googleusercontent.com", None):
		try:
			value = cookies.get(name, domain=domain) if domain is not None else cookies.get(name)
		except TypeError:
			value = cookies.get(name)
		except Exception:
			value = ""

		if value:
			return value

	return ""


# Add CORS middleware
app.add_middleware(
	CORSMiddleware,
	allow_origins=["*"],
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)

# Authentication credentials
SECURE_1PSID = os.environ.get("SECURE_1PSID", "")
SECURE_1PSIDTS = os.environ.get("SECURE_1PSIDTS", "")
API_KEY = os.environ.get("API_KEY", "")
ENABLE_THINKING = os.environ.get("ENABLE_THINKING", "false").lower() == "true"
TEMPORARY_CHAT = os.environ.get("TEMPORARY_CHAT", "false").lower() == "true"
DISABLE_BUILTIN_MODELS = os.environ.get("DISABLE_BUILTIN_MODELS", "false").lower() == "true"
AUTO_DELETE_CHAT = os.environ.get("AUTO_DELETE_CHAT", "true").lower() == "true" and not TEMPORARY_CHAT
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
GEMINI_PROXY = os.environ.get("GEMINI_PROXY", "").strip()


def _norm_proxy(p):
    """把认证 socks5 代理统一转成本地自研转发器(无认证本地端口),避免 curl_cffi 直连
    认证 SOCKS5 报 libcurl (97) 且无法远端 DNS——与浏览器窗口走同一路径。非 socks 认证原样返回。"""
    if not p:
        return None
    try:
        if isinstance(p, dict):
            mode = p.get("mode")
            if mode in ("socks5", "socks5h", "socks") and p.get("username"):
                from fingerprint import local_socks
                lp = local_socks.start_local_socks(
                    str(p.get("host", "")).strip(), int(p.get("port") or 1080),
                    str(p.get("username")), str(p.get("password") or ""), max_concurrent=64)
                return f"socks5h://127.0.0.1:{lp}"
            return None
        # str 形式,如 socks5h://user:pass@host:port
        s = str(p).strip()
        from urllib.parse import urlparse
        u = urlparse(s)
        if u.scheme in ("socks5", "socks5h", "socks") and u.username:
            from fingerprint import local_socks
            lp = local_socks.start_local_socks(
                u.hostname or "", int(u.port or 1080), u.username, u.password or "", max_concurrent=64)
            return f"socks5h://127.0.0.1:{lp}"
    except Exception:
        return p
    return p
PARALLEL_TOOL_CALLS = os.environ.get("PARALLEL_TOOL_CALLS", "true").lower() == "true"
SECRET_FILE_PATH = os.path.join(os.path.dirname(__file__), "secrets", "proxy_secret")
GEMINI_COOKIE_PATH = os.path.join(os.path.dirname(__file__), "secrets")
SESSION_VALIDATION_PROMPT = "Reply with exactly OK."
GEM_ID = os.environ.get("GEM_ID", "")
AUTH_FAILURE_TEXT_PATTERNS = (
	"are you signed in",
	"sign in",
	"signed in",
	"log in",
	"logged in",
)
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0"

os.environ.setdefault("GEMINI_COOKIE_PATH", GEMINI_COOKIE_PATH)


def normalize_custom_model_specs(raw_config) -> List[Dict[str, Dict[str, str]]]:
	"""Normalize custom model config from dict/list YAML or JSON."""
	if not raw_config:
		return []

	if isinstance(raw_config, dict):
		raw_config = raw_config.get("models", [raw_config])

	if not isinstance(raw_config, list):
		raise ValueError("Custom model config must be a list or a dict containing a 'models' list")

	normalized_models = []
	for idx, item in enumerate(raw_config, start=1):
		if not isinstance(item, dict):
			raise ValueError(f"Custom model entry #{idx} must be a mapping")

		model_name = str(item.get("model_name", "")).strip()
		model_header = item.get("model_header")
		if not model_name:
			raise ValueError(f"Custom model entry #{idx} is missing model_name")
		if not isinstance(model_header, dict):
			raise ValueError(f"Custom model '{model_name}' must define model_header as a mapping")

		normalized_models.append(
			{
				"model_name": model_name,
				"model_header": {str(key): str(value) for key, value in model_header.items()},
			}
		)

	return normalized_models


def load_custom_models() -> Dict[str, Dict[str, Dict[str, str]]]:
	"""Load custom model overrides from file/env on demand."""
	registry = {}
	custom_models_file = os.environ.get("CUSTOM_MODELS_FILE", "").strip()
	custom_models = os.environ.get("CUSTOM_MODELS", "").strip()

	if custom_models_file:
		config_path = Path(custom_models_file)
		if not config_path.is_absolute():
			config_path = Path(__file__).parent / config_path
		if not config_path.exists():
			raise ValueError(f"CUSTOM_MODELS_FILE does not exist: {config_path}")
		file_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
		for model in normalize_custom_model_specs(file_config):
			registry[model["model_name"].lower()] = model

	if custom_models:
		inline_config = yaml.safe_load(custom_models)
		for model in normalize_custom_model_specs(inline_config):
			registry[model["model_name"].lower()] = model

	return registry


def get_custom_model_registry() -> Dict[str, Dict[str, Dict[str, str]]]:
	"""Read the latest custom model config for each request."""
	return load_custom_models()


async def background_delete_chat(client: GeminiClient, cid: str):
	"""Deletes a chat conversation in the background to avoid blocking the main thread."""
	if not cid:
		return
	try:
		await client.delete_chat(cid)
	except Exception as e:
		logger.error(f"Failed to auto-delete chat {cid}: {e}")


def response_indicates_auth_failure(text: str) -> bool:
	"""Return True if the response text looks like a signed-out or degraded session."""
	normalized = (text or "").strip().lower()
	if not normalized:
		return True
	return any(pattern in normalized for pattern in AUTH_FAILURE_TEXT_PATTERNS)


async def fetch_readable_chat_response(client: GeminiClient, cid: str, retry_delays: List[int]) -> Optional[object]:
	"""Poll Gemini history until the chat becomes readable or retries are exhausted."""
	for attempt, delay in enumerate(retry_delays, start=1):
		try:
			if delay:
				await asyncio.sleep(delay)

			recovered = await client.fetch_latest_chat_response(cid)
			if recovered and getattr(recovered, "text", ""):
				return recovered
		except Exception as e:
			logger.warning("Gemini history read failed (retry %s/%s): %s", attempt, len(retry_delays), e)
			continue

	return None


async def background_verify_chat_persistence(client: GeminiClient, cid: str, source: str):
	"""Best-effort verification that a returned cid is readable from Gemini history."""
	if not cid:
		return

	retry_delays = [1, 3, 8]
	recovered = await fetch_readable_chat_response(client, cid, retry_delays)
	if recovered:
		logger.debug(
			"Gemini history verification succeeded: source=%s cid=%s text_len=%s metadata=%s",
			source,
			cid,
			len(recovered.text),
			getattr(recovered, "metadata", None),
		)
		return

	logger.warning(
		"Gemini history verification exhausted retries for cid=%s source=%s",
		cid,
		source,
	)


async def validate_gemini_client_session(client: GeminiClient, source: str):
	"""Verify that an initialized client can create and read back a normal persistent Gemini chat."""
	validation_cid = None
	try:
		response = await client.generate_content(SESSION_VALIDATION_PROMPT, temporary=False)
		response_text = getattr(response, "text", "") or ""
		metadata = getattr(response, "metadata", None) or []
		validation_cid = metadata[0] if metadata else None

		if response_indicates_auth_failure(response_text):
			raise ValueError("validation probe returned signed-out or empty content")

		if not validation_cid:
			raise ValueError("validation probe returned no persistent chat metadata")

		recovered = await fetch_readable_chat_response(client, validation_cid, [1, 3, 8])
		if not recovered or response_indicates_auth_failure(getattr(recovered, "text", "") or ""):
			raise ValueError("validation probe chat was not readable from Gemini history")

		logger.info("Gemini session validation succeeded using %s credentials", source)
	finally:
		if validation_cid:
			try:
				await client.delete_chat(validation_cid)
			except Exception:
				logger.debug("Failed to delete Gemini validation chat %s", validation_cid)


def load_or_generate_secret() -> str:
	"""
	Load the signature secret from file, or generate a new one if not found.
	"""
	if os.path.exists(SECRET_FILE_PATH):
		try:
			with open(SECRET_FILE_PATH, "r") as f:
				secret = f.read().strip()
				if secret:
					logger.info(f"Loaded proxy secret from {SECRET_FILE_PATH}")
					return secret
		except Exception as e:
			logger.warning(f"Failed to read secret file, trying to generate a new one: {e}")

	# Generate new secret if not found or error occurred
	new_secret = secrets.token_hex(32)
	try:
		# Ensure directory exists
		os.makedirs(os.path.dirname(SECRET_FILE_PATH), exist_ok=True)
		with open(SECRET_FILE_PATH, "w") as f:
			f.write(new_secret)

		# Set restrictive permissions (user-only readable/writable)
		try:
			os.chmod(SECRET_FILE_PATH, 0o600)
		except Exception as e:
			logger.warning(f"Failed to set restrictive permissions on {SECRET_FILE_PATH}: {e}")

		logger.info(f"Generated new proxy secret and saved to {SECRET_FILE_PATH}")
		return new_secret
	except Exception as e:
		logger.error(f"Error writing secret file: {e}")
		# if unable to save, return an in-memory ephemeral secret instead of using API_KEY or SECURE_1PSID
		ephemeral_secret = secrets.token_urlsafe(32)
		logger.warning("Using an in-memory secret to proxy images for this session.")
		return ephemeral_secret


SIGNATURE_SECRET = load_or_generate_secret()

# Watermark removal constants
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
ALPHA_MAP_CACHE = {}


def get_alpha_map(size: int) -> np.ndarray:
	"""Load and cache the alpha map from the background capture image."""
	if size in ALPHA_MAP_CACHE:
		return ALPHA_MAP_CACHE[size]

	bg_path = os.path.join(ASSETS_DIR, f"bg_{size}.png")
	if not os.path.exists(bg_path):
		logger.warning(f"Watermark asset not found: {bg_path}")
		return None

	try:
		with Image.open(bg_path) as img:
			img_data = np.array(img.convert("RGB"))
			alpha_map = np.max(img_data, axis=2) / 255.0
			ALPHA_MAP_CACHE[size] = alpha_map
			return alpha_map
	except Exception as e:
		logger.error(f"Error loading alpha map {size}: {e}")
		return None


def remove_gemini_watermark(image_bytes: bytes) -> bytes:
	"""Remove Gemini watermark using Reverse Alpha Blending."""
	try:
		with Image.open(io.BytesIO(image_bytes)) as img:
			width, height = img.size
			orig_format = img.format

			if width > 1024 and height > 1024:
				logo_size, margin = 96, 64
			else:
				logo_size, margin = 48, 32

			alpha_map = get_alpha_map(logo_size)
			if alpha_map is None:
				return image_bytes

			x = width - margin - logo_size
			y = height - margin - logo_size
			if x < 0 or y < 0:
				logger.warning(f"Image too small for watermark removal: {width}x{height}")
				return image_bytes

			# Reverse Alpha Blending: original = (watermarked - α × 255) / (1 - α)
			img_array = np.array(img.convert("RGB")).astype(np.float64)
			roi = img_array[y : y + logo_size, x : x + logo_size].copy()

			alpha = np.clip(alpha_map, 0.002, 0.99)
			alpha_expanded = np.expand_dims(alpha, axis=2)
			cleaned_roi = (roi - alpha_expanded * 255.0) / (1.0 - alpha_expanded)
			cleaned_roi = np.clip(np.round(cleaned_roi), 0, 255).astype(np.uint8)

			img_array_uint8 = np.array(img.convert("RGB"))
			img_array_uint8[y : y + logo_size, x : x + logo_size] = cleaned_roi

			out_io = io.BytesIO()
			save_format = orig_format or "PNG"
			if save_format.upper() == "JPEG":
				Image.fromarray(img_array_uint8).save(out_io, format="JPEG", quality=95)
			else:
				Image.fromarray(img_array_uint8).save(out_io, format=save_format)
			return out_io.getvalue()

	except Exception as e:
		logger.error(f"Error removing watermark: {e}")
		return image_bytes


if not SECURE_1PSID or not SECURE_1PSIDTS:
	logger.warning("Gemini credentials are missing; set SECURE_1PSID and SECURE_1PSIDTS before serving requests.")
else:
	logger.info(
		"Startup config: thinking=%s temporary_chat=%s auto_delete_chat=%s public_base_url=%s gemini_webapi=%s",
		ENABLE_THINKING,
		TEMPORARY_CHAT,
		AUTO_DELETE_CHAT,
		bool(PUBLIC_BASE_URL),
		get_gemini_webapi_version(),
	)
	if not re.match("^[\\w\\-\\.]+$", SECURE_1PSID):
		logger.warning(
			"SECURE_1PSID contains characters outside the safe cache filename pattern. This may be valid for auth, but cached 1PSIDTS lookup will fall back to the env value."
		)

if not API_KEY:
	logger.info("API key authentication is disabled.")
else:
	logger.info("API key authentication is enabled.")


def correct_markdown(md_text: str) -> str:
	"""
	修正Markdown文本，移除Google搜索链接包装器，并根据显示文本简化目标URL。
	"""

	def simplify_link_target(text_content: str) -> str:
		match_colon_num = re.match(r"([^:]+:\d+)", text_content)
		if match_colon_num:
			return match_colon_num.group(1)
		return text_content

	def replacer(match: re.Match) -> str:
		outer_open_paren = match.group(1)
		display_text = match.group(2)

		new_target_url = simplify_link_target(display_text)
		new_link_segment = f"[`{display_text}`]({new_target_url})"

		if outer_open_paren:
			return f"{outer_open_paren}{new_link_segment})"
		else:
			return new_link_segment

	pattern = r"(\()?\[`([^`]+?)`\]\((https://www.google.com/search\?q=)(.*?)(?<!\\)\)\)*(\))?"

	fixed_google_links = re.sub(pattern, replacer, md_text)
	# fix wrapped markdownlink
	pattern = r"`(\[[^\]]+\]\([^\)]+\))`"
	return re.sub(pattern, r"\1", fixed_google_links)


# Pydantic models for API requests and responses
class ContentItem(BaseModel):
	type: str
	text: Optional[str] = None
	image_url: Optional[Dict[str, str]] = None


class Message(BaseModel):
	role: str
	content: Union[str, List[ContentItem], None] = None
	name: Optional[str] = None
	tool_calls: Optional[List[Dict[str, Any]]] = None
	tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
	model: str
	messages: List[Message]
	temperature: Optional[float] = 0.7
	top_p: Optional[float] = 1.0
	n: Optional[int] = 1
	stream: Optional[bool] = False
	max_tokens: Optional[int] = None
	presence_penalty: Optional[float] = 0
	frequency_penalty: Optional[float] = 0
	user: Optional[str] = None
	tools: Optional[List[Dict[str, Any]]] = None
	tool_choice: Optional[Union[str, Dict[str, Any]]] = None
	parallel_tool_calls: Optional[bool] = True
	# F9: json mode —— {"type":"json_object"} 时对最终文本做 JSON 提取兜底
	response_format: Optional[Dict[str, Any]] = None
	# Step5 按需思考: 客户端按请求指定是否开启思考
	thinking: Optional[bool] = None
	reasoning_effort: Optional[str] = None


def _effective_thinking(req: "ChatCompletionRequest") -> bool:
	"""按需 thinking 判定: 显式 thinking 优先 → reasoning_effort 映射 → 全局开关。
	reasoning_effort: high/medium → 开;low/none/off → 关。"""
	if req.thinking is not None:
		return bool(req.thinking)
	effort = (req.reasoning_effort or "").lower()
	if effort:
		return effort in ("high", "medium", "full")
	return ENABLE_THINKING


class Choice(BaseModel):
	index: int
	message: Message
	finish_reason: str


class Usage(BaseModel):
	prompt_tokens: int
	completion_tokens: int
	total_tokens: int


class ChatCompletionResponse(BaseModel):
	id: str
	object: str = "chat.completion"
	created: int
	model: str
	choices: List[Choice]
	usage: Usage


class ModelData(BaseModel):
	id: str
	object: str = "model"
	created: int
	owned_by: str = "google"


class ModelList(BaseModel):
	object: str = "list"
	data: List[ModelData]


# Authentication dependency
async def verify_api_key(authorization: str = Header(None)):
	"""
	Verify the API key extracted from the Authorization header.
	Also accepts valid admin session tokens for the quick test chat.
	"""
	if not API_KEY:
		# If API_KEY is not set in environment, skip validation (for development)
		return

	if not authorization:
		raise HTTPException(status_code=401, detail="Missing Authorization header")

	# Accept valid admin session tokens (for admin panel quick test)
	if authorization.startswith("Bearer "):
		token = authorization[7:]
		from admin import _admin_sessions, _clean_expired_sessions
		_clean_expired_sessions()
		if token in _admin_sessions:
			return

	try:
		scheme, token = authorization.split()
		if scheme.lower() != "bearer":
			raise HTTPException(
				status_code=401,
				detail="Invalid authentication scheme. Use Bearer token",
			)

		if token != API_KEY:
			raise HTTPException(status_code=401, detail="Invalid API key")
	except ValueError:
		raise HTTPException(
			status_code=401,
			detail="Invalid authorization format. Use 'Bearer YOUR_API_KEY'",
		)

	return token


# 媒体生成服务(生图/生视频/生音乐): 复用 API_KEY 鉴权,挂载于鉴权依赖定义之后
import media_api as _media_api
app.include_router(_media_api.router)


# ============ OpenAI 风格错误契约(供 Agent 客户端按语义分类,而非裸 500) ============
# DSH 等客户端依据 error.code/message 文本做错误分类:
#   - 含 "context_length_exceeded"/"context length exceeded" → CONTEXT_WINDOW_EXCEEDED
#     → 触发客户端侧自动压缩并重试(而不是当 SERVER 错误傻重试 5 次)
#   - FastAPI HTTPException 默认返回 {"detail": ...} 形状,OpenAI SDK 解析不到
#     error.message,因此统一用 JSONResponse 输出 OpenAI 兼容形状 {"error": {...}}

# Gemini web 端单次请求 f.req 解码字符数上限(实测标定: ≤1,000,494 成功 / ≥1,020,494 拒绝)
# 预检阈值取 980K 字符(约 245K tokens),紧贴硬件墙,多出 30K 宝贵上下文空间
GEMINI_REQ_CHAR_LIMIT = 980_000
CTX_CHECK_THRESHOLD = int(os.environ.get("GEMINI_CTX_CHECK_THRESHOLD", "980000"))


def openai_error(status_code: int, message: str, err_type: str, code: str = None, retry_after: int = None):
	"""构造 OpenAI API 兼容的错误 JSONResponse。"""
	headers = {}
	if retry_after is not None:
		headers["Retry-After"] = str(retry_after)
	payload = {"error": {"message": message, "type": err_type}}
	if code:
		payload["error"]["code"] = code
	return JSONResponse(status_code=status_code, content=payload, headers=headers)


def is_context_overflow_error(e: Exception, prompt_char_len: int = None) -> bool:
	"""识别 Gemini 上游的上下文超限类失败。
	覆盖两种现场:
	1. 运行时 e=4 错误帧:Google 直接关闭流,库内表现为
	   APIError("The original request may have been silently aborted by Google."),
	   且因 CID 未分配重连后依然复现(重试无意义)。
	2. 兜底文本匹配(防库版本变更文案)。
	prompt_char_len 提供时做尺寸闸门:小请求(远低于红线)的偶发 aborted
	属于其他瞬时故障,不应误判为超限而触发客户端压缩。"""
	if isinstance(e, HTTPException):
		return False
	text = str(e)
	matched = "silently aborted" in text
	if not matched:
		lowered = text.lower()
		matched = ("context length" in lowered or "context window" in lowered) and (
			"exceed" in lowered or "too long" in lowered or "too large" in lowered
		)
	if not matched:
		return False
	if prompt_char_len is not None and prompt_char_len < CTX_CHECK_THRESHOLD * 0.6:
		return False
	return True


def context_overflow_response(char_len: int, after_degrade: bool = False):
	"""OpenAI 风格 400 + context_length_exceeded。
	消息文案刻意命中 DSH 客户端的超限正则(STRUCTURED_CONTEXT_OVERFLOW /
	"maximum context length"),使其归类为 CONTEXT_WINDOW_EXCEEDED 并走
	"压缩后重试"的自动恢复路径,而非 SERVER 类盲目重试。
	after_degrade=True 表示已裁剪过历史仍超限 → 提示需缩减消息本身(如巨型工具结果)。"""
	hint = (" Even after trimming history, the latest message itself is too large - "
			"reduce or split the content of the newest message." if after_degrade else "")
	return openai_error(
		400,
		f"This model's maximum context length is {GEMINI_REQ_CHAR_LIMIT} characters. "
		f"However, your request is {char_len} characters long. "
		f"Please reduce the length of the messages and try again.{hint} (context_length_exceeded)",
		"invalid_request_error",
		code="context_length_exceeded",
	)


@app.exception_handler(HTTPException)
async def http_exception_openai_shape(request: Request, exc: HTTPException):
	"""把所有 HTTPException 渲染为 OpenAI 兼容形状,并保留 detail 兼容旧客户端。
	同时透传 Retry-After 头。"""
	headers = dict(exc.headers) if exc.headers else {}
	detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
	err_type = "invalid_request_error" if exc.status_code < 500 else "server_error"
	return JSONResponse(
		status_code=exc.status_code,
		content={
			"error": {"message": detail, "type": err_type},
			"detail": detail,
		},
		headers=headers,
	)


# Simple error handler middleware
@app.middleware("http")
async def error_handling(request: Request, call_next):
	"""
	Global middleware to catch unhandled exceptions, log the error,
	and return a standardized HTTP 500 response.
	"""
	try:
		return await call_next(request)
	except Exception:
		logger.exception("Request failed")
		return JSONResponse(
			status_code=500,
			content={
				"error": {
					"message": "Internal server error",
					"type": "internal_server_error",
				}
			},
		)


# Get list of available models
# 基础模型(plus/advanced 为同模型档位变体,隐藏)
BASE_MODELS = ("gemini-pro", "gemini-flash", "gemini-flash-lite")


@app.get("/v1/models")
async def list_models():
	"""返回可用模型列表:仅暴露 3 个基础模型(plus/advanced 为档位变体,隐藏但仍可用)"""
	global gemini_client
	try:
		gemini_client = await get_gemini_client()
	except Exception:
		pass  # 客户端未就绪时仍返回基础列表
	now = int(datetime.now(tz=timezone.utc).timestamp())
	data = []
	seen = set()
	custom_model_registry = get_custom_model_registry()

	for custom_model in custom_model_registry.values():
		model_name = custom_model["model_name"]
		seen.add(model_name.lower())
		data.append({"id": model_name, "object": "model", "created": now, "owned_by": "google-gemini-web"})

	if DISABLE_BUILTIN_MODELS:
		return {"object": "list", "data": data}

	# 从账号动态发现真实展示名(Google 升级换代时自动跟随)
	display_names = {}
	try:
		if gemini_client is not None:
			for m in gemini_client.list_models() or []:
				display_names[getattr(m, "model_name", "")] = getattr(m, "display_name", "")
	except Exception:
		pass

	for name in BASE_MODELS:
		if name.lower() in seen:
			continue
		item = {
			"id": name,
			"object": "model",
			"created": now,
			"owned_by": "google-gemini-web",
			"context_length": 245000,
		}
		dn = display_names.get(name)
		desc_parts = []
		if dn:
			desc_parts.append(f"真实模型: {dn}")
		desc_parts.append("最大有效上下文: 245K tokens (980K 字符)")
		item["description"] = " | ".join(desc_parts)
		data.append(item)
	return {"object": "list", "data": data}


# Helper to convert between Gemini and OpenAI model names
def map_model_name(openai_model_name: str) -> Union[Model, Dict[str, Dict[str, str]]]:
	"""根据模型名称字符串查找匹配的 Model 枚举值或自定义模型配置。

	优先命中自定义模型注册表，这样可以覆写 gemini_webapi 内置模型头。
	找不到自定义配置时，再精确匹配 Model 枚举（大小写不敏感）；
	仍找不到则返回第一个非 UNSPECIFIED 模型。
	"""
	normalized = openai_model_name.lower()
	custom_model_registry = get_custom_model_registry()

	if normalized in custom_model_registry:
		return custom_model_registry[normalized]

	# 精确匹配 Model 枚举
	for m in Model:
		model_name = m.model_name if hasattr(m, "model_name") else str(m)
		if normalized == model_name.lower():
			return m

	# 找不到精确匹配，返回第一个非 UNSPECIFIED 的模型
	for m in Model:
		if m is not Model.UNSPECIFIED:
			return m
	return next(iter(Model))


def get_effective_model_debug_info(model: Union[Model, Dict[str, Dict[str, str]]]) -> Dict[str, Dict[str, str] | str]:
	"""Return the resolved model name and headers for request logging."""
	if isinstance(model, dict):
		return {
			"model_name": str(model.get("model_name", "")),
			"model_header": {str(key): str(value) for key, value in model.get("model_header", {}).items()},
		}

	return {
		"model_name": getattr(model, "model_name", str(model)),
		"model_header": {str(key): str(value) for key, value in getattr(model, "model_header", {}).items()},
	}


# Prepare conversation history from OpenAI messages format
def prepare_conversation(messages: List[Message], skip_system: bool = False) -> tuple:
	"""
	Convert a list of OpenAI-formatted message objects into a
	flat string conversation format suitable for the Gemini API.
	Also extracts and saves base64 images to temporary files.

	Args:
		skip_system: 当启用 Gemini Gem 时传 True，跳过 system 消息拼接，
			避免与 Gem 的 system prompt 重复冲突。

	Returns:
		A tuple containing the constructed conversation string and a list of paths to temporary image files.
	"""
	conversation = ""
	temp_files = []
	_last_tc_map: dict = {}  # F8: 上一条 assistant 的 tool_call_id → 工具名(供 tool 结果标注归属)

	for msg in messages:
		# 启用 Gem 时跳过 system 消息（Gem 本身就是 system prompt 通道）
		if skip_system and msg.role == "system":
			continue
		# 工具调用历史:tool 结果 与 assistant 的 tool_calls 都转成文本喂给 Gemini
		if msg.role == "tool":
			# F8: 并行多调用时按 tool_call_id 回查工具名,结果与调用一一对应(不再靠顺序隐式关联)
			_tcid = getattr(msg, "tool_call_id", "") or ""
			_label = f"对应调用 {_tcid} → {_last_tc_map.get(_tcid, '未知工具')}" if (_tcid and _last_tc_map) else ""
			conversation += tools_shim.serialize_tool_result(msg, label=_label) + "\n\n"
			continue
		if msg.role == "assistant" and getattr(msg, "tool_calls", None):
			# 记录本轮 tool_call_id → 工具名映射,供紧随其后的 tool 结果标注归属
			_last_tc_map = {}
			for _tc in (msg.tool_calls or []):
				if isinstance(_tc, dict):
					_last_tc_map[_tc.get("id") or ""] = (_tc.get("function") or {}).get("name", "?")
			conversation += "Assistant: " + tools_shim.serialize_assistant_tool_calls(msg) + "\n\n"
			continue
		if isinstance(msg.content, str):
			# String content handling
			if msg.role == "system":
				conversation += f"System: {msg.content}\n\n"
			elif msg.role == "user":
				conversation += f"Human: {msg.content}\n\n"
			elif msg.role == "assistant":
				conversation += f"Assistant: {msg.content}\n\n"
		else:
			# Mixed content handling
			if msg.role == "user":
				conversation += "Human: "
			elif msg.role == "system":
				conversation += "System: "
			elif msg.role == "assistant":
				conversation += "Assistant: "

			for item in msg.content:
				if item.type == "text":
					conversation += item.text or ""
				elif item.type == "image_url" and item.image_url:
					# Handle image
					image_url = item.image_url.get("url", "")
					if image_url.startswith("data:image/"):
						# Process base64 encoded image
						try:
							# Extract the base64 part
							base64_data = image_url.split(",")[1]
							image_data = base64.b64decode(base64_data)

							# Create temporary file to hold the image
							with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
								tmp.write(image_data)
								temp_files.append(tmp.name)
						except Exception as e:
							logger.error(f"Error processing base64 image: {str(e)}")

			conversation += "\n\n"

	# Add a final prompt for the assistant to respond to
	conversation += "Assistant: "

	return conversation, temp_files


# Dependency to get the initialized Gemini client
# ──────────────────────────────── 多账号池(指纹系统) ────────────────────────────────
# 每个指纹账号窗口登录 Google 后,其 cookie 进入账号池;服务端自动选择有额度的账号。
POOL_SCAN_INTERVAL = 30.0
POOL_QUOTA_MIN = 150          # 剩余 credits 低于此值视为额度不足,触发切换
_pool_last_scan = 0.0
_pool: dict[str, dict] = {}
_active_pool_pid: str = ""


def pool_snapshot() -> dict:
	"""供 admin API / UI 查询账号池状态(含健康分/熔断状态)"""
	open_count = 0
	accounts = []
	for pid, st in _pool.items():
		_circuit = _circuit_label(st)
		if _circuit != "closed":
			open_count += 1
		accounts.append({
			"pid": pid,
			"status": st.get("status"),
			"quota": st.get("quota"),
			"mode": st.get("mode", "unknown"),
			"active": pid == _active_pool_pid,
			"last_error": st.get("last_error", "")[:150],
			"stall_count": st.get("stall_count", 0),
			"last_stall_at": st.get("last_stall_at", ""),
			"health_score": _account_health_score(st),
			"circuit": _circuit,
			"err_streak": st.get("_err_streak", 0),
			"last_ttfb": st.get("_last_ttfb"),
			"isolated": bool(st.get("isolated")),
			"ttfb_history": list(st.get("_ttfb_history") or ()),  # [{"t":ts,"v":s},...]
			"proxy": _account_proxy_url(st),
			"proxy_health": {k: v for k, v in _probe_state(_account_proxy_url(st)).items()} if st.get("proxy") else None,
		})
	return {
		"accounts": accounts,
		"active_pid": _active_pool_pid,
		"circuit_open": open_count,
		"patrol": {
			"last_run": _patrol_last_run,
			"interval": PATROL_INTERVAL,
			"probe_fail_limit": PATROL_PROBE_FAIL_LIMIT,
			"suspects": dict(_patrol_suspects),
		},
		"proxy_pool": _proxy_health_snapshot(),
		"affinity": {
			"active": len(_affinity_map),
			"max": _AFFINITY_MAX,
			"ttl": _AFFINITY_TTL,
		},
		"alerts": _alerts_snapshot(),
		"refreshing": _BALANCE_REFRESH.get("running", False),
		"refresh_progress": {"done": _BALANCE_REFRESH.get("done", 0), "total": _BALANCE_REFRESH.get("total", 0)},
		"refresh_summary": (f"成功 {len(_BALANCE_REFRESH['ok'])} · 失败 {len(_BALANCE_REFRESH['fail'])}"
							if not _BALANCE_REFRESH.get("running") and _BALANCE_REFRESH.get("total") else ""),
	}


def _pool_scan():
	"""扫描指纹账号目录,同步 cookie 到账号池(每 POOL_SCAN_INTERVAL 秒一次,轻量)。"""
	global _pool, _pool_last_scan, _active_pool_pid, COOKIE_PUSH_TIME
	now = time.time()
	if now - _pool_last_scan < POOL_SCAN_INTERVAL:
		return
	_pool_last_scan = now
	try:
		import fingerprint.account_cookies as ac
		from fingerprint import profiles as fp_profiles
		found = {}
		for p in fp_profiles.list_all():
			pid = p["id"]
			g = ac.read_gemini_cookies(pid)
			if not g:
				continue
			old = _pool.get(pid)
			if old and old.get("status") == "ok" and old.get("psid") == g["psid"]:
				# 同一账号:保留现有 client(含 auto_refresh 任务)。
				# psidts 会被 gemini_webapi 的 auto_refresh 周期性旋转并写回文件,
				# 若因此重建池会丢弃正在使用的 client → 触发"额度不足"死锁(1.0 线上 500 根因)。
				# 仅当 psid(账号本体)变化才视为换号重建。
				old["psidts"] = g["psidts"]
				found[pid] = old
				continue
			found[pid] = {"pid": pid, "psid": g["psid"], "psidts": g["psidts"],
						  "proxy": ac.account_proxy(pid), "client": None,
						  "quota": None, "status": "pending", "last_error": "", "mode": "unknown",
						  "stall_count": 0, "last_stall_at": ""}
		# 空结果守卫: 枚举/读 cookie 瞬时失败会让 found 为空;
		# 若直接覆盖会把健康池清空 → 请求中途丢号(间歇 503 / 健康记录落空)。
		if not found and _pool:
			logger.warning("[账号池] 本次扫描结果为空(疑似瞬时枚举失败),保留现有 %d 个账号,5s 后重扫",
						   len(_pool))
			_pool_last_scan = now - POOL_SCAN_INTERVAL + 5
			return
		_pool = found
		# 健康监控字段补齐(新条目与留存条目统一初始化)
		for _st in _pool.values():
			_health_init(_st)
		# 启动即可用:检测到有窗口 cookie(自动推送)即记录一次推送时间
		if found:
			COOKIE_PUSH_TIME = time.strftime("%Y-%m-%d %H:%M:%S")
		if _active_pool_pid and _active_pool_pid not in _pool:
			_active_pool_pid = ""
	except Exception as e:
		logger.warning(f"账号池扫描失败: {e}")


async def _init_pool_account(st: dict, light: bool = False):
	"""为账号初始化 GeminiClient(带账号独立代理),并抓取额度。成功 → status=ok。
	light=True(启动路径): init 的 8 个 RPC 已含用户状态/用量/配额/滥用校验,
	跳过额外的生成探针(validate_gemini_client_session) → 每账号省 5~15s;
	首次业务请求若凭据真失效,会走既有 mark_client_sick/换号兜底。"""
	# 并发互斥: 并行初始化/多请求同时触发时,同账号只建一次连接
	lock = _account_init_locks.setdefault(st["pid"], asyncio.Lock())
	async with lock:
		if st.get("status") == "ok" and st.get("client") is not None:
			return st["client"]  # 已被并发任务初始化完成,直接复用
		return await _init_pool_account_inner(st, light)


async def _init_pool_account_inner(st: dict, light: bool = False):
	try:
		client = GeminiClient(st["psid"], st["psidts"], proxy=_norm_proxy(st.get("proxy")) or None)
		_t0 = time.time()
		# 上游到 google 会不时(fly 数据中心 IP 被风控),初始化重试
		# timeout=90(原 300: 启动等一个死账号 5 分钟毫无意义),重试 2 次
		_last = None
		for _try in range(2):
			try:
				await client.init(timeout=90, auto_refresh=False, watchdog_timeout=150)
				_last = None
				break
			except Exception as e:
				_last = e
				import asyncio as _aio
				await _aio.sleep(2)
		if _last:
			raise _last
		# init 短超时仅用于启动检查;生成看门狗恢复常态(否则影响后续业务请求)
		client.timeout = 300
		if not light:
			await validate_gemini_client_session(client, f"pool:{st['pid']}")
		st["client"] = client
		st["status"] = "ok"
		st["mode"] = "proxy" if st.get("proxy") else "direct"
		st["last_error"] = ""
		q = getattr(client, "quotas", None) or {}
		usage = q.get("usage_info", {}) or {}
		daily = usage.get("daily") or usage.get("current_5h") or usage
		st["quota"] = {
			"remaining": daily.get("remaining_credits"),
			"pct": daily.get("usage_percentage"),
			"reset_at": daily.get("reset_at", ""),
		}
		logger.info(f"账号 {st['pid']} 初始化成功(耗时 {time.time() - _t0:.1f}s, 剩余 {st['quota'].get('remaining')})")
		# 统一双客户端系统: 池 client 空登记进请求级池(_account_clients),
		# 首个业务请求 acquire_client 直接复用热会话 —— 否则会再完整 init 一遍(10~30s)
		if _account_clients.get(st["pid"]) is None:
			_account_clients[st["pid"]] = {"client": client, "psid": st["psid"], "psidts": st["psidts"]}
		_init_alerted[st["pid"]] = False  # 初始化成功,解除告警去重
		return client
	except Exception as e:
		st["status"] = "expired"
		st["last_error"] = str(e)[:200]
		if st.get("client"):
			try:
				await st["client"].close()
			except Exception:
				pass
		st["client"] = None
		logger.warning(f"账号 {st['pid']} 初始化失败: {e}")
		# 初始化失败告警(去重: 同一账号只在状态转为 expired 时提示一次)
		if not _init_alerted.get(st["pid"]):
			_init_alerted[st["pid"]] = True
			_push_alert(f"账号 {st['pid']} 初始化失败: {str(e)[:70]}", "ERROR", st["pid"])
		return None


def _account_has_quota(st: dict) -> bool:
	q = st.get("quota") or {}
	rem = q.get("remaining")
	if rem is None:
		return st.get("status") == "ok"
	return rem > POOL_QUOTA_MIN


def _pick_ready_healthiest(exclude: str = "") -> str:
	"""就绪(ok+已初始化)且未熔断、有额度、未隔离的账号中按健康分最高选择。不做初始化。"""
	best = None
	for pid, st in _pool.items():
		if pid == exclude or st.get("status") != "ok" or st.get("client") is None:
			continue
		if st.get("isolated") or _circuit_is_open(st) or not _account_has_quota(st):
			continue
		if _proxy_down(_account_proxy_url(st)):  # 代理判死 → 该代理下账号全部路由排除
			continue
		score = _account_health_score(st)
		if best is None or score > best[1]:
			best = (pid, score)
	return best[0] if best else ""


def _ready_candidates(exclude: str = ""):
	"""就绪候选列表 [(pid, health_score), ...];隔离/熔断/无配额/未初始化/代理判死者排除。"""
	out = []
	for pid, st in _pool.items():
		if pid == exclude or st.get("status") != "ok" or st.get("client") is None:
			continue
		if st.get("isolated") or _circuit_is_open(st) or not _account_has_quota(st):
			continue
		if _proxy_down(_account_proxy_url(st)):  # 代理判死 → 路由排除
			continue
		out.append((pid, _account_health_score(st)))
	return out


def _pick_ready_soft(exclude: str = "") -> str:
	"""软负载均衡: 就绪账号中按健康分【加权随机】选择 —— 新任务散到各健康账号,
	避免所有新任务都塞给"当前最高分"账号把它打挂。与会话粘性互补:
	首次分配用此函数,同任务后续由 _affinity_pick 钉住不动(缓存保留)。"""
	cands = _ready_candidates(exclude)
	if not cands:
		return ""
	if len(cands) == 1:
		return cands[0][0]
	# 权重 = 健康分(下限 5,避免负/零权重),高分账号被选概率更高但仍可能选到次高 → 分摊流量
	total = 0.0
	weights = []
	for pid, sc in cands:
		w = max(5.0, float(sc))
		weights.append((pid, w))
		total += w
	r = secrets.SystemRandom().random() * total
	acc = 0.0
	for pid, w in weights:
		acc += w
		if r <= acc:
			return pid
	return weights[-1][0]  # 兜底(浮点边界)


async def _pick_pool_account(exclude: str = "") -> str:
	"""健康路由: 就绪账号中选健康分最高者(熔断拦截期/额度不足者排除);
	无就绪 → 惰性初始化未试账号;仍无 → 熔断退避已到期的账号作半开探针放行。"""
	pid = _pick_ready_healthiest(exclude)
	if pid:
		return pid
	for pid, st in _pool.items():
		if pid == exclude or st.get("status") == "expired" or st.get("client") is not None:
			continue
		if st.get("isolated"):
			continue
		# 代理判死 → 不做惰性初始化(必失败且白费 90s 超时)
		_purl = _account_proxy_url(st)
		if _purl and _proxy_down(_purl):
			continue
		await _init_pool_account(st)
		if st.get("status") == "ok" and _account_has_quota(st):
			return pid
		_log_bridge(f"[账号池] 账号 {pid} 初始化未成功(status={st.get('status')}, err={st.get('last_error','')[:80]})", "WARNING")
	# 探针回退: 全池健康可用账号为空,但存在熔断退避到期账号 → 放行健康分最高者(半开探测)
	probe_best = None
	for pid, st in _pool.items():
		if pid == exclude or st.get("status") != "ok" or st.get("client") is None:
			continue
		if st.get("isolated") or _circuit_label(st) != "half_open":
			continue
		score = _account_health_score(st)
		if probe_best is None or score > probe_best[1]:
			probe_best = (pid, score)
	if probe_best:
		_log_bridge(f"[账号池] 无健康可用账号,放行 {probe_best[0]} 作半开探针(熔断退避已到期)", "WARNING")
		return probe_best[0]
	return ""


async def _get_pool_client():
	"""多账号池模式:返回当前可用账号的 client,额度不足自动切换(AUTO_SWITCH_COOKIE 控制)。"""
	global gemini_client, _init_failure_ts, CLIENT_MODE, _active_pool_pid
	async with gemini_client_lock:
		st = _pool.get(_active_pool_pid)
		if st and st.get("client") is not None and st.get("status") == "ok":
			if _account_has_quota(st):
				gemini_client = st["client"]
				CLIENT_MODE = st.get("mode", "unknown")
				return st["client"]
			if not AUTO_SWITCH_COOKIE:
				# 自动切换关闭:保持当前账号,额度不足直接报错
				_init_failure_ts = time.time()
				raise HTTPException(status_code=503, detail="当前账号额度不足(自动切换 Cookie 已关闭)", headers={"Retry-After": "60"})
		# 需要切换账号(或当前账号需重建)。exclude 当前,仅当存在其他候选;
		# 池里只有当前一个账号时不能 exclude 自己 —— 否则 503 死锁,永不自愈(1.0 线上 500 根因)。
		_has_other = any(pid != _active_pool_pid and st.get("status") != "expired"
						 for pid, st in _pool.items())
		pid = await _pick_pool_account(exclude=_active_pool_pid if _has_other else "")
		if not pid:
			_init_failure_ts = time.time()
			raise HTTPException(
				status_code=503,
				detail="所有账号额度不足或需要重新登录(请在指纹浏览器管理页检查各账号状态)",
				headers={"Retry-After": "60"},
			)
		_active_pool_pid = pid
		st = _pool[pid]
		gemini_client = st["client"]
		CLIENT_MODE = st.get("mode", "unknown")
		logger.info(f"已切换到账号 {pid}(剩余 {st.get('quota', {}).get('remaining')})")
		return st["client"]


def _log_bridge(msg, level="INFO"):
	try:
		import gemini_core as _gc
		_gc.add_log(msg, level)
	except Exception:
		pass


# ───────── 异常告警(Step: 熔断/掉线/配额耗尽 → 看板红点提示) ─────────
_ALERTS = deque(maxlen=50)  # [{"t","level","msg","pid"}]
_ALERT_SEEN = True          # 是否已看过(False=有未读 → 看板红点)
_quota_alerted: dict = {}   # pid -> 是否已就"配额不足"发过告警(恢复后重置)
_init_alerted: dict = {}    # pid -> 是否已就"初始化失败"发过告警(成功后重置)


def _push_alert(msg: str, level: str = "WARNING", pid: str = ""):
	"""记录一条告警(带时间戳),并标记为未读。level: WARNING|ERROR。
	方向一: WARNING+ 告警同时异步派发至外部通道(Webhook/Bark/Server酱/Telegram),
	失败静默 —— 外发永不影响业务链路。"""
	global _ALERT_SEEN
	_ALERTS.append({"t": time.time(), "level": level, "msg": str(msg)[:200], "pid": pid})
	_ALERT_SEEN = False
	_log_bridge(msg, level)
	if level in ("WARNING", "ERROR", "CRITICAL"):
		try:
			import alert_dispatcher as _ad
			_category = f"account:{pid}" if pid else "system"
			_title = f"{pid} 异常告警" if pid else "系统告警"
			try:
				_loop = asyncio.get_running_loop()
			except RuntimeError:
				_loop = None
			if _loop is not None and _loop.is_running():
				_loop.create_task(_ad.send_alert_async(_title, str(msg), level=level, category=_category))
		except Exception:
			pass  # 外部派发绝不影响主链路


def _alerts_snapshot() -> dict:
	"""告警快照: 最近告警列表 + 未读 + 历史统计。"""
	alerts = list(_ALERTS)
	today0 = time.strftime("%Y-%m-%d")
	today = sum(1 for a in alerts if time.strftime("%Y-%m-%d", time.localtime(a["t"])) == today0)
	errors = sum(1 for a in alerts if a["level"] == "ERROR")
	return {
		"unseen": (not _ALERT_SEEN),
		"alerts": alerts[-20:],
		"stats": {"total": len(alerts), "today": today, "errors": errors},
	}


# 余额刷新后台任务状态(供前端轮询)
_BALANCE_REFRESH: dict = {"running": False, "done": 0, "total": 0, "ok": [], "fail": [], "started_at": 0.0}
_balance_refresh_lock = asyncio.Lock()


async def _refresh_one_account_balance(st: dict):
	"""单账号真·刷新余额: 库内 _fetch_quota() 独立 RPC。
	(原实现只读 init 时的内存快照 —— 点了等于没刷,这就是"积分一直不变"的根因)"""
	pid = st["pid"]
	try:
		# 收紧超时: 此前 init 120s + fetch 45s,若账号凭据过期会让 running 挂 165s+(看板"一直刷新中"的根因)
		if st.get("client") is None:
			await asyncio.wait_for(_init_pool_account(st, light=True), timeout=45)
		client = st.get("client")
		if client is None:
			raise RuntimeError("client 初始化失败")
		await asyncio.wait_for(client._fetch_quota(), timeout=30)
		q = getattr(client, "quotas", None) or {}
		usage = q.get("usage_info", {}) or {}
		daily = usage.get("daily") or usage.get("current_5h") or usage
		st["quota"] = {"remaining": daily.get("remaining_credits"),
					   "pct": daily.get("usage_percentage"),
					   "reset_at": daily.get("reset_at", "")}
		# 配额耗尽告警(低于阈值只报一次,恢复后重置)
		_rem = st["quota"].get("remaining")
		if _rem is not None and _rem < POOL_QUOTA_MIN:
			if not _quota_alerted.get(pid):
				_quota_alerted[pid] = True
				_push_alert(f"账号 {pid} 配额不足(剩余 {_rem}),将触发切换/需充值", "WARNING", pid)
		else:
			_quota_alerted[pid] = False
		_BALANCE_REFRESH["ok"].append(pid)
		_log_bridge(f"[刷新余额] 账号 {pid} 已回填: 剩余 {st['quota'].get('remaining')}", "SUCCESS")
	except Exception as e:
		_BALANCE_REFRESH["fail"].append(pid)
		if st.get("client") is None:
			st["quota"] = {"remaining": None, "error": True}
		_log_bridge(f"[刷新余额] 账号 {pid} 失败: {type(e).__name__}: {str(e)[:120]}", "WARNING")
	finally:
		_BALANCE_REFRESH["done"] += 1


async def _refresh_accounts_balance_async():
	"""后台并发抓取所有已登录账号余额:真·RPC 刷新、进度可轮询、汇总日志。"""
	try:
		_pool_scan()
		_log_bridge("[刷新余额] 已点击,开始并发抓取已登录账号…")
		targets = [st for st in _pool.values()
				   if st.get("client") is not None or (st.get("psid") or "") or st.get("logged")]
		if not targets:
			_log_bridge("[刷新余额] 没有发现已登录账号,结束刷新", "WARNING")
			return
		_BALANCE_REFRESH.update({"done": 0, "total": len(targets), "ok": [], "fail": [],
								 "started_at": time.time(), "running": True})
		_log_bridge(f"[刷新余额] 发现 {len(targets)} 个已登录账号,开始并发抓取…")
		await asyncio.gather(*(_refresh_one_account_balance(st) for st in targets), return_exceptions=True)
		ok_n, fail_n = len(_BALANCE_REFRESH["ok"]), len(_BALANCE_REFRESH["fail"])
		_log_bridge(f"[刷新余额] 抓取完成: 成功 {ok_n} 个,失败 {fail_n} 个"
					f"{' 失败账号: ' + ','.join(_BALANCE_REFRESH['fail']) if fail_n else ''}",
					"SUCCESS" if not fail_n else "WARNING")
	except Exception as e:
		_log_bridge(f"[刷新余额] 任务异常: {e}", "WARNING")
	finally:
		_BALANCE_REFRESH["running"] = False


async def refresh_accounts_balance():
	"""触发后台并发刷新并【立即返回】—— 不再阻塞等待 150s。
	此前端点会 sleep 直到刷新结束再返回,导致:
	  1) 桥接请求被拖 150s;
	  2) 返回时 refreshing 已为 False,前端误判「刷新启动失败」(真实问题)。
	现在只投递后台任务,进度由前端轮询 pool_snapshot.refreshing 逐账号更新。"""
	if not _BALANCE_REFRESH["running"]:
		_BALANCE_REFRESH["running"] = True  # 先占位,防止重复触发
		asyncio.create_task(_refresh_accounts_balance_async())
	return {"ok": True, "started": True, "refreshing": _BALANCE_REFRESH["running"],
			"accounts": [{"pid": pid, "quota": _pool.get(pid, {}).get("quota")} for pid in _pool]}


async def get_gemini_client():
	"""
	Get or initialize the global GeminiClient instance.

	多账号池模式:若有指纹账号池,自动选择有额度的账号并支持额度用尽自动切换;
	无池(仅 .env 单账号)时保持原有行为。

	Raises:
		HTTPException: If initialization fails due to invalid parameters or connection issues.
	"""
	global gemini_client, _init_failure_ts, CLIENT_MODE
	# 账号池优先:扫描到指纹账号时走多账号自动切换
	_pool_scan()
	if _pool:
		try:
			return await _get_pool_client()
		except HTTPException:
			raise
		except Exception as e:
			logger.warning(f"账号池取客户端失败,回退单账号: {e}")
	if gemini_client is not None:
		return gemini_client

	# 冷却:若刚失败(<60s),快速报错,不重复触发耗时的重新初始化
	if time.time() - _init_failure_ts < _INIT_RETRY_COOLDOWN:
		raise HTTPException(
			status_code=503,
			detail="Gemini client 初始化刚失败(可能是 cookie 过期),请检查登录态,稍后自动重试",
			headers={"Retry-After": "30"},
		)

	async with gemini_client_lock:
		if gemini_client is not None:
			return gemini_client

		try:
			psid = SECURE_1PSID
			cached_psidts = load_cached_1psidts(psid)
			attempts = []

			if cached_psidts:
				attempts.append(("cache", cached_psidts))
			if SECURE_1PSIDTS:
				attempts.append(("environment", SECURE_1PSIDTS))

			seen_psidts = set()
			new_attempts = []
			for source, psidts in attempts:
				if not psidts or psidts in seen_psidts:
					continue
				seen_psidts.add(psidts)
				new_attempts.append((source, psidts))
			attempts = new_attempts

			if not attempts:
				raise HTTPException(
					status_code=500,
					detail="Missing SECURE_1PSIDTS and no cached rotated 1PSIDTS is available",
				)

			last_error = None
			# 代理尝试序列:有代理时先走代理,失败自动降级直连(免费代理常间歇性拒绝握手)
			proxy_modes = [_norm_proxy(GEMINI_PROXY) or None]
			if GEMINI_PROXY:
				proxy_modes.append(None)  # 直连兜底
			for proxy_mode in proxy_modes:
				if gemini_client is not None:
					break
				direct_flag = " via proxy" if proxy_mode else " direct"
				for source, psidts in attempts:
					tmp_client = None
					try:
						logger.info("Initializing Gemini client using %s credentials%s", source, direct_flag)

						tmp_client = GeminiClient(psid, psidts, proxy=proxy_mode)
						await tmp_client.init(timeout=300, auto_refresh=False, watchdog_timeout=150)
						await validate_gemini_client_session(tmp_client, source)

						gemini_client = tmp_client
						CLIENT_MODE = "direct" if proxy_mode is None and GEMINI_PROXY else ("proxy" if proxy_mode else "direct")
						if proxy_mode is None and GEMINI_PROXY:
							logger.warning("代理不可用,已自动降级直连(服务可用,但家庭 IP 直连有风控风险)")
						break
					except Exception as e:
						last_error = e
						logger.warning(f"Gemini session setup failed using {source} 1PSIDTS{'' if proxy_mode else ' (direct)'}: {e}")
						if tmp_client is not None:
							try:
								await tmp_client.close()
							except Exception:
								pass

			if gemini_client is None:
				_init_failure_ts = time.time()
				raise last_error

		except Exception as e:
			logger.error(f"Failed to initialize Gemini client: {str(e)}")
			raise HTTPException(status_code=500, detail=f"Failed to initialize Gemini client: {str(e)}")
	return gemini_client


# ───────── 请求级客户端池实现 ─────────
def _pool_snapshot_creds() -> tuple[str, str]:
	"""当前账号的 psid+psidts(严格配对!来自同一份窗口 cookie)。
	psid 与 psidts 来自不同会话会导致 UNAUTHENTICATED(1.0/1.2 均踩过)。"""
	try:
		st = _pool.get(_active_pool_pid)
		if st and st.get("psid") and st.get("psidts"):
			return st["psid"], st["psidts"]
		for st2 in _pool.values():
			if st2.get("psid") and st2.get("psidts"):
				return st2["psid"], st2["psidts"]
	except Exception:
		pass
	return SECURE_1PSID, SECURE_1PSIDTS


def st_psid_of(pid: str) -> str:
	st = _pool.get(pid) or {}
	return (st.get("psid") or SECURE_1PSID)


def _fresh_cookie_sources(pid: str) -> list[dict]:
	"""候选凭据源(按 mtime 新→旧):窗口明文导出 → secrets 服务端缓存 → 池快照 → .env。
	psid+psidts 严格同源配对,绝不混配。"""
	out = []
	seen = set()
	try:
		wf = Path(__file__).resolve().parent / "fingerprint" / "storage" / pid / "gemini_cookies.json"
		if wf.exists():
			j = json.loads(wf.read_text(encoding="utf-8"))
			if j.get("psid") and j.get("psidts") and j["psid"] not in seen:
				out.append({"psid": j["psid"], "psidts": j["psidts"], "mtime": wf.stat().st_mtime, "src": "window"})
				seen.add(j["psid"])
	except Exception:
		pass
	try:
		psid0 = st_psid_of(pid)
		cache_dir = Path(__file__).resolve().parent / os.environ.get("GEMINI_COOKIE_PATH", "secrets")
		for f in cache_dir.glob(".cached_cookies_" + psid0 + "*.json"):
			try:
				j = json.loads(f.read_text(encoding="utf-8"))
				p = next((c["value"] for c in j if c["name"] == "__Secure-1PSID"), None)
				t = next((c["value"] for c in j if c["name"] == "__Secure-1PSIDTS"), None)
				if p and t and p not in seen:
					out.append({"psid": p, "psidts": t, "mtime": f.stat().st_mtime, "src": "cache"})
					seen.add(p)
			except Exception:
				continue
	except Exception:
		pass
	st = _pool.get(pid) or {}
	if st.get("psid") and st["psid"] not in seen:
		out.append({"psid": st["psid"], "psidts": st.get("psidts") or "", "mtime": 0, "src": "pool"})
		seen.add(st["psid"])
	if SECURE_1PSID and SECURE_1PSID not in seen:
		out.append({"psid": SECURE_1PSID, "psidts": SECURE_1PSIDTS, "mtime": 0, "src": "env"})
	out.sort(key=lambda x: x["mtime"], reverse=True)
	return out


async def _get_or_rebuild_account_client(pid: str, st: dict) -> object:
	"""取该账号的单例 client;凭据已变化(psidts 旋转)或会话已死 → 热重建(串行)。
	重建按 mtime 新→旧尝试多份同源配对凭据,全部失败抛最后一个错。"""
	if pid not in _account_build_locks:
		_account_build_locks[pid] = asyncio.Lock()
	entry = _account_clients.get(pid)
	creds_changed = (not entry) or entry["psid"] != st["psid"] or entry["psidts"] != st["psidts"]
	if entry and not creds_changed and entry.get("client") is not None and entry["client"]._running:
		return entry["client"]
	async with _account_build_locks[pid]:
		entry = _account_clients.get(pid)
		creds_changed = (not entry) or entry["psid"] != st["psid"] or entry["psidts"] != st["psidts"]
		if entry and not creds_changed and entry.get("client") is not None and entry["client"]._running:
			return entry["client"]
		if entry and entry.get("client") is not None:
			try:
				await entry["client"].close()
			except Exception:
				pass
		last_err = None
		sources = _fresh_cookie_sources(pid)
		proxy = _norm_proxy(st.get("proxy")) or None
		for src in sources:
			try:
				psidts = load_cached_1psidts(src["psid"]) or src["psidts"]
				c = GeminiClient(src["psid"], psidts, proxy=proxy)
				c.watchdog_timeout = float(os.environ.get("GEMINI_WATCHDOG_TIMEOUT", "150"))
				await c.init(timeout=300, auto_refresh=False, watchdog_timeout=150)
				from gemini_webapi.constants import AccountStatus
				if c.account_status != AccountStatus.AVAILABLE:
					raise RuntimeError(f"会话未通过账号状态校验({c.account_status.name})——该凭据源已失效")
				_account_clients[pid] = {"client": c, "psid": src["psid"], "psidts": src["psidts"]}
				logger.info("[客户端池] 账号 %s 会话就绪(凭据源=%s, auto_refresh=off)", pid, src["src"])
				return c
			except Exception as e:
				last_err = e
				logger.warning("[客户端池] 账号 %s 凭据源 %s init 失败: %s", pid, src["src"], str(e)[:120])
				continue
		# 所有静态凭据源都失效 → headless 自愈:用窗口 profile 真实访问 gemini,
		# Google 会下发新 Set-Cookie;仅当 Google 侧会话仍存活时有效,否则提示人工登录
		# 自愈去重:一次自愈 ~30-40s,期间同账号的其他请求直接快速 503(带 Retry-After),
		# 不在锁上排队逐个重复 32s 自愈(此前 503 风暴的成因之一)
		_now_ts = time.time()
		_recovering_since = _account_recovering.get(pid, 0.0)
		if _now_ts - _recovering_since < _ACCOUNT_RECOVER_COOLDOWN:
			logger.warning("[客户端池] 账号 %s 自愈冷却中(上次尝试 %ds 前),快速失败", pid, int(_now_ts - _recovering_since))
			raise HTTPException(
				status_code=503,
				detail="该账号会话自愈刚尝试过未成功(冷却中),请稍候重试或在账号管理手动检查登录态",
				headers={"Retry-After": "30"},
			)
		_account_recovering[pid] = _now_ts
		try:
			new_creds = await _headless_refresh_cookie(pid, proxy)
			if new_creds:
				_psid, _psidts = new_creds
				_c2 = GeminiClient(_psid, _psidts, proxy=proxy)
				_c2.watchdog_timeout = float(os.environ.get("GEMINI_WATCHDOG_TIMEOUT", "150"))
				await _c2.init(timeout=300, auto_refresh=False)
				_account_clients[pid] = {"client": _c2, "psid": _psid, "psidts": _psidts}
				_account_recovering.pop(pid, None)
				logger.info("[客户端池] 账号 %s 会话已自愈(headless 刷新 cookie)", pid)
				return _c2
		except Exception as e:
			logger.warning("[客户端池] 账号 %s headless 自愈失败: %s", pid, str(e)[:120])
		if isinstance(last_err, HTTPException):
			raise last_err
		logger.error("[客户端池] 账号 %s 全部凭据源失效(含 headless 自愈): %s", pid, str(last_err)[:160])
		raise HTTPException(
			status_code=503,
			detail="账号会话已失效且自动刷新失败——请在账号管理打开该窗口重新访问 gemini.google.com 登录一次",
			headers={"Retry-After": "60"},
		)


async def _headless_refresh_cookie(pid: str, proxy):
	"""headless 打开 gemini(用窗口 profile)获取 Google 新下发的 cookie。
	返回 (psid, psidts) 或 None(会话已死/cookie 未变化)。"""
	from fingerprint.browser import FingerprintBrowser
	from fingerprint.account_cookies import save_gemini_cookies
	browser = None
	try:
		# TargetClosedError 根因: 窗口在线时其 userdata 目录被锁,headless 用同一目录
		# launch_persistent_context 会拿到已关闭的浏览器/上下文 → 报 TargetClosedError。
		# 在线窗口自己会完成 Cookie 续期,headless 不与该窗口抢 profile 锁。
		try:
			from fingerprint.window import is_running as _is_window_running
			if _is_window_running(pid):
				trace_logger.info("[自愈] 账号 %s 窗口在线,跳过 headless 刷新(避免与在线窗口争抢 profile 锁)", pid)
				return None
		except Exception:
			pass
		browser = FingerprintBrowser(pid, proxy={"mode": proxy} if proxy else None)
		try:
			await browser.launch(headless=True)
		except Exception as e:
			# TargetClosedError: 浏览器/上下文关闭(profile 被占/窗口崩溃);记录后按"无可刷新"处理
			_is_target = "TargetClosedError" in type(e).__name__ or "has been closed" in str(e)
			if _is_target:
				trace_logger.warning("[自愈] 账号 %s headless 启动失败(浏览器/上下文已关闭,可能是窗口在线或异常退出): %s", pid, str(e)[:100])
			else:
				trace_logger.warning("[自愈] 账号 %s headless 启动失败: %s", pid, str(e)[:120])
			return None
		pg = await browser.new_page()
		try:
			await pg.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=45000)
			await asyncio.sleep(3)
		except Exception:
			pass
		cks = {c["name"]: c["value"] for c in await browser._context.cookies()}
		psid = cks.get("__Secure-1PSID", "")
		psidts = cks.get("__Secure-1PSIDTS", "")
		old = None
		try:
			_wf = Path(__file__).resolve().parent / "fingerprint" / "storage" / pid / "gemini_cookies.json"
			old = json.loads(_wf.read_text(encoding="utf-8")).get("psidts")
		except Exception:
			pass
		if psid and psidts and psidts != old:
			save_gemini_cookies(pid, psid, psidts)
			return psid, psidts
		return None
	finally:
		if browser:
			try:
				await browser.close()
			except Exception:
				pass


# ───────── 静默体检员实现 ─────────
async def _patrol_probe_account(pid: str, st: dict) -> bool:
	"""轻量探针:对存活会话发一次 quota RPC(~1s),不打扰业务。True=健康,False=可疑。"""
	# 代理判死 → quota RPC 必超时,快速失败省 20s
	_purl = _account_proxy_url(st)
	if _purl and _proxy_down(_purl):
		return False
	client = st.get("client")
	if client is None:
		client = (_account_clients.get(pid) or {}).get("client")
	if client is None:
		return False
	try:
		await asyncio.wait_for(client._fetch_quota(), timeout=20)
		return True
	except Exception:
		return False


async def _patrol_heal_account(pid: str, st: dict) -> bool:
	"""对可疑账号提前 headless 刷新 + 重建会话(复用自愈路径,但主动触发)。
	与 _account_recovering 冷却共享,避免和业务触发的自愈打架。"""
	_now = time.time()
	if _now - (_account_recovering.get(pid, 0.0)) < _ACCOUNT_RECOVER_COOLDOWN:
		return False
	_account_recovering[pid] = _now
	proxy = _norm_proxy(st.get("proxy")) or None
	try:
		new_creds = await asyncio.wait_for(_headless_refresh_cookie(pid, proxy), timeout=75)
		if new_creds:
			_psid, _psidts = new_creds
			_c2 = GeminiClient(_psid, _psidts, proxy=proxy)
			_c2.watchdog_timeout = float(os.environ.get("GEMINI_WATCHDOG_TIMEOUT", "150"))
			await asyncio.wait_for(_c2.init(timeout=300, auto_refresh=False), timeout=90)
			_account_clients[pid] = {"client": _c2, "psid": _psid, "psidts": _psidts}
			st["psid"] = _psid
			st["psidts"] = _psidts
			st["status"] = "ok"
			st["last_error"] = ""
			_account_recovering.pop(pid, None)
			_log_bridge(f"[静默体检] 账号 {pid} 已提前刷新会话(连续探针失败 {_patrol_suspects.get(pid, 0)} 次后自愈)", "SUCCESS")
			return True
		_log_bridge(f"[静默体检] 账号 {pid} headless 刷新无新 cookie(会话可能已死或未变化)", "WARNING")
		_push_alert(f"账号 {pid} 巡逻刷新未取得新 cookie,会话可能已失效", "WARNING", pid)
	except Exception as e:
		_log_bridge(f"[静默体检] 账号 {pid} 主动刷新失败: {type(e).__name__}: {str(e)[:100]}", "WARNING")
		_push_alert(f"账号 {pid} 主动刷新失败: {type(e).__name__}: {str(e)[:80]}", "ERROR", pid)
	return False


async def _account_patrol_loop():
	"""静默体检主循环: 周期性轻量探针 → 可疑账号提前 headless 刷新。
	安全设计: 探针 1s 级并发安全;刷新走冷却去重;任何异常只记日志不中断巡逻。"""
	global _patrol_last_run
	while True:
		try:
			await asyncio.sleep(PATROL_INTERVAL)
			_pool_scan()
			if not _pool:
				continue
			_patrol_last_run = time.time()
			_log_bridge(f"[静默体检] 开始巡逻 {len(_pool)} 个账号(轻量探针)…")
			for pid, st in list(_pool.items()):
				# 代理判死 → 账号探针必失败(走同一死代理),快速失败且不触发 headless 自愈
				_purl = _account_proxy_url(st)
				if _purl and _proxy_down(_purl):
					trace_logger.info("[代理] 账号 %s 的出口代理已判死,跳过巡逻探针(等代理恢复)", pid)
					continue
				ok = await _patrol_probe_account(pid, st)
				if ok:
					if _patrol_suspects.pop(pid, None):
						_log_bridge(f"[静默体检] 账号 {pid} 探针恢复正常", "SUCCESS")
					continue
				fails = _patrol_suspects.get(pid, 0) + 1
				_patrol_suspects[pid] = fails
				trace_logger.info("[静默体检] 账号 %s 探针失败 %d/%d 次", pid, fails, PATROL_PROBE_FAIL_LIMIT)
				if fails >= PATROL_PROBE_FAIL_LIMIT:
					await _patrol_heal_account(pid, st)
					_patrol_suspects.pop(pid, None)  # 无论成败重置计数(冷却防刷)
		except asyncio.CancelledError:
			raise
		except Exception as e:
			logger.warning(f"[静默体检] 巡逻异常: {e}")
			await asyncio.sleep(60)


# ───────── 代理池健康探测(方向二): 区分"代理故障"与"账号故障",防代理抖动误杀健康账号 ─────────
# 痛点: 代理断连时,走该代理的账号请求全部失败 → 熔断阶梯烧到 10 分钟 + headless 自愈
#      (自愈也走同一个死代理,纯属白费) —— 账号本身是健康的,被网络拖累。
# 方案: 按唯一代理 URL 维护健康状态(async 轻量探针,HTTP 204 无 body);
#      连败 ≥ 阈值 → 判死: 路由排除该代理账号 + 告警 + 巡逻跳过 headless 自愈;
#      判死期间业务失败不再累加账号错误链(错误是代理的,不是账号的);
#      探针恢复 → 自动清错误链/熔断,账号回池,恢复告警。
PROXY_PROBE_INTERVAL = float(os.environ.get("GEMINI_PROXY_PROBE_INTERVAL", "300"))   # 探测周期(秒)
PROXY_PROBE_TIMEOUT = float(os.environ.get("GEMINI_PROXY_PROBE_TIMEOUT", "12"))      # 单次探测超时(秒)
PROXY_PROBE_FAIL_TRIP = int(os.environ.get("GEMINI_PROXY_PROBE_FAIL_TRIP", "2"))     # 连败几次判死
PROXY_PROBE_URL = os.environ.get("GEMINI_PROXY_PROBE_URL", "https://www.gstatic.com/generate_204")
_proxy_health: dict = {}      # 归一化代理URL -> {"alive","fails","ok_streak","rtt_ms","last_error","last_ok","down_since"}


def _probe_state(url) -> dict:
	"""取(或建)某代理的健康状态条目。url 为空(直连)返回未跟踪占位。"""
	if not url:
		return {"alive": None, "fails": 0, "ok_streak": 0, "rtt_ms": None,
				"last_error": "", "last_ok": None, "down_since": None}
	st = _proxy_health.setdefault(url, {"alive": True, "fails": 0, "ok_streak": 0, "rtt_ms": None,
										"last_error": "", "last_ok": None, "down_since": None})
	return st


def _proxy_down(url) -> bool:
	"""该代理是否处于判死期(路由排除依据)。空 URL(直连)恒 False。"""
	if not url:
		return False
	return _probe_state(url)["alive"] is False


async def _probe_proxy_once(url: str, timeout: float = 0.0) -> tuple:
	"""单次代理探针: 经代理 GET 轻端点(gstatic generate_204,无 body)。
	返回 (ok, rtt_ms, err)。测的是"代理→Google 通道",与账号凭据无关。"""
	import httpx
	timeout = timeout or PROXY_PROBE_TIMEOUT
	t0 = time.time()
	try:
		async with httpx.AsyncClient(proxy=url, timeout=timeout, trust_env=False) as c:
			r = await c.get(PROXY_PROBE_URL)
			rtt = (time.time() - t0) * 1000.0
			# 2xx/3xx/407 内任意 HTTP 应答都证明隧道通(407 会让库层报错,此处按失败算)
			if r.status_code < 400:
				return True, rtt, ""
			return False, rtt, f"HTTP {r.status_code}"
	except Exception as e:
		return False, (time.time() - t0) * 1000.0, f"{type(e).__name__}: {str(e)[:90]}"


def _proxy_record_result(url: str, ok: bool, rtt_ms: float = None, err: str = ""):
	"""回填一次探测结果: 状态机 unknown/alive → down(连败≥TRIP) → alive(恢复)。
	状态翻转时推告警;恢复时联动清该代理下账号的错误链与熔断(自动回池)。"""
	if not url:
		return
	st = _probe_state(url)
	st["last_ok"] = time.time() if ok else st.get("last_ok")
	st["rtt_ms"] = round(rtt_ms) if rtt_ms is not None and ok else st.get("rtt_ms")
	was = st["alive"]
	if ok:
		st["fails"] = 0
		st["ok_streak"] = (st.get("ok_streak") or 0) + 1
		st["last_error"] = ""
		if was is False and st["ok_streak"] >= 1:  # 恢复(一次成功即回池,探针本身轻量可信)
			st["alive"] = True
			st["down_since"] = None
			dur = ""
			_log_bridge(f"[代理] 出口 {url} 探测恢复(rtt {st['rtt_ms']}ms),账号自动回池", "SUCCESS")
			_push_alert(f"出口代理恢复: {url}(rtt {st['rtt_ms']}ms),受影响账号已自动回池", "SUCCESS")
			_proxy_recovery_unwind(url)
	else:
		st["fails"] = (st.get("fails") or 0) + 1
		st["ok_streak"] = 0
		st["last_error"] = err
		if st["fails"] >= PROXY_PROBE_FAIL_TRIP and was is not False:
			st["alive"] = False
			st["down_since"] = time.time()
			_log_bridge(f"[代理] 出口 {url} 连续 {st['fails']} 次探测失败,判死: {err}", "ERROR")
			_push_alert(f"出口代理判死: {url}({err}),其下账号已路由排除,恢复后自动回池", "ERROR")


def _proxy_recovery_unwind(url: str):
	"""代理恢复联动: 该代理下所有账号清错误链/熔断/嫌疑计数 —— 之前是代理的锅,还给账号清白。"""
	n = 0
	for pid, st in _pool.items():
		if _norm_proxy(st.get("proxy")) != url and (st.get("proxy") or "") != url:
			continue
		_health_init(st)
		if st.get("_err_streak"):
			st["_err_streak"] = 0
			n += 1
		if st.get("_circuit") == "open":
			_circuit_close(st, pid)
		_patrol_suspects.pop(pid, None)
	if n:
		_log_bridge(f"[代理] 代理恢复联动: 清除 {n} 个账号的错误链(自动回池)", "SUCCESS")


def _account_proxy_url(st: dict) -> str:
	"""账号生效代理的归一化 URL(与 GeminiClient 实际使用的完全一致)。"""
	try:
		return _norm_proxy(st.get("proxy")) or (str(st.get("proxy")).strip() if st.get("proxy") else "")
	except Exception:
		return ""


async def _proxy_health_loop():
	"""代理探针主循环: 周期并发探测池内所有唯一代理(含全局 GEMINI_PROXY);
	判死的代理进入降频复测(每轮都测,便于第一时间发现恢复)。"""
	while True:
		try:
			await asyncio.sleep(PROXY_PROBE_INTERVAL)
			urls = set()
			for st in list(_pool.values()):
				u = _account_proxy_url(st)
				if u:
					urls.add(u)
			if GEMINI_PROXY:
				urls.add(_norm_proxy(GEMINI_PROXY) or GEMINI_PROXY)
			if not urls:
				continue
			results = await asyncio.gather(*[_probe_proxy_once(u) for u in urls], return_exceptions=True)
			for u, res in zip(urls, results):
				if isinstance(res, Exception):
					_proxy_record_result(u, False, None, f"{type(res).__name__}: {str(res)[:90]}")
				else:
					ok, rtt, err = res
					_proxy_record_result(u, ok, rtt, err)
					if ok:
						trace_logger.info("[代理] 探测 OK %s rtt=%.0fms", u, rtt)
					else:
						trace_logger.warning("[代理] 探测失败 %s: %s", u, err)
		except asyncio.CancelledError:
			raise
		except Exception as e:
			logger.warning(f"[代理] 探测循环异常: {e}")
			await asyncio.sleep(60)


def _proxy_health_snapshot() -> dict:
	"""供 admin API / 看板: 全部代理健康状态 + 各账号绑定概览。"""
	proxies = []
	for url, st in _proxy_health.items():
		proxies.append({"url": url, **{k: v for k, v in st.items()}})
	bindings = []
	for pid, st in list(_pool.items()):
		bindings.append({"pid": pid, "proxy": _account_proxy_url(st), "down": _proxy_down(_account_proxy_url(st))})
	return {"interval": PROXY_PROBE_INTERVAL, "fail_trip": PROXY_PROBE_FAIL_TRIP,
			"probe_url": PROXY_PROBE_URL, "proxies": proxies, "bindings": bindings}


async def _proxy_test_once(payload: dict) -> dict:
	"""看板"测试代理"按钮: 对任意代理 URL 立即探测一次(不落状态机,只返回结果)。"""
	url = (payload or {}).get("proxy") or ""
	url = _norm_proxy(url) or url
	ok, rtt, err = await _probe_proxy_once(url) if url else (False, None, "代理 URL 为空")
	return {"proxy": url, "ok": ok, "rtt_ms": round(rtt) if rtt is not None else None, "error": err}


# ───────── 会话粘性(Step3):同一任务连续轮次固定账号 ─────────
# 痛点: 健康路由每个请求都挑"最高分账号",同一 Agent 任务的多轮可能轮换账号,
#       打断 Google 服务端的 prefill 缓存(每轮重新算前缀)。
# 方案: 任务指纹(sha256 of system+首条消息) → 钉住第一次用的健康账号,
#       该账号仍健康(有 client/未熔断/有配额)就一直复用;失效才遗忘并重新路由。
# 容量 64 / TTL 30 分钟(超时未用自动遗忘,防无限增长与陈年绑定)。
_AFFINITY_MAX = int(os.environ.get("GEMINI_AFFINITY_MAX", "64"))
_AFFINITY_TTL = float(os.environ.get("GEMINI_AFFINITY_TTL", "1800"))
_AFFINITY_SICK_COOLDOWN = float(os.environ.get("GEMINI_AFFINITY_SICK_COOLDOWN", "60"))  # F11: 499 后粘性冷却
_affinity_map: dict = {}        # fingerprint -> {"pid", "last_used"}
_affinity_sick: dict = {}       # F11: pid -> 断连时刻(冷却期内该账号不再被粘)


def _msg_plain_text(m) -> str:
	"""取消息的纯文本(兼容 str 与多模态 content 列表)。"""
	c = getattr(m, "content", "")
	if isinstance(c, str):
		return c
	try:
		return "".join((it.text or "") for it in c if getattr(it, "type", "") == "text")
	except Exception:
		return ""


def _affinity_key(messages) -> str:
	"""任务指纹: system 全文 + 首条非 system 消息文本。
	同一任务的多轮请求(追加的历史/工具轮次)这些内容不变 → 指纹稳定;
	不同任务首条消息不同 → 指纹不同。异常/无消息返回 ""(不启用粘性,退化为健康路由)。"""
	try:
		if not messages:
			return ""
		sys_parts = []
		first = ""
		for m in messages:
			text = _msg_plain_text(m)
			if m.role == "system":
				sys_parts.append(text)
			elif not first:
				first = text
		seed = ("\x00".join(sys_parts) + "\x01" + first) or "anon"
		return hashlib.sha256(seed.encode("utf-8", "ignore")).hexdigest()[:16]
	except Exception:
		return ""


def _affinity_pick(key: str) -> str:
	"""返回该任务应粘住的账号 pid;若已无此任务绑定或账号不再健康 → ""。
	防线缝隙修复(F1): 粘性路径必须与就绪候选同标准 —— 手动隔离(isolated)与
	代理判死(_proxy_down)的账号不得经粘性绕过(此前只查熔断/配额,导致
	"看板明明隔离了,请求还在打这个号"的穿透)。
	F11: 客户端断连致 499 的账号进入短冷却(_AFFINITY_SICK_COOLDOWN,默认 60s),
	冷却期内同任务粘性改道 —— 断连通常意味着该会话正劣化,立刻回粘只会再撞一次。"""
	if not key:
		return ""
	ent = _affinity_map.get(key)
	if not ent:
		return ""
	pid = ent["pid"]
	st = _pool.get(pid)
	if not st or st.get("status") != "ok" or st.get("client") is None:
		return ""
	if st.get("isolated"):
		return ""
	if _proxy_down(_account_proxy_url(st)):
		return ""
	if _circuit_is_open(st) or not _account_has_quota(st):
		return ""
	# F11: 断连冷却(冷却期内不粘,冷却结束后自动恢复粘性)
	_sick_at = _affinity_sick.get(pid, 0.0)
	if _sick_at and time.time() - _sick_at < _AFFINITY_SICK_COOLDOWN:
		return ""
	if _sick_at:
		_affinity_sick.pop(pid, None)  # 冷却已过,清标记
	ent["last_used"] = time.time()
	return pid


def _affinity_remember(key: str, pid: str):
	"""记录/刷新该任务的账号归属。容量与 TTL 清理(防膨胀)。"""
	if not key or not pid:
		return
	now = time.time()
	if len(_affinity_map) >= _AFFINITY_MAX:
		for k in [k for k, v in _affinity_map.items() if now - v.get("last_used", 0) > _AFFINITY_TTL]:
			_affinity_map.pop(k, None)
	if len(_affinity_map) >= _AFFINITY_MAX:
		_oldest = min(_affinity_map.items(), key=lambda kv: kv[1].get("last_used", 0))[0]
		_affinity_map.pop(_oldest, None)
	_affinity_map[key] = {"pid": pid, "last_used": now}


async def acquire_client(affinity_key: str = "") -> object:
	"""租当前账号的单例会话;同账号并发用信号量限流(默认2,可 GEMINI_PER_ACCOUNT_CONCURRENCY 调)。
	Step1: 账号选择改为健康路由 —— 就绪账号中按健康分最高取号(自动避开慢/故障/熔断账号);
	无健康可用时回退到活动/首个账号(保留原语义:冷启动或单账号池不因路由而 503)。
	Step3: 会话粘性 —— 若该任务(affinity_key)已有归属账号且仍健康,优先复用同一账号
	(同一任务连续轮次固定账号 → Google 服务端 prefill 缓存命中率高,响应更快更稳)。"""
	global _active_pool_pid
	cur = asyncio.current_task()
	if cur in _client_checkout:
		return _client_checkout[cur]["client"]
	_pool_scan()
	pid = _affinity_pick(affinity_key)
	if not pid:
		# 首次分配: 健康分加权随机(软负载均衡),避免所有新任务集中到最高分账号
		pid = _pick_ready_soft()
	if not pid:
		# F12 回退(熔断感知): 活动/首个账号,跳过隔离与熔断拦截期内账号;
		# 全池都在熔断期内时,选 circuit_until 最早到期者作"有意探针"(保留原防 503 语义:
		# 单账号池冷启动不因路由而 503),不再盲目穿透熔断阶梯。
		_cands = [p for p, s in _pool.items() if (s.get("psid") or "") and not s.get("isolated")]
		_closed = [p for p in _cands if not _circuit_is_open(_pool[p])]
		_open = [p for p in _cands if _circuit_is_open(_pool[p])]
		if _closed:
			pid = _active_pool_pid if _active_pool_pid in _closed else _closed[0]
		elif _open:
			pid = min(_open, key=lambda p: _pool[p].get("_circuit_until") or 0.0)
			_log_bridge(f"[账号池] 全池熔断,放行 {pid} 作最早到期探针(防 503)", "WARNING")
		else:
			pid = ""
	st = _pool.get(pid)
	if not st or not (st.get("psid") or ""):
		raise HTTPException(status_code=503, detail="没有可用的指纹账号(请先在账号管理创建窗口并登录)", headers={"Retry-After": "60"})
	if affinity_key:
		_affinity_remember(affinity_key, pid)
	if pid != _active_pool_pid:
		_active_pool_pid = pid
	if "_sem" not in st:
		st["_sem"] = asyncio.Semaphore(_PER_ACCT_CONCURRENCY)
	# F13: 取号排队从 300s 收敛为 GEMINI_ACQUIRE_WAIT(默认 60s)—— 客户端看门狗一般 300s,
	# 傻等 300s 大概率等到的是"客户端已断开"。超时不直接 503:先换一个就绪账号承接(粘性跟随换号),仍失败才 503。
	try:
		await asyncio.wait_for(st["_sem"].acquire(), timeout=_ACQUIRE_WAIT)
	except asyncio.TimeoutError:
		_alt = _pick_ready_soft(exclude=pid)
		_switched = False
		if _alt and _alt != pid:
			_st2 = _pool.get(_alt)
			if _st2 and (_st2.get("psid") or ""):
				if "_sem" not in _st2:
					_st2["_sem"] = asyncio.Semaphore(_PER_ACCT_CONCURRENCY)
				try:
					await asyncio.wait_for(_st2["_sem"].acquire(), timeout=_ACQUIRE_WAIT)
					_log_bridge(f"[账号池] {pid} 并发已满 → 换 {_alt} 承接本请求", "WARNING")
					pid, st = _alt, _st2
					_switched = True
				except asyncio.TimeoutError:
					pass
		if not _switched:
			raise HTTPException(status_code=503, detail=(
				"同账号并发已达上限(进行中的长响应未结束),且无可换账号承接;"
				"请稍后重试或调大 GEMINI_PER_ACCOUNT_CONCURRENCY / GEMINI_ACQUIRE_WAIT"), headers={"Retry-After": "10"})
	# 到这里信号量已持有(可能已换号)→ 刷新归属与活动指针,再取客户端
	if affinity_key:
		_affinity_remember(affinity_key, pid)
	if pid != _active_pool_pid:
		_active_pool_pid = pid
	try:
		client = await _get_or_rebuild_account_client(pid, st)
	except Exception:
		st["_sem"].release()
		raise
	_client_checkout[cur] = {"pid": pid, "client": client, "sem": st["_sem"]}
	return client


async def release_client() -> None:
	"""归还:释放信号量。若请求期间会话被上游 close → 单例置 None,下次请求热重建。"""
	cur = asyncio.current_task()
	chk = _client_checkout.pop(cur, None)
	if not chk:
		return
	entry = _account_clients.get(chk["pid"])
	if entry and entry.get("client") is chk["client"]:
		c = chk["client"]
		try:
			if c is None or not c._running:
				_account_clients[chk["pid"]] = None
		except Exception:
			_account_clients[chk["pid"]] = None
	try:
		chk["sem"].release()
	except Exception:
		pass


def mark_client_sick(client: object) -> None:
	"""请求中捕获会话停机(挂起/UNAUTH):单例置空,下次请求用最新凭据热重建。"""
	for pid, entry in _account_clients.items():
		if entry and entry.get("client") is client:
			_account_clients[pid] = None
			_push_alert(f"账号 {pid} 会话在请求中失效(挂起/未授权),已标记待重建", "ERROR", pid)
			break


def get_image_signature(url: str) -> str:
	"""
	Generate a HMAC-SHA256 signature for the image URL using the persistent SIGNATURE_SECRET.
	"""
	secret = SIGNATURE_SECRET.encode()
	return hmac.new(secret, url.encode(), hashlib.sha256).hexdigest()


def postprocess_text(text: str) -> str:
	"""Apply text cleanup and markdown corrections to response text."""
	text = text.replace("&lt;", "<").replace("\\<", "<").replace("\\_", "_").replace("\\>", ">")
	return correct_markdown(text)


def extract_image_markdown(response, base_url: str) -> str:
	"""Extract images from a response and return markdown image links."""
	result = ""
	if hasattr(response, "images") and response.images:
		for img in response.images:
			img_url = getattr(img, "url", None)
			if img_url:
				sig = get_image_signature(img_url)
				proxy_url = f"{base_url}/gemini-proxy/image?url={quote(img_url)}&sig={sig}"
				result += f"\n\n![🎨 Loading image...]({proxy_url})"
	return result


# ──────────── 上下文优雅降级(替代硬 400)────────────
# 背景: DSH 切模型后按新模型窗口放宽压缩目标,可能发出 >950K 字符请求,旧逻辑直接 400,
# DSH 靠 request-error 陷阱试错式压缩(连打 3 次 400 才成功)。这里改为服务端主动截断:
# 保留 system + 最新轮次(工具调用成对删除),压回预算内继续执行 → DSH 无需经历失败重试。
CONTEXT_DEGRADE_MODE = os.environ.get("CONTEXT_DEGRADE_MODE", "auto").lower()  # auto | off


def _estimate_message_chars(msg) -> int:
	"""单条消息在扁平对话中的近似字符数(与 prepare_conversation 输出严格对齐)。"""
	if msg.role == "tool":
		return len(tools_shim.serialize_tool_result(msg)) + 2
	prefix = {"system": "System: ", "user": "Human: ", "assistant": "Assistant: "}.get(msg.role, "")
	if msg.role == "assistant" and getattr(msg, "tool_calls", None):
		return len(prefix) + len(tools_shim.serialize_assistant_tool_calls(msg)) + 2
	content = msg.content
	if isinstance(content, str):
		body = content
	else:
		body = "".join((item.text or "") for item in content if item.type == "text")
	return len(prefix) + len(body) + 2


_TRUNC_NOTICE_TMPL = "\n\n[…内容过长已自适应截断,省略约 {n} 字符,仅保留开头部分…]"


def _clone_with_content(msg, new_content):
	"""返回替换 content 后的消息副本(兼容 pydantic Message 与测试桩)。"""
	try:
		return msg.model_copy(update={"content": new_content})
	except Exception:
		import copy as _copy
		c = _copy.copy(msg)
		c.content = new_content
		return c


def _truncate_message_content(msg, max_chars: int):
	"""把单条消息的文本内容头部截断到 max_chars(含截断提示)。
	返回 (新消息, 省略字符数);无需截断返回原消息。tool_calls 消息不截断(会破坏 JSON)。"""
	if max_chars <= 0 or getattr(msg, "tool_calls", None):
		return msg, 0
	content = getattr(msg, "content", "")
	notice_reserve = len(_TRUNC_NOTICE_TMPL.format(n=999999)) + 8
	if isinstance(content, str):
		if len(content) <= max_chars:
			return msg, 0
		keep = max(0, max_chars - notice_reserve)
		omitted = len(content) - keep
		new_c = content[:keep] + _TRUNC_NOTICE_TMPL.format(n=omitted)
		return _clone_with_content(msg, new_c), omitted
	# 多模态 content 列表: 只截文本项(与非文本项的估计口径一致,非文本项不计入估算)
	try:
		text_total = sum(len(it.text or "") for it in content if getattr(it, "type", "") == "text")
		if text_total <= max_chars:
			return msg, 0
		keep = max(0, max_chars - notice_reserve)
		omitted = text_total - keep
		new_items = []
		used = 0
		for it in content:
			if getattr(it, "type", "") == "text" and used < keep:
				t = it.text or ""
				remain = keep - used
				if len(t) > remain:
					try:
						new_items.append(it.model_copy(update={"text": t[:remain]}))
					except Exception:
						return msg, 0  # 无法安全截断 → 保持原样
					used += remain
				else:
					new_items.append(it)
					used += len(t)
			else:
				new_items.append(it)
		# 追加截断提示到最后一个文本项
		for i in range(len(new_items) - 1, -1, -1):
			it = new_items[i]
			if getattr(it, "type", "") == "text":
				try:
					new_items[i] = it.model_copy(update={"text": (it.text or "") + _TRUNC_NOTICE_TMPL.format(n=omitted)})
				except Exception:
					pass
				break
		return _clone_with_content(msg, new_items), omitted
	except Exception:
		return msg, 0


def _degrade_messages_for_budget(messages, max_chars: int) -> tuple:
	"""从最旧的非 system 消息开始裁剪历史,直到估算总长 ≤ max_chars。
	- 保留全部 system 消息与最后一条(当前提问,绝不删除)
	- assistant tool_calls 与其紧随的 tool 结果成对删除(避免截断到一半)
	- 返回 (截断后 messages, 裁剪掉的估算字符数)"""
	if not messages:
		return messages, 0
	tail_add = len("Assistant: ")
	total = sum(_estimate_message_chars(m) for m in messages) + tail_add
	if total <= max_chars:
		return messages, 0
	drop_from = next((i for i, m in enumerate(messages) if m.role != "system"), 0)
	# system 消息必须计入预算(返回时会原样加回,漏算会导致最终对话超出预算)
	head = list(messages[:drop_from])
	head_est = sum(_estimate_message_chars(x) for x in head)
	kept = list(messages[drop_from:])
	kept_est = head_est + sum(_estimate_message_chars(x) for x in kept) + tail_add
	i = 0
	while kept_est > max_chars and i < len(kept) - 1:
		removed = _estimate_message_chars(kept[i])
		if kept[i].role == "assistant" and getattr(kept[i], "tool_calls", None):
			j = i + 1
			while j < len(kept) and kept[j].role == "tool":
				removed += _estimate_message_chars(kept[j])
				j += 1
			del kept[i:j]
		else:
			del kept[i]
		kept_est -= removed
	# 边界安全:开头残留的孤立 tool 结果一并移除。
	# 注意保留"仅剩的最后一条"(len(kept)>1)—— 它是当前提问,交给下方自适应截断而不是丢掉。
	while len(kept) > 1 and kept[0].role == "tool":
		kept_est -= _estimate_message_chars(kept[0])
		kept.pop(0)
	# ── 最后手段: 仍超预算 → 对最后一条消息本身做自适应内容截断 ──
	# 场景: 最新一条是巨型工具结果/超长粘贴,历史已删光也压不回 → 截其内容(保开头+省略提示)
	# 而不是死亡 400。tool_calls 消息不截(会破坏 JSON)。
	if kept_est > max_chars and kept:
		last = kept[-1]
		if getattr(last, "tool_calls", None):
			# 最后一条是工具调用且超限: 连同其结果一起删(执行链完整性优先),只留 system+更早内容
			j = len(kept)
			while j > 0 and kept[j - 1].role in ("assistant", "tool"):
				j -= 1
			for k in range(j, len(kept)):
				kept_est -= _estimate_message_chars(kept[k])
			kept = kept[:j]
		else:
			# allowance = 预算 - 其余保留部分 - 截断提示与框架开销余量
			allowance = max(0, max_chars - (kept_est - _estimate_message_chars(last)) - 200)
			new_last, omitted = _truncate_message_content(last, allowance)
			if omitted > 0:
				kept_est -= omitted
				kept[-1] = new_last
	return head + kept, total - kept_est


# ───────── 上下文压缩(超限过多时: LLM 分批摘要替代机械裁剪,防 Agent 降智) ─────────
# 机械裁剪=失忆(丢中间步骤,Agent 重复踩坑);LLM 压缩=记要点(保留目标/步骤/发现/待办)。
# 梯度: 超限 ≤ _COMPRESS_SMALL_OVER → 机械裁剪(瞬时);超出过多 → 分批压缩;失败 → 回退机械。
# 摘要按内容哈希缓存: Agent 多轮迭代时旧段内容不变 → 摘要直接命中,不重复消耗。
CONTEXT_COMPRESS_MODE = os.environ.get("CONTEXT_COMPRESS_MODE", "auto").lower()   # auto | off
_COMPRESS_TAIL_KEEP = int(os.environ.get("GEMINI_COMPRESS_TAIL_KEEP", "200000"))   # 最近内容原样保留
_COMPRESS_BATCH = int(os.environ.get("GEMINI_COMPRESS_BATCH", "700000"))           # 单批压缩输入上限
_COMPRESS_DIGEST_MAX = int(os.environ.get("GEMINI_COMPRESS_DIGEST_MAX", "16000"))  # 单份摘要上限(字符)
_COMPRESS_SMALL_OVER = int(os.environ.get("GEMINI_COMPRESS_SMALL_OVER", "50000"))  # 超出≤此值走机械
_COMPRESS_CACHE: dict = {}     # sha256(batch_text) -> {"digest", "t"}
_COMPRESS_CACHE_MAX = 32
_SUMMARIZE_PROMPT = (
    "你是会话压缩器。将下面的对话历史压缩成一份高密度摘要,供 AI 助手继续执行任务时使用。"
    "必须保留: 1) 用户的任务目标与全部约束; 2) 已完成的步骤及结果(关键文件路径/命令/数据/编号); "
    "3) 重要发现、错误与解决办法; 4) 未完成事项与下一步计划。\n"
    "【格式纪律(最重要)】凡描述已执行的工具调用,必须原样保留原生标签格式 "
    '<tool_call>{{"name":"工具名","arguments":{{关键参数}}}}</tool_call>'
    "(arguments 可精简为关键参数),严禁写成'已调用XX工具/执行了XX命令'的叙述句式"
    " —— 叙述式样例会诱导后续助手模仿叙述而不真正调用工具。\n"
    "要求: 条目化、信息密集、严禁虚构;总长不超过 {maxc} 字。\n\n"
    "[对话历史开始]\n{body}\n[对话历史结束]"
)


def _msg_flat_text(msg) -> str:
    """消息 → 扁平对话文本(与 prepare_conversation 口径一致,供摘要输入)。"""
    if msg.role == "tool":
        return tools_shim.serialize_tool_result(msg) + "\n\n"
    if msg.role == "assistant" and getattr(msg, "tool_calls", None):
        return "Assistant: " + tools_shim.serialize_assistant_tool_calls(msg) + "\n\n"
    prefix = {"system": "System: ", "user": "Human: ", "assistant": "Assistant: "}.get(msg.role, "")
    return prefix + _msg_plain_text(msg) + "\n\n"


def _split_compress_batches(middle: list, batch_chars: int) -> list:
    """中段消息按估算字符数切成若干批(消息粒度,不拆单条)。"""
    batches, cur, cur_est = [], [], 0
    for msg in middle:
        est = _estimate_message_chars(msg)
        if cur and cur_est + est > batch_chars:
            batches.append(cur)
            cur, cur_est = [], 0
        cur.append(msg)
        cur_est += est
    if cur:
        batches.append(cur)
    return batches


def _compress_cache_get(key: str):
    ent = _COMPRESS_CACHE.get(key)
    if ent is not None:
        ent["t"] = time.time()
    return (ent or {}).get("digest")


def _compress_cache_put(key: str, digest: str):
    if len(_COMPRESS_CACHE) >= _COMPRESS_CACHE_MAX:
        oldest = min(_COMPRESS_CACHE.items(), key=lambda kv: kv[1]["t"])[0]
        _COMPRESS_CACHE.pop(oldest, None)
    _COMPRESS_CACHE[key] = {"digest": digest, "t": time.time()}


async def _summarize_batch(gemini_client, batch: list) -> tuple:
    """把一批消息交给 LLM 压缩成摘要。返回 (digest, from_cache)。"""
    texts = []
    for msg in batch:
        if _estimate_message_chars(msg) > _COMPRESS_BATCH:
            msg, _ = _truncate_message_content(msg, int(_COMPRESS_BATCH * 0.9))  # 摘要输入端头截断
        texts.append(_msg_flat_text(msg))
    body = "".join(texts)
    key = hashlib.sha256(body.encode("utf-8", "ignore")).hexdigest()
    cached = _compress_cache_get(key)
    if cached:
        return cached, True
    prompt = _SUMMARIZE_PROMPT.format(maxc=_COMPRESS_DIGEST_MAX, body=body[:_COMPRESS_BATCH])
    resp = await asyncio.wait_for(gemini_client.generate_content(prompt), timeout=180)
    digest = (getattr(resp, "text", "") or "").strip()
    if not digest:
        raise RuntimeError("压缩器返回空摘要")
    if len(digest) > _COMPRESS_DIGEST_MAX:
        digest = digest[:_COMPRESS_DIGEST_MAX]
    _bump_token_usage(_count_tokens(prompt), _count_tokens(digest))
    try:
        if AUTO_DELETE_CHAT and getattr(resp, "metadata", None):
            asyncio.create_task(background_delete_chat(gemini_client, resp.metadata[0]))
    except Exception:
        pass
    _compress_cache_put(key, digest)
    return digest, False


async def _compact_messages(gemini_client, messages, budget_chars: int, deadline: float = 0.0, disconnect_task=None):
    """分批压缩中段历史并组装新上下文。返回 (new_messages, info)。
    无可压中段时抛 RuntimeError → 调用方回退机械裁剪。
    F6: deadline(时间预算)与 disconnect_task(断连感知)——
    客户端看门狗从它发请求起算,压缩耗时必须计入总预算,否则
    "压缩 60s + 生成 260s = 必然 499";断连后立即放弃压缩(省 3×180s 白工)。"""
    # 尾段: 从末尾向前保留最近内容(原样,缓存语义最相关)
    tail, tail_est, i = [], 0, len(messages)
    while i > 0 and tail_est < _COMPRESS_TAIL_KEEP:
        tail.insert(0, messages[i - 1])
        tail_est += _estimate_message_chars(messages[i - 1])
        i -= 1
    # 尾段不能以孤立 tool 结果开头 → 补齐其父 tool_calls
    while tail and tail[0].role == "tool" and i > 0:
        i -= 1
        tail.insert(0, messages[i])
        tail_est += _estimate_message_chars(messages[i])
    head = [m for m in messages[:i] if m.role == "system"]      # system 原样保留
    middle = [m for m in messages[:i] if m.role != "system"]    # 中段 = 待压缩历史
    if not middle:
        raise RuntimeError("无可压缩中段(历史已全部在尾部保留)")
    if disconnect_task is not None and disconnect_task.done():
        raise RuntimeError("客户端已断连,放弃压缩")
    batches = _split_compress_batches(middle, _COMPRESS_BATCH)
    # F6: 批数按时间预算收敛 —— 留至少 90s 给正式生成与网络往返
    if deadline > 0:
        remaining = deadline - time.time()
        # 预算已耗尽(或剩余不足以完成任何一批)→ 直接放弃压缩交机械裁剪
        if remaining <= 90.0:
            raise RuntimeError(f"时间预算已耗尽(剩 {remaining:.0f}s),放弃压缩")
        usable = remaining - 90.0
        per_batch = 180.0  # 单批摘要超时
        max_batches = max(1, int(usable // per_batch))
        if len(batches) > max_batches:
            trace_logger.warning(
                "[ctx-compact] 时间预算不足: %d 批 > 预算 %d 批 → 保留最新 %d 批,更旧的直接交给机械裁剪",
                len(batches), max_batches, max_batches)
            keep = batches[-max_batches:] if max_batches > 0 else []
            dropped_batches = batches[:-max_batches] if max_batches > 0 else batches
            _dropped = [x for b in dropped_batches for x in b]
            tail = _dropped + tail  # 被放弃的更旧批次原样并入尾段(宁可超限交裁剪,不静默丢内容)
            middle = [x for b in keep for x in b]
            batches = keep
            if not batches:
                raise RuntimeError("时间预算不足以压缩任何批次")
    digests, hits = [], 0
    for b in batches:
        if disconnect_task is not None and disconnect_task.done():
            raise RuntimeError("客户端已断连,中止压缩")
        if deadline > 0 and time.time() > deadline - 90.0:
            trace_logger.warning("[ctx-compact] 批间检查: 超出时间预算,中止后续压缩交机械裁剪")
            break
        d, hit = await _summarize_batch(gemini_client, b)
        hits += 1 if hit else 0
        digests.append(d)
    digest_text = "\n\n".join(digests)
    digest_msg = Message(role="user", content=f"[前情摘要 · 系统压缩历史生成,只读]\n{digest_text}")
    new_messages = head + [digest_msg] + tail
    info = {"compacted": True, "batches": len(batches), "cache_hits": hits,
            "digest_chars": len(digest_text), "middle_est": sum(_estimate_message_chars(x) for x in middle)}
    return new_messages, info


def _ctx_guard(conversation: str):
	"""发送前字符数预检。返回 None 放行;否则返回应直接 return 的 400 响应。
	超长请求在此拦截:零上游交互、零耗时、不消耗账号会话,
	避免"上传 1MB → Google 拒 → 库内重试 5 次 → 156 秒后 500"且把会话烧失效。"""
	char_len = len(conversation) if isinstance(conversation, str) else sum(len(p) for p in conversation)
	try:
		request_chars_var.set(char_len)
	except Exception:
		pass
	if char_len <= CTX_CHECK_THRESHOLD:
		return None
	logger.warning("[ctx-guard] 拒绝超长请求: %d 字符 > 阈值 %d(未触达上游)", char_len, CTX_CHECK_THRESHOLD)
	return context_overflow_response(char_len)


def _cleanup_temp_files(temp_files):
	for temp_file in temp_files or []:
		try:
			os.unlink(temp_file)
		except Exception:
			pass


async def _watch_disconnect(req, interval: float = 1.5):
	"""轮询客户端断连:断开返回 True(用于取消上游僵尸工作)。
	探测不可用时挂起自身永不完成 —— 行为退化为旧版,绝不误杀正常请求。"""
	try:
		while True:
			if await req.is_disconnected():
				return True
			await asyncio.sleep(interval)
	except asyncio.CancelledError:
		raise
	except Exception:
		await asyncio.Event().wait()


@app.post("/v1/chat/completions")
async def create_chat_completion(
	request: ChatCompletionRequest,
	raw_request: Request,
	api_key: str = Depends(verify_api_key),
):
	"""
	Handle chat completion requests, translating from OpenAI API format to Gemini API format.
	Supports both streaming and non-streaming responses, caching, thinking features,
	and background conversation cleanup based on configuration.
	"""
	try:
		# Step5 按需思考: 本次请求是否开启 thinking(显式 thinking > reasoning_effort > 全局)
		_thinking_on = _effective_thinking(request)
		# 请求级客户端池:租独立 client(单副本挂起只影响本请求,不拖垮全局)
		_released = {"v": False, "streaming": False}

		async def _release_now():
			"""幂等归还。纯流式路径由 generate_stream 的 finally 归还
			(handler 返回响应头时流还在生成,提前归还会让副本被并发租出——1.2 初版间歇 500 根因)。"""
			if _released["streaming"]:
				return
			if not _released["v"]:
				_released["v"] = True
				await release_client()

		gemini_client = await acquire_client(affinity_key=_affinity_key(request.messages))
		_background_refs = [gemini_client]

		# 转换消息为对话格式（启用 Gem 时跳过 system 消息）
		# 上下文超预算梯度处理: LLM 分批压缩(防降智) → 机械裁剪(兜底) → 400(物理极限)
		_degraded_chars = 0
		_degrade_applied = False
		_messages_for_conv = request.messages
		_compress_info = None
		if CONTEXT_DEGRADE_MODE != "off" and CONTEXT_COMPRESS_MODE != "off":
			# 工具协议注入体积必须计入预算(协议含全部工具 Schema,可达数十 KB),
			# 否则"估算略低于阈值 + 协议注入"会突破 98 万被 ctx_guard 拒 400
			_proto_reserve = 0
			if request.tools:
				try:
					_proto_reserve = len(tools_shim.build_protocol(
						request.tools,
						parallel=(request.parallel_tool_calls is not False and PARALLEL_TOOL_CALLS),
					))
				except Exception:
					_proto_reserve = 8000  # 兜底预留
			_budget = max(100000, CTX_CHECK_THRESHOLD - _proto_reserve)
			_est_total = sum(_estimate_message_chars(m) for m in request.messages) + len("Assistant: ") + _proto_reserve
			if _est_total > CTX_CHECK_THRESHOLD:
				_over = _est_total - _budget
				# 梯度1: 大幅超限 → LLM 分批压缩(保要点,防降智);失败 → 梯度2 机械裁剪兜底
				if _over > _COMPRESS_SMALL_OVER:
					# F6: 断连监听提前到压缩阶段(压缩期间客户端断开 → 立即放弃,不再白做 3×180s 摘要);
					# 总时间预算 200s,留 ≥90s 给正式生成;超预算自动降批数,放弃的批次原样并入尾段交机械裁剪
					_compact_disc = asyncio.create_task(_watch_disconnect(raw_request))
					try:
						_compress_deadline = time.time() + 200.0
						_compacted, _compress_info = await _compact_messages(
							gemini_client, request.messages, _budget,
							deadline=_compress_deadline, disconnect_task=_compact_disc)
						_est_c = sum(_estimate_message_chars(x) for x in _compacted) + len("Assistant: ") + _proto_reserve
						if _est_c <= _budget:
							_messages_for_conv = _compacted
							_degrade_applied = True
							logger.warning(
								"[ctx-compact] LLM 压缩完成: %d 字符 → %d 字符(协议 %d, %d 批, 缓存命中 %d) — 保要点防降智",
								_est_total, _est_c, _proto_reserve, _compress_info.get("batches", 0),
								_compress_info.get("cache_hits", 0),
							)
							_push_alert(
								f"上下文已 LLM 压缩: {_est_total}→{_est_c} 字符({_compress_info.get('batches', 0)} 批,"
								f"缓存命中 {_compress_info.get('cache_hits', 0)})", "WARNING")
						else:
							raise RuntimeError(f"压缩后仍超预算({_est_c})")
					except Exception as e:
						logger.warning("[ctx-compact] LLM 压缩失败(%s: %s) → 回退机械裁剪", type(e).__name__, str(e)[:100])
						_messages_for_conv = request.messages
					finally:
						_compact_disc.cancel()
				# 梯度2: 小幅超限或压缩失败 → 机械裁剪(瞬时,零成本)
				if not _degrade_applied:
					_messages_for_conv, _degraded_chars = _degrade_messages_for_budget(request.messages, _budget)
					_degrade_applied = _degraded_chars > 0
					if _degrade_applied:
						logger.warning(
							"[ctx-degrade] 机械裁剪: 估算 %d 字符(含协议预留 %d) → 裁剪 %d 字符 (保 system+最新轮次)",
							_est_total, _proto_reserve, _degraded_chars,
						)
		conversation, temp_files = prepare_conversation(
			_messages_for_conv,
			skip_system=bool(GEM_ID),
		)
		logger.info(
			"Chat completion request: stream=%s requested_model=%s messages=%s temp_files=%s",
			request.stream,
			request.model,
			len(request.messages),
			len(temp_files),
		)

		# 获取适当的模型
		model = map_model_name(request.model)
		model_debug_info = get_effective_model_debug_info(model)
		logger.info(
			"Resolved Gemini model: requested=%s effective=%s headers=%s",
			request.model,
			model_debug_info["model_name"],
			model_debug_info["model_header"],
		)

		# 创建响应对象
		completion_id = f"chatcmpl-{uuid.uuid4()}"
		created_time = int(time.time())
		base_url = PUBLIC_BASE_URL or str(raw_request.base_url).rstrip("/")

		# Prepare generate_content arguments
		gen_kwargs = {"model": model}
		if TEMPORARY_CHAT:
			gen_kwargs["temporary"] = True
		if temp_files:
			gen_kwargs["files"] = temp_files
		if GEM_ID:
			gen_kwargs["gem"] = GEM_ID

		# ============ webTools 模拟工具调用 ============
		if request.tools:
			# F3 前置: 工具名清单(协议注入与 tool_choice 校验都要用)
			_tool_names = []
			for _t in (request.tools or []):
				if isinstance(_t, dict):
					_fn = _t.get("function") or {}
					if _fn.get("name"):
						_tool_names.append(_fn["name"])
			# 结构修复: 协议必须注入在 "Assistant: " 生成点之前!
			# 若协议落在生成点之后,模型把协议当"自己要续写的素材" → 回声/绕过协议直接叙述
			_TAIL = "Assistant: "
			_conv_before_protocol = conversation
			if conversation.endswith(_TAIL):
				_conv_before_protocol = conversation[: -len(_TAIL)]
				conversation = _conv_before_protocol
			conversation += tools_shim.build_protocol(
				request.tools,
				parallel=(request.parallel_tool_calls is not False and PARALLEL_TOOL_CALLS),
			)
			# F10: 记录注入的协议体积(usage 对账时扣除,非用户/模型内容;tool_choice 指令稍后追加)
			_proto_chars = len(conversation) - len(_conv_before_protocol)
			# F3: tool_choice 支持 —— required/指定函数 → 协议内强制指令;
			# 不支持的模式显式 400(此前静默忽略,客户端以为强制了却拿到纯文本 = 另一类假执行)
			_tc_directive = ""
			_tc_name = ""
			if request.tool_choice is not None:
				if isinstance(request.tool_choice, str) and request.tool_choice in ("required", "auto", "none"):
					_tc_directive = tools_shim.build_tool_choice_directive(request.tool_choice, _tool_names)
				elif isinstance(request.tool_choice, dict):
					_fn = (request.tool_choice.get("function") or {})
					_tc_name = _fn.get("name") or request.tool_choice.get("name")
					if _tc_name and _tc_name not in _tool_names:
						raise HTTPException(status_code=400, detail=(
							f"tool_choice 指定的工具 '{_tc_name}' 未在 tools 中声明;"
							"请核对工具名,或使用 tool_choice='required'"))
					_tc_directive = tools_shim.build_tool_choice_directive(request.tool_choice, _tool_names)
				else:
					raise HTTPException(status_code=400, detail=(
						f"不支持的 tool_choice 形态: {request.tool_choice!r};"
						"支持 'auto'/'required'/'none' 或 {'type':'function','function':{'name':...}}"))
			if _tc_directive:
				conversation += _tc_directive
				_proto_chars += len(_tc_directive)
			conversation += _TAIL
			_blocked = _ctx_guard(conversation)
			if _blocked is not None:
				_cleanup_temp_files(temp_files)
				await _release_now()
				# 降级后仍超限 → 400 附带"缩减最新消息本身"的指引
				return context_overflow_response(
					len(conversation) if isinstance(conversation, str) else sum(len(p) for p in conversation),
					after_degrade=_degrade_applied)
			# 工具轮次:非流式生成以便提取;校验失败打回重试(≤2次)
			last_response = None
			final_text = ""
			tool_calls = []
			attempt_conv = conversation
			_plan_only_last = False  # 最后一轮是否为"计划收尾"(用于连败放行时告警)
			# 执行中的多步任务判定:历史里已有工具调用(tool 结果或 assistant tool_calls)
			# → 防线3 依据:纯计划收尾视为"假完成",打回强制执行
			_has_prior_tool_use = any(
				getattr(m, "tool_calls", None) or m.role == "tool" for m in request.messages)
			# 防线4(第一轮计划收尾): 仅当用户消息本身在要"计划/方案"时才放行计划文本;
			# 否则执行型任务第一轮就甩计划 = 假执行,同样打回。
			_last_user_text = ""
			for _m in reversed(request.messages):
				if _m.role == "user":
					_last_user_text = _msg_plain_text(_m)
					break
			_user_asks_plan = tools_shim.user_asks_plan(_last_user_text)
			# 防线7 开关: 谎报完成自查(GEMINI_COMPLETION_VERIFY,默认开)
			_verify_completion = os.environ.get("GEMINI_COMPLETION_VERIFY", "true").lower() != "off"
			# 追踪上下文 + 断连竞速:
			# DSH 对工具路径发的是"伪流式"(端点生成完才开始发 SSE),其 300s 空闲看门狗
			# 实际给整个生成计时;客户端超时断连后,此前上游会继续跑完(retry=5 的僵尸工作,
			# 反复锤已劣化会话 → 烧号)。现在竞速监听断连,断开即取消上游调用。
			_trace_state = {"pid": _active_pool_pid, "chars": len(conversation), "ttfb": [], "tools": True}
			gen_kwargs["_trace_state"] = _trace_state  # 观测器在 _generate 入口 pop,不透传上游
			_t0_req = time.time()
			_disconnect = asyncio.create_task(_watch_disconnect(raw_request))
			try:
				for attempt in range(3):
					gen_task = asyncio.ensure_future(gemini_client.generate_content(attempt_conv, **gen_kwargs))
					_done, _pending = await asyncio.wait({gen_task, _disconnect}, return_when=asyncio.FIRST_COMPLETED)
					if _disconnect in _done and not gen_task.done():
						# 客户端已断连:取消上游僵尸工作 + 会话标记待重建
						gen_task.cancel()
						try:
							await gen_task
						except BaseException:
							pass
						mark_client_sick(gemini_client)
						# F11: 断连账号进入粘性冷却 —— 同任务短时间内的下一轮不再撞同一劣化会话
						try:
							if _active_pool_pid:
								_affinity_sick[_active_pool_pid] = time.time()
						except Exception:
							pass
						trace_logger.warning(
							"[zombie] client disconnected mid-generation; upstream cancelled (chars=%s ttfb=%s elapsed=%.1fs)",
							len(conversation), _trace_state.get("ttfb"), time.time() - _t0_req,
						)
						_cleanup_temp_files(temp_files)
						await _release_now()
						return JSONResponse(status_code=499, content={
							"error": {"message": "client disconnected during generation", "type": "cancelled"}
						})
					last_response = gen_task.result()
					raw_text = getattr(last_response, "text", "") or ""
					calls = tools_shim.extract_tool_calls(raw_text)
					# F3 执行侧闭环: tool_choice 指定了函数 → 本回合只认该函数的调用
					if calls and _tc_name:
						calls = [c for c in calls if c.get("name") == _tc_name]
					# F3: tool_choice="none" → 客户端明确不要调用,剥掉任何标签转为纯文本
					if calls and request.tool_choice == "none":
						final_text = tools_shim.sanitize_assistant_text(raw_text)
						break
					if calls:
						valid, errors = tools_shim.validate_tool_calls(calls, request.tools)
						if valid and not errors:
							tool_calls = valid
							# 净化:剥标签/协议回声,并删除调用后自导自演的"工具结果"段落
							final_text = tools_shim.sanitize_assistant_text(raw_text)
							break
						_attempt_conv = tools_shim.append_feedback(attempt_conv, errors, level=attempt)
						_trace_state["validation_retry"] = attempt + 1
						trace_logger.info("[tools] 第 %d 次校验打回重试: %s", attempt + 1, "; ".join(errors)[:150])
						attempt_conv = _attempt_conv
						continue
					# 防线: 无合法调用,但检测到破损标签/协议回声 → 打回重试,绝不放行
					if tools_shim.has_malformed_tool_call(raw_text):
						reason = "模型回吐了 [工具调用协议] 模板" if "[工具调用协议]" in raw_text else "存在破损/未闭合的 <tool_call> 标签"
						_attempt_conv = tools_shim.append_feedback(attempt_conv, [reason + ",未解析出有效调用"], level=attempt)
						_trace_state["validation_retry"] = attempt + 1
						trace_logger.warning("[tools] 第 %d 次破损/回声拦截打回: %s", attempt + 1, reason)
						attempt_conv = _attempt_conv
						continue
					# 防线2: 无合法调用,模型伪造"工具结果"/替系统或用户发言 → 打回重试
					# (根因防护: 工具结果只会由系统以 Human 消息提供,assistant 自导自演即泄漏源)
					if tools_shim.has_fake_tool_result(raw_text) or tools_shim.has_role_confusion(raw_text):
						reason = "自导自演了'工具结果'或替系统/用户发言(结果只会由系统以 Human 消息提供,严禁编造或复述)"
						_attempt_conv = tools_shim.append_feedback(attempt_conv, [reason], level=attempt)
						_trace_state["validation_retry"] = attempt + 1
						trace_logger.warning("[tools] 第 %d 次伪造结果/角色错乱拦截打回: %s", attempt + 1, reason)
						attempt_conv = _attempt_conv
						continue
					# F3 执行侧: tool_choice 指定函数但模型迟迟不调用 → 最后一次机会原样放行纯文本
					# (客户端拿到的是明确的"未按 tool_choice 调用"信号,好过死循环)
					if _tc_name and attempt >= 2:
						trace_logger.warning("[tools] tool_choice 指定 %s 连续未调用,兜底放行纯文本", _tc_name)
						final_text = tools_shim.sanitize_assistant_text(raw_text)
						break
					# 防线3: 执行中的多步任务(历史已有工具调用)里,模型只输出"计划/方案"就收尾
					# → 打回强制以 <tool_call> 继续,否则 Agent 客户端看到纯文本 stop 就停在计划上
					if _has_prior_tool_use and tools_shim.has_plan_only(raw_text):
						_plan_only_last = True
						_trace_state["guard"] = "plan_midtask"
						reason = "你只输出了计划/方案/步骤清单,没有实际执行(历史任务尚未完成,严禁以计划收尾)"
						_attempt_conv = tools_shim.append_feedback(attempt_conv, [reason], level=attempt)
						_trace_state["validation_retry"] = attempt + 1
						trace_logger.warning("[tools] 第 %d 次计划收尾拦截打回(升级级别 %d): %s", attempt + 1, attempt, reason)
						attempt_conv = _attempt_conv
						continue
					# 防线4: 第一轮计划收尾(历史还没有工具调用)。执行型任务第一轮就甩计划 = 假执行起点,
					# Agent 客户端会把纯文本当最终回答收工。放行条件: 用户消息本身在要"计划/方案"。
					# 预算 1 次: 打回一次后若模型仍坚持计划 → 视为合法计划请求放行(避免误伤僵持)。
					if (not _has_prior_tool_use) and _tool_names and (not _user_asks_plan) \
							and tools_shim.has_plan_only(raw_text) and attempt < 2:
						_plan_only_last = True
						_trace_state["guard"] = "plan_first"
						reason = ("你声明了可调用工具但只输出了计划。若该任务需要实际操作,"
								  "请立即输出 <tool_call> 开始执行;严禁只给计划就结束回合。")
						_attempt_conv = tools_shim.append_feedback(attempt_conv, [reason], level=1)
						_trace_state["validation_retry"] = attempt + 1
						trace_logger.warning("[tools] 第 %d 次第一轮计划收尾拦截打回: %s", attempt + 1, reason[:60])
						attempt_conv = _attempt_conv
						continue
					# 防线5: 叙述式执行 —— 用"我调用了X工具"的叙述代替真实标签(历史中的调用不会因叙述发生)
					# 识别精度: 执行动词 + 已声明工具名 共现;代码围栏内示例不误报。
					if _tool_names and tools_shim.has_narrative_execution(raw_text, _tool_names):
						_trace_state["guard"] = "narrative"
						reason = ("你用文字叙述了'调用/执行工具'的过程,但叙述不会真正执行任何操作。"
								  "请立即输出真实的 <tool_call> 标签块;或若任务已完成,输出不带工具名的最终总结。")
						_attempt_conv = tools_shim.append_feedback(attempt_conv, [reason], level=attempt)
						_trace_state["validation_retry"] = attempt + 1
						trace_logger.warning("[tools] 第 %d 次叙述式执行拦截打回: %s", attempt + 1, reason[:60])
						attempt_conv = _attempt_conv
						continue
					# 防线6: 谎报完成自查 —— 执行中任务声称"已完成"但本轮没有任何工具调用。
					# 自查打回一次: 要么继续输出 <tool_call>,要么显式 [TASK-COMPLETE] 确认。
					if _verify_completion and _has_prior_tool_use and tools_shim.has_completion_claim(raw_text):
						_trace_state["guard"] = "completion_verify"
						reason = ("你声称任务已完成。结束前请自查: 对照任务目标,若仍有未完成步骤,立即输出 <tool_call> 继续;"
								  "若确已全部完成,在回答最开头加上 [TASK-COMPLETE] 标记并给出完成摘要。")
						_attempt_conv = tools_shim.append_feedback(attempt_conv, [reason], level=attempt)
						_trace_state["validation_retry"] = attempt + 1
						trace_logger.warning("[tools] 第 %d 次完成声明自查打回: %s", attempt + 1, reason[:60])
						attempt_conv = _attempt_conv
						continue
					# 正常自然语言回复:净化(剥标签/协议回声/伪造结果/错乱角色/自查标记)后放行
					final_text = tools_shim.sanitize_assistant_text(raw_text)
					if "[TASK-COMPLETE]" in final_text:
						final_text = final_text.replace("[TASK-COMPLETE]", "").strip()
					# F9: json mode 兜底提取
					if (request.response_format or {}).get("type") == "json_object":
						final_text = _enforce_json_output(final_text)
					break
			finally:
				_disconnect.cancel()
				_cleanup_temp_files(temp_files)
				gen_kwargs.pop("_trace_state", None)
				trace_logger.info(
					"[tools] done chars=%s ttfb=%s first_content=%s think_chunks=%s chunks=%s gen_seconds=%s stall=%s err=%s vretry=%s degraded=%s compact=%s total=%.1fs",
					_trace_state.get("chars"), _trace_state.get("ttfb"), _trace_state.get("first_content_at"),
					_trace_state.get("think_chunks"), _trace_state.get("chunks"), _trace_state.get("gen_seconds"),
					_trace_state.get("stall"), _trace_state.get("error"), _trace_state.get("validation_retry"),
					_degraded_chars if _degraded_chars else 0,
					json.dumps(_compress_info, ensure_ascii=False) if _compress_info else "none",
					time.time() - _t0_req,
				)
			if not tool_calls and not final_text and last_response is not None:
				final_text = tools_shim.sanitize_assistant_text(
					getattr(last_response, "text", "") or str(last_response))
				# 兜底放行感知: 3 轮打回后仍计划收尾 → 放行计划文本但推看板告警,
				# 让"Agent 停在计划上"的情况在运维侧可见(此前只有 trace 日志,用户无感)
				if _plan_only_last and _has_prior_tool_use:
					trace_logger.warning("[tools] %d 轮打回后仍计划收尾,兜底放行计划文本(pid=%s)", 3, _active_pool_pid)
					_push_alert(
						f"账号 {_active_pool_pid} 检测到连续计划收尾(3 轮拦截失败),已放行计划文本 —— 该任务可能停在计划上未执行",
						"WARNING", _active_pool_pid)

			if AUTO_DELETE_CHAT and hasattr(last_response, "metadata") and last_response.metadata and len(last_response.metadata) > 0:
				cid = last_response.metadata[0]
				asyncio.create_task(background_delete_chat(gemini_client, cid))
			for temp_file in temp_files:
				try:
					os.unlink(temp_file)
				except Exception:
					pass

			if tool_calls:
				message = {"role": "assistant", "content": final_text or "", "tool_calls": tool_calls}
				finish = "tool_calls"
			else:
				final_text = postprocess_text(final_text)
				message = {"role": "assistant", "content": final_text}
				# F9: 库层截断检测 —— max_tokens 触顶时如实上报 length(此前恒 stop,客户端误以为正常收尾)
				if last_response is not None and bool(getattr(last_response, "truncated", False)):
					finish = "length"
				else:
					finish = "stop"

			if request.stream:

				async def tool_stream():
					# 工具结果已在上游生成完毕,这里只是重放;流结束必须归还租借
					try:
						def mk(delta, fr=None):
							return ("data: " + json.dumps({
								"id": completion_id, "object": "chat.completion.chunk",
								"created": created_time, "model": request.model,
								"choices": [{"index": 0, "delta": delta, "finish_reason": fr}],
							}) + "\n\n")
						yield mk({"role": "assistant"})
						if tool_calls:
							for i, tc in enumerate(tool_calls):
								yield mk({"tool_calls": [{"index": i, "id": tc["id"], "type": "function",
														 "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]})
							if final_text:
								yield mk({"content": final_text})
						else:
							if final_text:
								yield mk({"content": final_text})
						yield mk({}, fr=finish)
						yield "data: [DONE]\n\n"
					finally:
						# Token 统计:tools+stream 是 Agent 主用路径,此前完全漏统计(看板不动的根因)
						# F10: 扣除协议注入(非用户/模型内容),避免用量虚高
						try:
							_usage_text = final_text or (json.dumps(tool_calls) if tool_calls else " ")
							_bump_token_usage(_count_tokens(conversation) - _proto_chars, _count_tokens(_usage_text))
						except Exception:
							pass
						# 关键:归还租借。此前缺失导致每次带 tools 的流式请求泄漏 1 个信号量,
						# 8 次后信号量占死,chat/gems 全部排队超时('前几轮OK之后全挂'的根因)
						await release_client()

				return StreamingResponse(tool_stream(), media_type="text/event-stream")

			usage_text = final_text or (json.dumps(tool_calls) if tool_calls else "")
			# F10: 扣除协议注入(非用户/模型内容),避免用量虚高
			_pt = max(1, _count_tokens(conversation) - _proto_chars); _ct = _count_tokens(usage_text)
			_bump_token_usage(_pt, _ct)
			return {
				"id": completion_id, "object": "chat.completion", "created": created_time, "model": request.model,
				"choices": [{"index": 0, "message": message, "finish_reason": finish}],
				"usage": {"prompt_tokens": _pt,
						  "completion_tokens": _ct,
						  "total_tokens": _pt + _ct},
			}

		# 发送前预检(非 tools 路径:stream 与非 stream 共用)
		_blocked = _ctx_guard(conversation)
		if _blocked is not None:
			_cleanup_temp_files(temp_files)
			await _release_now()
			return context_overflow_response(
				len(conversation) if isinstance(conversation, str) else sum(len(p) for p in conversation),
				after_degrade=_degrade_applied)
		_trace_state = {"pid": _active_pool_pid, "chars": len(conversation), "ttfb": []}
		gen_kwargs["_trace_state"] = _trace_state  # 观测器在 _generate 入口 pop,不透传上游

		if request.stream:
			# Real streaming using upstream generate_content_stream
			async def generate_stream():
				try:

					def make_chunk(delta: dict, finish_reason=None):
						return (
							"data: "
							+ json.dumps(
								{
									"id": completion_id,
									"object": "chat.completion.chunk",
									"created": created_time,
									"model": request.model,
									"choices": [
										{
											"index": 0,
											"delta": delta,
											"finish_reason": finish_reason,
										}
									],
								}
							)
							+ "\n\n"
						)

					# Send initial role chunk
					yield make_chunk({"role": "assistant"})

					# Token 统计:累计所有发出的内容(含 thinking 标签/图片 markdown,口径与非流式一致)
					out_parts: list[str] = []

					def emit_content(s: str):
						"""发内容块并累计输出文本(Token 统计用)"""
						out_parts.append(s)
						return make_chunk({"content": s})

					thinking_started = False
					thinking_ended = False
					yielded_images = 0
					text_buffer = ""
					captured_cid = None
					chunk_count = 0
					last_metadata = None

					async for chunk in gemini_client.generate_content_stream(conversation, **gen_kwargs):
						chunk_count += 1
						if hasattr(chunk, "metadata") and chunk.metadata:
							last_metadata = chunk.metadata
						# Capture conversation ID for auto-deletion
						if AUTO_DELETE_CHAT and captured_cid is None and hasattr(chunk, "metadata") and chunk.metadata and len(chunk.metadata) > 0:
							captured_cid = chunk.metadata[0]

						# Handle thinking/thoughts delta
						if _thinking_on and hasattr(chunk, "thoughts_delta") and chunk.thoughts_delta:
							if not thinking_started:
								yield emit_content("<think>\n")
								thinking_started = True

							# Also include reasoning_content for full Open WebUI native compatibility
							yield emit_content(chunk.thoughts_delta)
							# reasoning_content 单独再发一次(不重复计入统计)
							yield make_chunk(
								{
									"content": chunk.thoughts_delta,
									"reasoning_content": chunk.thoughts_delta,
								}
							)

						# Handle text delta
						if hasattr(chunk, "text_delta") and chunk.text_delta:
							# Close thinking tag before first text content
							if thinking_started and not thinking_ended:
								thinking_ended = True
								yield emit_content("\n</think>\n\n")

							text_buffer += chunk.text_delta
							safe_to_yield = False

							# Yield if buffer ends with whitespace and looks like it's outside a markdown link
							if (
								text_buffer[-1].isspace()
								and text_buffer.count("[") == text_buffer.count("]")
								and text_buffer.count("(") == text_buffer.count(")")
							):
								safe_to_yield = True
							elif len(text_buffer) > 500:
								safe_to_yield = True

							if safe_to_yield:
								yield emit_content(postprocess_text(text_buffer))
								text_buffer = ""

						# Handle inline images as they arrive
						if hasattr(chunk, "images") and chunk.images and len(chunk.images) > yielded_images:
							# Close thinking tag if an image arrives before any text
							if thinking_started and not thinking_ended:
								thinking_ended = True
								yield emit_content("\n</think>\n\n")

							new_images = chunk.images[yielded_images:]
							for img in new_images:
								img_url = getattr(img, "url", None)
								if img_url:
									sig = get_image_signature(img_url)
									proxy_url = f"{base_url}/gemini-proxy/image?url={quote(img_url)}&sig={sig}"
									img_md = f"\n\n![🎨 Loading image...]({proxy_url})\n\n"
									yield emit_content(img_md)
							yielded_images = len(chunk.images)

					# Flush any remaining text
					if text_buffer:
						yield emit_content(postprocess_text(text_buffer))

					# 流式结束:计入 Token 统计
					try:
						_stream_out = "".join(out_parts) or " "
						_pt = _count_tokens(conversation)
						_ct = _count_tokens(_stream_out)
						_bump_token_usage(_pt, _ct)
					except Exception:
						pass

					# Close thinking tag if it was never closed
					if thinking_started and not thinking_ended:
						yield make_chunk({"content": "\n</think>\n\n"})

					# Send finish chunk
					yield make_chunk({}, finish_reason="stop")
					yield "data: [DONE]\n\n"

					logger.info(
						"Streaming response completed: chunks=%s images=%s",
						chunk_count,
						yielded_images,
					)
					if last_metadata and len(last_metadata) > 0 and not AUTO_DELETE_CHAT:
						asyncio.create_task(background_verify_chat_persistence(gemini_client, last_metadata[0], "stream"))
				except Exception as e:
					logger.error(f"Error during streaming: {str(e)}", exc_info=True)
					_char_len = len(conversation) if isinstance(conversation, str) else 0
					# 运行时超限:流式已发 role chunk,无法改状态码,
					# 但错误文本会被 DSH 客户端读取分类 → 触发压缩恢复而非 SERVER 重试
					if is_context_overflow_error(e, _char_len):
						_ovf_msg = (f"This model's maximum context length is {GEMINI_REQ_CHAR_LIMIT} characters. "
									f"However, your request is {_char_len} characters long. "
									f"Please reduce the length of the messages and try again. (context_length_exceeded)")
						yield make_chunk({"content": f"\n\n[context_length_exceeded] {_ovf_msg}"})
					else:
						yield make_chunk({"content": "\n\n[An internal error occurred while streaming]"})
					yield make_chunk({}, finish_reason="stop")
					yield "data: [DONE]\n\n"
				finally:
					# 纯流式路径:流真正结束才归还租借(handler 返回响应头时流仍在生成)
					_released["streaming"] = True
					try:
						if gemini_client is not None and not gemini_client._running:
							mark_client_sick(gemini_client)
					except Exception:
						pass
					await release_client()
					# Create background task to delete the chat if AUTO_DELETE_CHAT is enabled
					if AUTO_DELETE_CHAT and captured_cid:
						asyncio.create_task(background_delete_chat(gemini_client, captured_cid))

					# 清理临时文件
					for temp_file in temp_files:
						try:
							os.unlink(temp_file)
						except Exception as e:
							logger.warning(f"Failed to delete temp file {temp_file}: {str(e)}")

			return StreamingResponse(generate_stream(), media_type="text/event-stream")
		else:
			# Non-streaming response
			try:
				response = await gemini_client.generate_content(conversation, **gen_kwargs)

				if AUTO_DELETE_CHAT and hasattr(response, "metadata") and response.metadata and len(response.metadata) > 0:
					cid = response.metadata[0]
					asyncio.create_task(background_delete_chat(gemini_client, cid))
				elif hasattr(response, "metadata") and response.metadata and len(response.metadata) > 0:
					asyncio.create_task(background_verify_chat_persistence(gemini_client, response.metadata[0], "non-stream"))
				elif not getattr(response, "metadata", None):
					logger.warning("Non-stream response returned no Gemini metadata. This request may not map to a persistent Gemini chat.")

			finally:
				# 清理临时文件
				for temp_file in temp_files:
					try:
						os.unlink(temp_file)
					except Exception as e:
						logger.warning(f"Failed to delete temp file {temp_file}: {str(e)}")

			# 提取文本响应
			reply_text = ""
			if _thinking_on and hasattr(response, "thoughts") and response.thoughts:
				reply_text += f"<think>\n{response.thoughts}\n</think>\n\n"
			if hasattr(response, "text"):
				reply_text += response.text
			else:
				reply_text += str(response)

			# 提取并追加图片响应
			reply_text += extract_image_markdown(response, base_url)
			# 净化为对客户端输出前的最终文本(历史含工具结果时防止模型复述/自导自演)
			reply_text = tools_shim.sanitize_assistant_text(reply_text)
			reply_text = postprocess_text(reply_text)
			# F9: json mode 兜底提取(客户端请求 json_object 时尽力返回纯 JSON)
			if (request.response_format or {}).get("type") == "json_object":
				reply_text = _enforce_json_output(reply_text)

			if not reply_text or reply_text.strip() == "":
				logger.warning("Empty response received from Gemini")
				reply_text = "Server returned an empty response. Please check that Gemini API credentials are valid."

			_pt2 = _count_tokens(conversation); _ct2 = _count_tokens(reply_text)
			_bump_token_usage(_pt2, _ct2)

			result = {
				"id": completion_id,
				"object": "chat.completion",
				"created": created_time,
				"model": request.model,
				"choices": [
					{
						"index": 0,
						"message": {"role": "assistant", "content": reply_text},
						"finish_reason": "stop",
					}
				],
				"usage": {
					"prompt_tokens": _pt2,
					"completion_tokens": _ct2,
					"total_tokens": _pt2 + _ct2,
				},
			}

			logger.info("Non-streaming response completed")
			return result

	except HTTPException:
		# 保持 503/400 等语义(已由全局 handler 渲染为 OpenAI 兼容形状)
		await _release_now()
		raise
	except Exception as e:
		# 会话停机类错误(挂起被上游 close/风控):标记该副本不健康,下次请求重建
		try:
			from gemini_webapi.exceptions import APIError
			_sick = isinstance(e, APIError) or "closed" in str(e).lower() or "stream" in str(e).lower() or "unauthenticated" in str(e).lower()
		except Exception:
			_sick = False
		if _sick:
			mark_client_sick(gemini_client)

		_char_len = len(conversation) if isinstance(conversation, str) else 0
		# 运行时超限兜底:预检阈值(95%)与 Google 真实墙(~102 万)之间的灰色地带,
		# 以及带 files 的请求额外占位。映射为 400 context_length_exceeded,
		# 让 Agent 客户端走"压缩后重试",而不是 SERVER 类重试 5 次烧 156 秒。
		if is_context_overflow_error(e, _char_len):
			logger.error("[ctx-guard] 运行时判定上下文超限: chars=%s err=%s", _char_len, str(e)[:200])
			await _release_now()
			_cleanup_temp_files(temp_files)
			return context_overflow_response(_char_len)

		logger.error(f"Error generating completion: {str(e)}", exc_info=True)
		await _release_now()
		# 上游 Gemini 错误(瞬时)→ 502 语义,与代理自身 bug(500)分离
		try:
			from gemini_webapi.exceptions import APIError as _APIError, GeminiError as _GeminiError
			_upstream = isinstance(e, (_APIError, _GeminiError))
		except Exception:
			_upstream = False
		if _upstream:
			raise HTTPException(status_code=502, detail=f"Upstream Gemini error: {str(e)[:300]}")
		raise HTTPException(status_code=500, detail=f"Error generating completion: {str(e)}")
	finally:
		# 兜底归还(幂等;纯流式路径 _release_now 因 streaming 标志跳过,由生成器归还)
		await _release_now()


# 兼容:部分客户端(如 Reasonix)把 Base URL 当作 chat 端点直接 POST 到 /v1
@app.post("/v1", include_in_schema=False)
async def chat_completions_v1_alias(request: ChatCompletionRequest, raw_request: Request, api_key: str = Depends(verify_api_key)):
	return await create_chat_completion(request, raw_request, api_key)


@app.get("/gemini-proxy/image")
async def proxy_image(url: str, sig: str):
	"""
	Proxy images from Google domains to bypass browser security policies.
	Requires a valid HMAC signature.
	"""
	# Verify signature
	expected_sig = get_image_signature(url)
	if not hmac.compare_digest(sig, expected_sig):
		logger.warning(f"Invalid signature for proxy request: {url}")
		raise HTTPException(status_code=403, detail="Invalid signature")

	# Prevent open proxying
	allowed_domains = ["google.com", "googleusercontent.com", "gstatic.com"]

	try:
		parsed = urlparse(url)
		if parsed.scheme not in ["http", "https"]:
			logger.warning(f"Invalid scheme in proxy request: {parsed.scheme}")
			raise HTTPException(status_code=400, detail="Invalid URL scheme")

		hostname = parsed.hostname
		if not hostname:
			logger.warning(f"No hostname in proxy request: {url}")
			raise HTTPException(status_code=400, detail="Invalid URL")

		hostname = hostname.lower()
		is_allowed = any(hostname == d or hostname.endswith("." + d) for d in allowed_domains)

		if not is_allowed:
			logger.warning(f"Blocked proxy request for domain: {hostname}")
			raise HTTPException(status_code=403, detail="Domain not allowed")
	except ValueError:
		logger.warning(f"Malformed URL in proxy request: {url}")
		raise HTTPException(status_code=400, detail="Invalid URL")

	# Minimal browser-like headers
	headers = {
		"User-Agent": DEFAULT_USER_AGENT,
		"Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
		"Accept-Language": "en-US,en;q=0.9",
		"Referer": "https://gemini.google.com/",
	}

	# 10MB limit
	MAX_BYTES = 10 * 1024 * 1024

	# Use scoped cookies to prevent leakage during redirects
	jar = httpx.Cookies()

	# Use the freshest available 1PSIDTS without overriding env cookies up front.
	psid = SECURE_1PSID
	psidts = get_cookie_value(getattr(gemini_client, "cookies", None), "__Secure-1PSIDTS") or load_cached_1psidts(psid) or SECURE_1PSIDTS

	jar.set("__Secure-1PSID", psid, domain=".google.com")
	jar.set("__Secure-1PSIDTS", psidts, domain=".google.com")
	jar.set("__Secure-1PSID", psid, domain=".googleusercontent.com")
	jar.set("__Secure-1PSIDTS", psidts, domain=".googleusercontent.com")

	async with httpx.AsyncClient(http2=True, cookies=jar, follow_redirects=True) as client:
		try:
			# Fetch original resolution to keep watermark at expected size/position
			fetch_url = re.sub(r"=s\d+$", "=s0", url) if re.search(r"=s\d+$", url) else url + "=s0"

			async with client.stream("GET", fetch_url, timeout=15.0, headers=headers) as resp:
				if resp.status_code != 200:
					logger.error(f"Google returned {resp.status_code} for image: {url}")

				resp.raise_for_status()

				content = bytearray()
				async for chunk in resp.aiter_bytes():
					content.extend(chunk)
					if len(content) > MAX_BYTES:
						logger.warning(f"Image too large: {url} (exceeded {MAX_BYTES} bytes)")
						raise HTTPException(status_code=413, detail="Image too large")
				# Validate Content-Type to prevent XSS/MIME sniffing
				upstream_content_type = resp.headers.get("content-type", "image/png").lower()
				if not upstream_content_type.startswith("image/"):
					logger.warning(f"Rejected non-image Content-Type: {upstream_content_type} for {url}")
					media_type = "image/png"
				else:
					media_type = upstream_content_type

				# Process watermark removal
				if media_type in ["image/png", "image/jpeg", "image/webp"]:
					processed_content = remove_gemini_watermark(bytes(content))
				else:
					processed_content = bytes(content)

				return Response(
					content=processed_content,
					media_type=media_type,
					headers={
						"Cross-Origin-Resource-Policy": "cross-origin",
						"Access-Control-Allow-Origin": "*",
						"Cache-Control": "public, max-age=86400",  # Cache for 24 hours
						"X-Content-Type-Options": "nosniff",
					},
				)
		except httpx.HTTPStatusError as e:
			logger.error(f"Failed to fetch image: {e.response.status_code} for {url}")
			raise HTTPException(
				status_code=e.response.status_code,
				detail=f"Failed to fetch image: Google returned {e.response.status_code}",
			)
		except HTTPException:
			raise
		except Exception as e:
			logger.error(f"Proxy error: {str(e)}")
			raise HTTPException(status_code=500, detail="Internal proxy error")


@app.get("/")
async def root():
	"""根路径直接跳转到管理面板。"""
	return RedirectResponse(url="/admin", status_code=307)


if __name__ == "__main__":
	import uvicorn

	uvicorn.run("main:app", host=HOST, port=PORT, log_level="info")
