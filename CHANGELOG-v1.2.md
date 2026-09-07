# Changelog

## 未发布 / Unreleased (v1.3-dev)

### 新增 / Added

- **媒体生成端点**: `/v1/images/generations`(OpenAI 兼容,url/b64_json)、`/v1/media/video`(Veo)、`/v1/media/music`(Lyria)、`/v1/media/image`;同步(wait=true)与异步(wait=false + job 轮询)双模式
- **签名媒体文件服务**: `/v1/media/file/{media_id}`(HMAC 过期签名,可直接嵌入 <img>/<video>/<audio>),媒体落盘 media_store/ 并 24h TTL 自动清理
- **媒体端点鉴权分级**: 生成类端点走 API_KEY,文件服务走签名(可嵌入网页标签)
# v1.2 更新日志 / Changelog

**发布日期 / Release date:** 2026-09-07

---

## 中文

### 新增

- **健康分路由**:TTFB(40%) + 配额(30%) + 停滞(20%) + 错误链(10%)四维实时评分(0~100),请求自动路由至健康节点;新任务按健康分加权随机分配(软负载),避免打挂最高分账号
- **会话粘性**:同一 Agent 任务的连续轮次钉住同一账号,命中 Google 服务端 prefill 缓存;账号劣化/隔离/代理判死自动解绑,499 断连后 60s 粘性冷却
- **阶梯熔断**:连续 3 次失败 → 30s → 2m → 10m 退避;到期自动半开探针恢复
- **静默体检**:周期探针存活会话,连续失败提前 headless 刷新 Cookie,用户无感
- **代理池健康探测**:每个指纹窗口可配独立 SOCKS5/HTTP 出口;异步探针按"代理→Google 通道"判活,判死自动路由排除并免误杀(业务失败不计入账号错误链),恢复自动回池
- **上下文四级梯度**:98 万字符预检 → LLM 分批压缩(摘要缓存使 Agent 多轮迭代零重复成本) → 机械裁剪 → 最后一条消息内容截断;协议体积计入预算,彻底告别超长上下文死亡 400
- **工具调用七道防线**:围栏遮蔽提取(示例调用不误执行)、破损/回声拦截、伪造结果/角色错乱拦截、计划收尾拦截(执行中+第一轮)、叙述式执行拦截、谎报完成自查;打回反馈升级措辞(提醒→警告→最后通牒),兜底放行推送告警
- **`tool_choice` 完整支持**:`auto` / `required` / 指定函数 / `none`,不支持的形态显式 400
- **`response_format: json_object`**:自动提取首个平衡 JSON 块
- **按需思考**:`thinking` / `reasoning_effort` 请求粒度控制
- **外部告警推送**:熔断/代理判死/凭据失效/配额不足 → Webhook(飞书/钉钉/企微)、Bark、Server酱、Telegram;同类告警防抖去重,看板免重启配置 + 一键测试
- **管理看板 v2**:账号健康分卡片(健康值/熔断灯/配额重置倒计时/TTFB 走势/代理延迟)、单账号隔离、告警横幅、日志双通道
- **指纹浏览器子系统**:窗口独立代理出口与健康探测、Cookie 自动采集推送、headless 静默续期、OpenMedia 品牌全面更名

### 修复

- 超长上下文(约 100 万字符)在估算漏算协议体积时的误拒:协议注入计入预算,正确降级
- 最后一条消息为巨型工具结果时压不回预算的死亡 400:自适应内容截断
- 代码块内的"示例工具调用"被误提取为真实调用执行
- 第一轮计划收尾、叙述式执行("我调用了XX")、谎报完成三类假执行
- 粘性路由绕过隔离/代理判死检查的穿透
- 同账号并发排队 300s 傻等:收敛为 60s 并自动换号承接
- 熔断兜底路径绕过半开纪律:全池熔断时选最早到期探针
- 499 断连僵尸上游工作:竞速取消,不再烧号
- 移除 OpenMedia 迁移遗留:7 条孤儿路由、未使用的 LLM/Vision 配置块、无引用的 ui_panel、下载中心/Skill 市场死 UI

### 下载

- `Gemini-API-Server-v1.2-win64-portable.zip` — Windows 便携一键包(免安装 Python,解压即用)

---

## English

### Added

- **Health-score routing**: real-time 0–100 score per account (TTFB 40% + quota 30% + stalls 20% + errors 10%); new tasks are distributed by weighted-random soft load balancing to avoid hammering the top-scoring account
- **Session affinity**: consecutive turns of the same Agent task stick to one account, preserving Google server-side prefill cache; auto-unbind on degradation/isolation/proxy death, plus a 60s stickiness cooldown after client disconnects (499)
- **Ladder circuit breaker**: 3 consecutive failures → 30s → 2m → 10m backoff; automatic half-open probe recovery
- **Silent patrol**: periodic session probes with proactive headless cookie refresh before credentials expire
- **Proxy pool health probing**: per-window SOCKS5/HTTP egress; async probes verify the "proxy → Google" path; dead proxies are routed around without polluting account health, and accounts auto-repool on recovery
- **Four-tier context management**: 980K-char pre-flight → LLM batch compression (digest cache makes repeated Agent turns free) → mechanical trimming → head-truncation of an oversized last message; protocol size is budgeted — no more hard 400 on long contexts
- **Seven defenses against fake tool execution**: fence-masked extraction (examples never execute), broken-tag/echo interception, fabricated-result and role-confusion interception, plan-only endings (mid-task and first-turn), narrative-only execution, and false-completion self-verification; escalating re-prompt tone and alerting on final fallback
- **Full `tool_choice` support**: `auto` / `required` / named function / `none`, with explicit 400 for unsupported shapes
- **`response_format: json_object`**: extracts the first balanced JSON block
- **On-demand thinking**: per-request `thinking` / `reasoning_effort`
- **External alerting**: circuit-open / proxy-dead / credential-failure / quota events → Webhook (Feishu/DingTalk/WeCom), Bark, ServerChan, Telegram; deduplicated with cooldown, hot-configurable from the dashboard
- **Dashboard v2**: per-account health cards (score/breaker/quota countdown/TTFB sparkline/proxy latency), account isolation, alert banner, dual log channels
- **Fingerprint browser subsystem**: per-window egress proxies with health probing, automatic cookie capture & push, headless silent renewal, full OpenMedia → Gemini-API-Server rebrand

### Fixed

- False rejection of ~1M-char requests caused by tool-protocol size missing from the budget estimate
- Hard 400 when the *last* message itself (e.g. a huge tool result) exceeded the budget: adaptive head-truncation
- Example tool calls inside code fences being extracted and executed as real calls
- Three classes of fake execution: first-turn plan endings, narrative-only "I called X", and false completion claims
- Sticky routing bypassing account isolation / dead-proxy checks
- 300s blind queueing on saturated accounts: now 60s with automatic failover to another account
- Circuit-breaker fallback ignoring the half-open discipline: picks the earliest-due account as a deliberate probe
- Zombie upstream work after 499 client disconnects: raced cancellation, no more credential burning
- Removed OpenMedia migration leftovers: 7 orphan routes, unused LLM/Vision config, unreferenced ui_panel, dead downloads/Skill-market UI

### Download

- `Gemini-API-Server-v1.2-win64-portable.zip` — Windows portable one-click package (no Python install required; unzip and run)

