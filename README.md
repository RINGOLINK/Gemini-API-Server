<div align="center">

# Gemini-API-Server

**多账号 Gemini 网页反代 · OpenAI 兼容 API · 指纹浏览器账号池 · 健康路由 · 工具调用协议**

基于 [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) · 致谢上游 [Gemi2Api-Server](https://github.com/zhiyu1998/Gemi2Api-Server)

</div>

---

## 目录

- [这是什么](#这是什么)
- [功能总览](#功能总览)
- [架构](#架构)
- [快速开始](#快速开始)
- [配置参考](#配置参考)
- [API 使用](#api-使用)
- [管理看板](#管理看板)
- [指纹浏览器子系统](#指纹浏览器子系统windows)
- [工具调用与假执行防线](#工具调用与假执行防线)
- [上下文管理](#上下文管理)
- [异常告警推送](#异常告警推送)
- [Docker 部署](#docker-部署)
- [常见问题](#常见问题)
- [声明](#声明)

---

## 这是什么

把 **Gemini 网页版**(`gemini.google.com`)反向封装成 **OpenAI 兼容 API**:
无需官方 API Key、无需付费订阅,使用你已登录网页版的 Google 账号 Cookie 即可获得完整的对话能力。

在基础反代之上,v2.0 内置了一套面向 **Agent 长任务**的完整运营能力:

- **多账号池**:多个 Google 账号组成资源池,额度用尽自动切换,单账号风控不影响整体可用性
- **健康路由**:每个账号按 TTFB / 配额 / 停滞 / 错误四维打分,请求自动路由到健康节点
- **工具调用协议**:让不支持原生函数调用的网页版模型输出标准 OpenAI `tool_calls`,并带七道防线拦截"计划收尾""叙述式执行"等假执行行为
- **上下文管理**:98 万字符预检、LLM 分批压缩、机械裁剪、内容截断四级梯度,Agent 超长上下文不再死亡 400
- **可观测性**:管理看板(健康分/TTFB 走势/配额倒计时/单账号隔离)+ 外部告警推送

```
DSH / Cline / 任意 OpenAI 客户端
        │  OpenAI 协议 (http://127.0.0.1:4444/v1)
        ▼
┌─────────────────────────────┐
│  Gemini-API-Server 守护进程   │
│  ┌─────────┐  ┌──────────┐  │
│  │ 4444    │  │ 4445     │  │
│  │ Chat API│  │ 管理看板  │  │
│  └─────────┘  └──────────┘  │
│  ┌───────────────────────┐  │
│  │ 账号池 · 健康路由 · 熔断 │  │
│  │ 工具协议 · 上下文管理    │  │
│  └───────────────────────┘  │
└──────────────┬──────────────┘
               │ Cookie 鉴权(反向网页协议)
               ▼
        gemini.google.com
```

## 功能总览

**v2.0 亮点**:媒体生成闭环(生图/生视频/生音乐 + 参考输入 + 异步任务化 + 看板媒体工坊)、代理上传回退、「自动删除会话」开关统一接管会话保留、看板启动宽限期与轮询稳定性加固。详见 [CHANGELOG-v2.0](CHANGELOG-v2.0.md)。

### 核心 API

| 能力 | 说明 |
|---|---|
| OpenAI 兼容 | `/v1/chat/completions`(流式 + 非流式)、`/v1/models`,主流 Agent 客户端即插即用 |
| 工具调用 | 完整支持 `tools` / `tool_choice`(auto/required/指定函数/none)/ `parallel_tool_calls` |
| 思考控制 | `thinking: true/false` 与 `reasoning_effort`(low/medium/high)按请求粒度开关 |
| JSON 模式 | `response_format: {"type": "json_object"}` 自动提取首个平衡 JSON 块 |
| 生图内嵌 | 对话中自然触发图片生成,经 HMAC 签名代理返回(见 [API 使用](#api-使用)) |
| Gems | 管理面板创建/激活 Google Gems 作为系统提示词 |
| 自定义模型 | `custom_models.yaml` 覆盖/新增模型名映射 |

### 账号池与健康路由

| 能力 | 说明 |
|---|---|
| 多账号池 | 指纹浏览器窗口 ↔ 服务端账号池实时同步,额度低于阈值自动切换 |
| 四维健康分 | TTFB(40%) + 配额(30%) + 停滞(20%) + 错误链(10%),0~100 分实时计算 |
| 软负载均衡 | 新任务按健康分**加权随机**分配,避免打挂最高分节点 |
| 会话粘性 | 同一 Agent 任务的连续轮次钉住同一账号,保住 Google 服务端 prefill 缓存;账号劣化自动解绑 |
| 阶梯熔断 | 连续 3 次失败 → 30s → 2m → 10m 退避;到期自动半开探针恢复 |
| 静默体检 | 周期探针存活会话,连续失败提前 headless 刷新 Cookie,用户无感 |
| 499 冷却 | 客户端断连的账号进入 60s 粘性冷却,防止同任务反复撞击劣化会话 |

### 代理池健康探测

| 能力 | 说明 |
|---|---|
| 独立出口 | 每个指纹窗口可配置独立 SOCKS5/HTTP 代理,服务端与浏览器走同一出口 |
| 异步探针 | 周期探测"代理 → Google 通道"(与账号凭据无关),多账号共享代理自动去重 |
| 判死联动 | 连败判死 → 路由排除 + 免误杀(业务失败不再计入账号错误链)+ 跳过无效自愈 |
| 自动回池 | 探针恢复 → 自动清除错误链/熔断,账号回池,全程告警可见 |

### 上下文管理

| 层级 | 触发条件 | 行为 |
|---|---|---|
| 预检 | >98 万字符 | 本地秒判,零上游消耗 |
| LLM 分批压缩 | 超限 >5 万字符 | 中段历史按 70 万字符/批交模型压缩成高密度摘要,近 20 万字符原样保留;**摘要缓存**使 Agent 多轮迭代零重复成本;时间预算与断连感知内置 |
| 机械裁剪 | 小幅超限/压缩失败 | 从最旧的非 system 消息裁起,工具调用与结果成对删除 |
| 内容截断 | 最后一条消息本身超限 | 头部截断巨型工具结果/超长粘贴,附省略提示 |
| 拒绝 | 物理极限 | 400 + 明确指引(OpenAI `context_length_exceeded` 形状,客户端可自动压缩重试) |

### 异常告警

| 能力 | 说明 |
|---|---|
| 触发源 | 熔断打开 / 代理判死 / 凭据自愈失败 / 配额不足 / 连续计划收尾 |
| 推送通道 | 通用 Webhook(飞书/钉钉/企微)、Bark、Server酱、Telegram |
| 防轰炸 | 同类告警冷却窗口(默认 300s)去重 |
| 配置 | 看板"⚙ 告警推送设置"弹窗,免重启热生效,一键测试 |

## 快速开始

> 完整环境说明见 **[INSTALL.md](INSTALL.md)**(中英双语)。

```bash
git clone https://github.com/<you>/Gemini-API-Server.git
cd Gemini-API-Server
pip install -r requirements.txt

copy .env.example .env        # Windows(cp .env.example .env)
# 编辑 .env:填入 Cookie,或改用下面的指纹浏览器自动采集
python gemini_core.py
```

启动后:

| 服务 | 地址 |
|---|---|
| Chat API(OpenAI 兼容) | `http://127.0.0.1:4444/v1` |
| 管理看板 | `http://127.0.0.1:4445` |
| 指纹浏览器 API | `http://127.0.0.1:4446` |

最省事的登录方式(仅 Windows):双击 `启动项目.bat` 启动桌面启动器 → 新建浏览器窗口 → 在窗口里登录 Google → Cookie 自动采集推送,`.env` 全程不用手填。

## 配置参考

<details open>
<summary><b>基础配置(.env)</b></summary>

| 变量 | 默认 | 说明 |
|---|---|---|
| `SECURE_1PSID` / `SECURE_1PSIDTS` | - | gemini.google.com 的 `__Secure-1PSID` / `__Secure-1PSIDTS` Cookie |
| `API_KEY` | `Gemi2Api-Server` | 保护 `/v1/*` 与管理面板的访问密钥,**部署必改** |
| `TEMPORARY_CHAT` | `false` | 临时对话模式(禁用思考/图片生成等部分功能) |
| `AUTO_DELETE_CHAT` | `true` | 生成结束自动从网页端删除对话记录 |
| `ENABLE_THINKING` | `false` | 全局默认思考开关(可被每请求 `thinking` 覆盖) |
| `PUBLIC_BASE_URL` | - | 反向代理部署时的外部 URL(图片代理链接用) |
| `CUSTOM_MODELS_FILE` | - | 自定义模型映射 YAML/JSON 路径 |
| `DISABLE_BUILTIN_MODELS` | `false` | 设为 true 时 `/v1/models` 仅返回自定义模型 |
| `GEMINI_PROXY` | - | 全局出口代理(`socks5://user:pass@host:port`),留空直连 |
| `HOST` / `PORT` | - | API 监听地址/端口(Docker 场景) |

</details>

<details>
<summary><b>健康路由与账号池</b></summary>

| 变量 | 默认 | 说明 |
|---|---|---|
| `GEMINI_PATROL_INTERVAL` | `900` | 静默体检巡逻周期(秒) |
| `GEMINI_PATROL_PROBE_FAIL_LIMIT` | `2` | 连续探针失败几次触发自愈 |
| `GEMINI_STALL_TTFB` | `60` | 首块超过该秒数记一次 stall |
| `GEMINI_WATCHDOG_TIMEOUT` | `150` | 生成看门狗(秒) |
| `GEMINI_PER_ACCOUNT_CONCURRENCY` | `8` | 单账号并发信号量 |
| `GEMINI_ACQUIRE_WAIT` | `60` | 取号排队上限(秒),超时自动换号 |
| `GEMINI_AFFINITY_TTL` | `1800` | 会话粘性 TTL(秒) |
| `GEMINI_AFFINITY_MAX` | `64` | 粘性表容量 |
| `GEMINI_AFFINITY_SICK_COOLDOWN` | `60` | 499 后粘性冷却(秒) |

</details>

<details>
<summary><b>代理池探测</b></summary>

| 变量 | 默认 | 说明 |
|---|---|---|
| `GEMINI_PROXY_PROBE_INTERVAL` | `300` | 探测周期(秒) |
| `GEMINI_PROXY_PROBE_TIMEOUT` | `12` | 单次探测超时(秒) |
| `GEMINI_PROXY_PROBE_FAIL_TRIP` | `2` | 连败几次判死 |
| `GEMINI_PROXY_PROBE_URL` | gstatic 204 | 探测端点(需经代理可达) |

</details>

<details>
<summary><b>上下文管理</b></summary>

| 变量 | 默认 | 说明 |
|---|---|---|
| `GEMINI_CTX_CHECK_THRESHOLD` | `980000` | 预检阈值(字符) |
| `CONTEXT_DEGRADE_MODE` | `auto` | 降级梯度总开关 |
| `CONTEXT_COMPRESS_MODE` | `auto` | LLM 压缩开关 |
| `GEMINI_COMPRESS_TAIL_KEEP` | `200000` | 压缩时近端原样保留(字符) |
| `GEMINI_COMPRESS_BATCH` | `700000` | 单批压缩输入上限(字符) |
| `GEMINI_COMPRESS_DIGEST_MAX` | `16000` | 单份摘要上限(字符) |
| `GEMINI_COMPRESS_SMALL_OVER` | `50000` | 超出≤此值走机械裁剪(不花 LLM 调用) |
| `GEMINI_COMPLETION_VERIFY` | `true` | 谎报完成自查开关 |

</details>

<details>
<summary><b>工具协议</b></summary>

| 变量 | 默认 | 说明 |
|---|---|---|
| `PARALLEL_TOOL_CALLS` | `true` | 允许单轮多工具调用 |

</details>

## API 使用

以下示例假设 `API_KEY=sk-xxx`,服务运行于 `http://127.0.0.1:4444`。

### 基础对话

```bash
curl http://127.0.0.1:4444/v1/chat/completions \
  -H "Authorization: Bearer sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-flash",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

### 流式

```bash
curl http://127.0.0.1:4444/v1/chat/completions \
  -H "Authorization: Bearer sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-flash",
    "messages": [{"role": "user", "content": "写一首关于秋天的诗"}],
    "stream": true
  }'
```

### 工具调用(Agent 接入)

```bash
curl http://127.0.0.1:4444/v1/chat/completions \
  -H "Authorization: Bearer sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-flash",
    "messages": [{"role": "user", "content": "看看 C 盘剩余空间"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "run_cmd",
        "description": "执行 shell 命令",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}
      }
    }],
    "tool_choice": "auto",
    "stream": false
  }'
```

返回标准 OpenAI `tool_calls` 形状,客户端执行工具后把结果以 `role: "tool"` 回传即可继续多轮。

### 按需思考

```json
{ "model": "gemini-flash", "thinking": true,  "messages": [...] }
{ "model": "gemini-flash", "reasoning_effort": "high", "messages": [...] }
```

### JSON 模式

```json
{ "model": "gemini-flash", "response_format": {"type": "json_object"}, "messages": [...] }
```

### 生图(对话内嵌)

直接在对话里提出图片需求,模型生成后回复中自动附带经签名代理的图片链接:

```json
{ "model": "gemini-flash", "messages": [{"role": "user", "content": "画一只戴宇航员头盔的橘猫"}] }
```

```markdown
![🎨 Loading image...](http://127.0.0.1:4444/gemini-proxy/image?url=...&sig=...)
```

> 链接带 HMAC 签名与域名白名单,仅本服务可代理,防 SSRF。

### 其他端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/models` | 可用模型列表 |
| GET | `/gemini-proxy/image` | 签名媒体代理 |
| GET | `/admin/api/*` | 管理面板 API(需登录会话) |

## 管理看板

浏览器打开 `http://127.0.0.1:4445`,使用 `API_KEY` 登录。

| 页面 | 能力 |
|---|---|
| 状态看板 | KPI 总览、账号健康分卡片(健康值/熔断灯/配额重置倒计时/TTFB 走势/代理延迟/隔离按钮)、桥日志与服务端日志 |
| 功能设置 | 思考/临时对话/自动删话/并行工具全局开关、出口代理、外部告警推送配置(带测试发送) |
| 系统提示词 | Google Gems 创建/激活/删除、对话快试 |
| 账号管理 | 指纹浏览器窗口增删、内核管理、Cookie 采集推送、账号切换/隔离/余额刷新 |

## 指纹浏览器子系统(Windows)

面向"一台机器跑多个独立 Google 会话"的场景,Windows 桌面启动器内置:

- **指纹窗口**:每窗口独立浏览器指纹(UserAgent/Canvas/字体/时区等)+ 独立 Cookie 存储,避免多账号关联
- **独立代理**:窗口级 SOCKS5/HTTP 出口,与服务端请求同源;看板可实时测试连通性
- **Cookie 采集**:窗口登录 Google 后自动抓取 `__Secure-1PSID/1PSIDTS` 推送服务端,`.env` 免手填
- **静默续期**:Cookie 临近过期自动 headless 刷新
- **健康治理**:窗口离线/账号熔断在看板一目了然,支持单账号隔离排障

> Linux/macOS 下指纹子系统不可用,但聊天 API / 看板 / 手动填 Cookie 的用法完全一致。

## 工具调用与假执行防线

Gemini 网页版没有原生函数调用,本项目用"提示词协议 + 标签包裹 + 校验打回"模拟,并针对长任务中模型退化为自然语言的行为设置了七道防线:

| # | 防线 | 拦截行为 |
|---|---|---|
| 0 | 围栏遮蔽提取 | 代码块里的"示例调用"不会被当成真调用执行 |
| 1 | 破损/回声 | 未闭合标签、协议模板回吐 |
| 2 | 伪造结果/角色错乱 | 自导自演"工具调用结果:"、替 System/User 发言 |
| 3 | 计划收尾(执行中) | 只给计划不给执行 → 打回强制 `<tool_call>` |
| 4 | 计划收尾(第一轮) | 执行型任务第一轮就甩计划 → 打回(用户真要计划则放行) |
| 5 | 叙述式执行 | "我调用了 read_file…"的叙述 → 打回(动词×已声明工具名共现,代码示例不误伤) |
| 6 | 谎报完成自查 | 声称完成但无本轮调用 → 自查打回,显式 `[TASK-COMPLETE]` 才放行 |

打回反馈按次数**升级措辞**(提醒 → 警告 → 最后通牒);3 轮打回仍失败则兜底放行并推送告警,每轮拦截在 trace 日志中留有 `guard=` 痕迹。

## 上下文管理

详见[功能总览](#上下文管理)。两点设计值得说明:

1. **LLM 压缩的"防降智"**:压缩提示词强制摘要中的工具调用保留原生 `<tool_call>` 标签格式 —— 叙述式摘要会成为后续轮次的模仿源,反而诱发假执行。
2. **摘要缓存**:摘要按内容哈希缓存,Agent 多轮迭代时旧段内容不变 → 命中缓存,压缩成本一次性。

## 异常告警

看板 → `⚙ 告警推送设置` → 选通道、填参数、测试、保存。触发源与级别:

| 级别 | 事件 |
|---|---|
| ERROR | 代理判死、凭据自愈失败 |
| WARNING | 熔断打开、连续计划收尾、上下文 LLM 压缩、余额刷新失败 |


## 媒体生成(生图 / 生视频 / 生音乐)

基于 Gemini 网页版的 Imagein / Veo / Lyria 能力,账号池直接承接媒体生成任务。

### OpenAI 兼容生图

```bash
curl http://127.0.0.1:4444/v1/images/generations \
  -H "Authorization: Bearer sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "一只戴宇航员头盔的橘猫", "n": 1, "response_format": "url"}'
```

返回 OpenAI 标准形状 `{"created": ..., "data": [{"url": ...}]}`;`response_format: "b64_json"` 返回 base64。

### 视频(Veo)与音乐(Lyria)

```bash
curl http://127.0.0.1:4444/v1/media/video \
  -H "Authorization: Bearer sk-xxx" -H "Content-Type: application/json" \
  -d '{"prompt": "a golden retriever running through flowers, slow motion", "wait": true}'
```

返回 `{status: "done", files: [{kind: "video", url: "...", bytes: ...}]}`(含缩略图)。音乐同构:`/v1/media/music`。

- **同步模式**(`wait: true`):直接等待生成完成(视频约 1~2 分钟)
- **异步模式**(`wait: false`):立即返回 `job_id`,用 `GET /v1/media/jobs/{job_id}` 轮询
- **文件服务**:`/v1/media/file/{media_id}` 带过期时间的 HMAC 签名链接,可直接嵌入 `<img>/<video>/<audio>`;文件默认保留 24 小时(`GEMINI_MEDIA_TTL_HOURS`)
- **配额说明**:视频/音乐消耗 Google 订阅计划的独立额度(与积分分开);额度不足返回 429 与友好提示

| 变量 | 默认 | 说明 |
|---|---|---|
| `GEMINI_MEDIA_TTL_HOURS` | `24` | 媒体文件保留时长(小时) |
| `GEMINI_MEDIA_TIMEOUT` | `280` | 单次生成超时(秒) |
| `GEMINI_MEDIA_DL_TIMEOUT` | `240` | 单文件下载+轮询超时(秒) |
| `GEMINI_MEDIA_CONCURRENCY` | `2` | 全局媒体生成并发 |

### 参考输入(图生图 / 图生视频首帧)— v2.0 新增

三个媒体端点与看板媒体工坊均支持参考输入,生图/生视频用 `image`,音乐用 `audio`:

```bash
# 图生图: image 传 base64 data-url 或图片 URL(支持数组=多张参考)
curl http://127.0.0.1:4444/v1/images/generations \
  -H "Authorization: Bearer sk-xxx" -H "Content-Type: application/json" \
  -d '{"prompt": "基于参考图生成新图,保持色调", "image": "data:image/png;base64,..."}'

# 图生视频: 参考图作首帧(建议 wait:false 异步)
curl http://127.0.0.1:4444/v1/media/video \
  -H "Authorization: Bearer sk-xxx" -H "Content-Type: application/json" \
  -d '{"prompt": "让画面中的元素自然流动", "wait": false, "image": "data:image/png;base64,..."}'
```

- **代理上传回退**:直连 `content-push.googleapis.com` 的上传在部分网络被运营商掐断;服务自动探测本地代理(`GEMINI_UPLOAD_PROXY` 或 127.0.0.1:12000/7890/10809/1080)经代理上传参考图,生成仍走直连会话(规避代理 IP 风控)
- **视频异步化**:视频建议 `wait: false`——立即返回 `job_id`,任务列表/轮询接口自动跟踪;上游 3 分钟长生成的流式断连不再杀死请求,断流自动重试一次
- **长生成 recovery**:账号 `watchdog_timeout` 300s,断流后从会话历史找回已生成结果

### 会话保留开关(自动删除会话)— v2.0 新增

「功能设置 → 自动删除会话」一个开关统一接管 **chat 与全部媒体任务**:

| 开关 | 行为 |
|---|---|
| **开**(日常推荐) | 任务成功落盘后自动删除 Gemini 会话,网页端不堆积;失败的任务保留会话供排查 |
| **关**(调试) | 全部会话保留,可随时在网页端人工核查每次生成 |

生成期间一律使用持久会话(断流 recovery 需要从历史找回结果),开关动态生效无需重启。

### 看板媒体工坊 — v2.0 新增

管理看板新增「媒体工坊」页:生图/生视频/生音乐统一入口——

- **参考图上传**:多选/追加,缩略图 chip 单张删除;生视频自动异步提交
- **API 接入信息面板**:三端点参数、鉴权、签名下载说明与 curl 示例一键复制
- **最近任务**:自动刷新进度(异步视频实时跟踪),失败展示完整错误,单任务删除

## Docker 部署
## Docker 部署

```bash
cp .env.example .env   # 填入 Cookie 与 API_KEY
docker compose up -d   # API: 4444 · 看板: 4445
```

> 指纹浏览器子系统仅限 Windows 宿主;Linux 容器内聊天 API 与看板完整可用(手动填 Cookie 模式)。

## 常见问题

<details>
<summary><b>请求返回 400 context_length_exceeded?</b></summary>
单次请求内容超过约 98 万字符。服务端会先尝试自动压缩/裁剪;若最后一条消息本身就超限,需要客户端缩减。DSH 类客户端会识别该错误并自动压缩重试。
</details>

<details>
<summary><b>为什么同一个任务总用同一个账号?</b></summary>
会话粘性:同任务的连续轮次固定账号可命中 Google 服务端 prefill 缓存,更快更省。账号劣化/隔离/代理判死时自动解绑换号,也可用 <code>GEMINI_AFFINITY_TTL</code> 调整。
</details>

<details>
<summary><b>模型只输出计划不执行工具?</b></summary>
v1.2 内置七道防线自动拦截打回。若仍遇到,查看 <code>logs/upstream_trace.log</code> 中的 <code>guard=</code> 字段定位防线,欢迎提 issue 附日志。
</details>

<details>
<summary><b>音乐/视频生成?</b></summary>
底层库已支持解析 Gemini 网页版的视频(Veo)与音乐生成结果,端点化在路线图中;当前版本对话内嵌生图可用。
</details>

## 声明

- 本项目仅供学习研究,请遵守 Google 服务条款,勿用于商业用途
- 使用个人 Cookie 存在账号风控风险,请自行评估并小号先行
- 多账号池功能请合理控制规模与频率

## 致谢

- [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) — 核心反向客户端
- [zhiyu1998/Gemi2Api-Server](https://github.com/zhiyu1998/Gemi2Api-Server) — 服务端化与指纹浏览器子系统上游

## License

本项目采用 **[AGPL-3.0](LICENSE)** 许可证。

| 组成部分 | 许可证 | 说明 |
|---|---|---|
| 本项目主体 | AGPL-3.0 | 含网络部署条款:通过网络提供服务同样须提供源码 |
| 衍生自 Gemi2Api-Server 的部分 | MIT(保留原署名) | 按 MIT 条款并入本组合作品 |
| 依赖 [Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) | AGPL-3.0 | 核心反向客户端 |
