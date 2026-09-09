# v2.0 更新日志 / Changelog

**发布日期 / Release date:** 2026-09-09

v2.0 相比 v1.2 的核心升级:**媒体生成完整闭环**(三端点参考输入 + 代理上传 + 异步任务化)、看板「媒体工坊」、启动与轮询稳定性全面加固、媒体会话保留统一由「自动删除会话」开关接管。

---

## 中文

### 新增

- **媒体参考输入(图生图 / 图生视频首帧)**:三个媒体端点与看板媒体工坊均支持 `image` 字段(base64 data-url 或图片 URL,支持多张),音乐支持 `audio` 参考;后端落盘临时文件经库 `files=` / `req_file_data` 注入
- **代理上传回退**:直连 `content-push.googleapis.com` 的 multipart POST 在部分网络环境被运营商掐断(21s connect timeout)——media_api 自动探测本地代理(`GEMINI_UPLOAD_PROXY` 或 127.0.0.1:12000/7890/10809/1080),参考图经代理会话上传拿到 `ttl_1d` 资源 id,生成仍走直连热会话(规避代理 IP 风控);仅上传阶段失败才回落直连,生成阶段失败直接报告不重传
- **视频异步化**:生视频默认 `wait=false` 异步提交(立即返回 `job_id`),任务列表每 5 秒自动轮询;规避上游 3 分钟长生成的流式断连杀死同步请求;看板与 REST 均可轮询 `GET /api/media/jobs/{job_id}`
- **看板「媒体工坊」**:生图/生视频/生音乐统一页面——多张参考图上传(缩略图 chip、单张删除)、API 接入信息面板(三端点参数与 curl 示例一键复制)、最近任务列表自动刷新
- **看板单任务轮询端点**:`GET /api/media/jobs/{job_id}`(此前只有列表,单查 404)
- **媒体会话保留统一开关**:「功能设置-自动删除会话」一个开关接管 chat 与全部媒体任务——开=成功落盘后清理会话(网页端不堆积,失败保留供排查);关=全部保留(调试);生成期间一律持久会话(断流 recovery 依赖历史可读)
- **看板启动宽限期**:服务启动后 180 秒内看板探测超时放宽至 8s 且失败静默(4444 初始化账号需 20~40s,此前 3s 探测在启动期刷屏)
- **断流自动重试**:媒体生成遇「connection lost / recovery timed out」自动重试一次(上游长生成流式连接偶发中断)
- **VERSION 文件兜底**:打包环境无包元数据时版本号回退读 `VERSION` 文件

### 修复

- **余额刷新按钮卡死**:看板桥全部高频路由(accounts/refresh、accounts、alerts、gems、proxy-health、media/jobs 等)从 150s 超时改走 3s 快速超时——此前刷新期间 4444 稍忙,前端每 2s 的轮询请求悬垂 150s,`refreshing` 翻转永远看不到,按钮永久卡「刷新中…」
- **刷新响应扁平化**:`/api/accounts/refresh` 返回的 `started/refreshing/accounts` 提升到顶层(此前嵌套在 `accounts` 字段里,旧前端判定失败,点击无反应)
- **账号初始化误杀**:validation probe 的历史读回超时(原窗口 12s)不再判死账号——历史写回延迟 ≠ 凭据失效(init 8 RPC + generate 均已成功);读回窗口加宽至 [2,5,10,20],惰性初始化改轻量模式(与启动路径一致,省 5~15s)
- **余额刷新超时匹配**:外层 init 超时 45s → 75s(内部 init 90s×2,45s 必被剪断导致刷新必失败)
- **长视频 recovery 窗口**:账号 `watchdog_timeout` 150s → 300s——带参考视频生成 3.5 分钟,断流后 recovery 轮询窗口需覆盖剩余生成时长
- **媒体会话语义**:参考生成路径曾硬编码临时会话导致①网页端无会话②断流 recovery 读不到历史必超时;现与纯文本路径一致(持久会话)
- **看板 JS 语法错误修复**(v1.2 后期引入):媒体工坊脚本曾因注入损坏整段不执行,看板徽章永远停在「检测中…」;现已 node --check 全量校验
- **启动器残留进程自愈**:端口被占但看板接口未就绪(旧代码残留进程)时,启动器自动清理并重新拉起,不再复用死进程
- **浏览器缓存**:看板全部响应加 `Cache-Control: no-store`,杜绝旧页面缓存假象
- **favicon 404**:看板补 204 静默响应

### 变更

- 看板首页版本徽章 V1.2 → **V2.0**
- 媒体生成走「正规取号」链路(粘性/健康路由/信号量限流),与业务请求同一套账号治理

## English

### Added

- **Media reference input (img2img / img2video first frame)**: all three media endpoints and the dashboard Media Workshop accept an `image` field (base64 data-url or URL, multiple supported); music accepts `audio`
- **Proxy upload fallback**: direct multipart POST to `content-push.googleapis.com` is often blocked (21s timeout) on some networks — media_api auto-detects a local proxy (`GEMINI_UPLOAD_PROXY` or 127.0.0.1:12000/7890/10809/1080), uploads references via a proxy session (ttl_1d resource id), and generates on the direct hot session (avoids proxy-IP risk control); falls back to direct upload only when the upload stage itself fails
- **Async video generation**: video defaults to `wait=false` (instant `job_id`), task list auto-polls every 5s; survives upstream 3-minute stream disconnects that kill synchronous requests
- **Dashboard Media Workshop**: reference-image upload with per-item delete chips, API access info panel with copyable curl examples, auto-refreshing task list
- **Single-job polling endpoint**: `GET /api/media/jobs/{job_id}`
- **Session retention governed by AUTO_DELETE_CHAT**: one switch controls chat and all media tasks — ON: delete conversation after success (web app stays clean; failed jobs keep theirs for debugging); OFF: keep everything. Generation always uses a persistent session so stream-loss recovery can read history
- **Startup grace period**: dashboard probe timeout 8s + silent failures during the first 180s after boot
- **Stream-loss auto-retry** for media generation
- **VERSION file fallback** for packaged environments

### Fixed

- Balance-refresh button hang: all dashboard bridge routes moved from 150s to 3s quick timeout (polls used to hang 150s while 4444 was busy)
- `/api/accounts/refresh` response flattened to top level
- Account init no longer false-killed on slow history read-back; window widened; lazy init now lightweight
- Refresh outer init timeout 45s -> 75s (inner init is 90s x2)
- Account watchdog_timeout 150s -> 300s for long video recovery
- Media session semantics (persistent, matching plain-text path)
- Dashboard JS syntax corruption fixed; full `node --check` enforcement
- Launcher self-heals stale processes (ports open but dashboard not ready)
- `Cache-Control: no-store` on all dashboard responses; favicon 204

### Changed

- Dashboard version badge V1.2 -> **V2.0**
- Media generation now goes through the same account-governance path as chat (sticky/health routing/semaphore)
