# Install / Environment Setup Guide

English | [中文](#中文安装指南)

---

## English

### Requirements

- **Python 3.11+** (3.12 recommended)
- Windows 10/11 recommended (the fingerprint browser subsystem is Windows-only; the chat API itself runs on Linux/macOS too)
- A Google account logged into [gemini.google.com](https://gemini.google.com)

### 1. Clone & create virtual environment

```bash
git clone https://github.com/<you>/Gemini-API-Server.git
cd Gemini-API-Server
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

<details>
<summary>Dependency overview (see requirements.txt for pinned versions)</summary>

| Package | Purpose |
|---|---|
| fastapi / uvicorn[standard] | HTTP API server (ports 4444/4445/4446) |
| gemini-webapi 2.1.1 | Gemini web reverse client (cookies auth, media parsing) |
| curl-cffi | TLS fingerprint impersonation for upstream requests |
| httpx | Outbound probes (proxy health, alert dispatch) |
| PySocks / socksio | SOCKS5 egress proxy support |
| loguru / rich | Structured logging (bridge subsystem) |
| pystray / Pillow | Desktop tray icon (launcher UI) |
| python-multipart / aiofiles / websockets | FastAPI extras |
| h2 | HTTP/2 for upstream connections |

</details>

### 3. Configure

```bash
copy .env.example .env      # Windows
cp .env.example .env        # Linux/macOS
```

Fill in `SECURE_1PSID` / `SECURE_1PSIDTS` from your gemini.google.com cookies,
**or** skip this and use the fingerprint browser launcher (step 4) which
captures cookies automatically.

> Set `API_KEY` to your own secret string - it protects `/v1/*` and the admin panel.

### 4. Run

```powershell
# Windows one-click (desktop tray + fingerprint browser launcher)
.\启动项目.bat
# or server-only
.\start.ps1
```

```bash
# Linux / macOS / manual
python gemini_core.py
```

| Service | Address |
|---|---|
| Chat API (OpenAI-compatible) | http://127.0.0.1:4444/v1 |
| Admin dashboard | http://127.0.0.1:4445 |
| Fingerprint browser API | http://127.0.0.1:4446 |

### 5. Docker (chat API only, Linux)

```bash
docker compose up -d
```

---

## 中文安装指南

### 环境要求

- **Python 3.11+**(推荐 3.12)
- 推荐 Windows 10/11(指纹浏览器子系统仅支持 Windows;聊天 API 本身支持 Linux/macOS)
- 一个已登录 [gemini.google.com](https://gemini.google.com) 的 Google 账号

### 1. 克隆并创建虚拟环境

```bash
git clone https://github.com/<you>/Gemini-API-Server.git
cd Gemini-API-Server
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

> 国内网络可用镜像加速:
> `pip install -r requirements.txt -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple`

<details>
<summary>依赖说明(锁定版本见 requirements.txt)</summary>

| 包 | 用途 |
|---|---|
| fastapi / uvicorn[standard] | HTTP 服务(端口 4444/4445/4446) |
| gemini-webapi 2.1.1 | Gemini 网页版反向客户端(Cookie 鉴权、媒体解析) |
| curl-cffi | 上游请求的 TLS 指纹模拟 |
| httpx | 出站探测(代理健康检查、告警推送) |
| PySocks / socksio | SOCKS5 出口代理支持 |
| loguru / rich | 桥接子系统结构化日志 |
| pystray / Pillow | 桌面托盘图标(launcher UI) |
| python-multipart / aiofiles / websockets | FastAPI 扩展 |
| h2 | 上游 HTTP/2 连接 |

</details>

### 3. 配置

```bash
copy .env.example .env      # Windows
cp .env.example .env        # Linux/macOS
```

填入 gemini.google.com 的 `SECURE_1PSID` / `SECURE_1PSIDTS` Cookie,
**或者**跳过此步直接使用指纹浏览器启动器(步骤 4),登录后自动采集 Cookie。

> 请把 `API_KEY` 改成你自己的密钥 —— 它保护 `/v1/*` 接口与管理面板。

### 4. 运行

```powershell
# Windows 一键(桌面托盘 + 指纹浏览器启动器)
.\启动项目.bat
# 或仅启动服务端
.\start.ps1
```

```bash
# Linux / macOS / 手动
python gemini_core.py
```

| 服务 | 地址 |
|---|---|
| 聊天 API(OpenAI 兼容) | http://127.0.0.1:4444/v1 |
| 管理看板 | http://127.0.0.1:4445 |
| 指纹浏览器 API | http://127.0.0.1:4446 |

### 5. Docker(仅聊天 API,Linux)

```bash
docker compose up -d
```

---

## 目录说明

```
Gemini-API-Server/
├── main.py               # OpenAI 兼容 API(核心)
├── gemini_core.py        # 单进程守护:4444(API)/4445(看板)/4446(浏览器桥)
├── admin.py              # 管理面板后端
├── tools_shim.py         # 工具调用协议模拟(<tool_call> 标签协议 + 假执行防线)
├── alert_dispatcher.py   # 外部告警推送(Webhook/Bark/Server酱/Telegram)
├── cookie_bridge.py      # 桥接:指纹窗口 Cookie → 服务端
├── launcher.py           # 桌面启动器(托盘)
├── fingerprint/          # 多账号指纹浏览器子系统
│   └── storage/          # (运行时生成)窗口 profile - 已 gitignore,含敏感数据勿提交
├── secrets/              # (运行时生成)Cookie 缓存/密钥 - 已 gitignore
├── templates/ assets/    # 看板界面
├── requirements.txt      # 依赖锁定
└── .env.example          # 配置模板
```

## 敏感数据提示

以下路径包含个人凭据,已被 `.gitignore` 排除,**切勿提交到公开仓库**:

- `.env`(Google Cookie、API_KEY)
- `secrets/`(Cookie 缓存、代理密钥)
- `fingerprint/storage/`(窗口 profile、浏览器 Cookie)
- `logs/`(运行日志)
