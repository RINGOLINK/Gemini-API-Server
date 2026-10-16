# Changelog v2.1

**发布日期 / Release date:** 2026-10-17

v2.1 adds account-aware dynamic discovery and routing for newly launched Gemini web models, including Gemini 4 Argon when the signed-in account exposes it in Gemini's model registry.

## 中文

### 新增

- **Gemini 4 Argon 网页模型适配**：`/v1/models` 除兼容旧别名外，动态返回当前登录账号网页端实际可用的模型、展示名和别名；Gemini 4 Argon 出现在账号模型注册表后，可按真实模型 ID 调用，也支持 `gemini-4-argon` / `gemini-4-argon-latest` 友好别名
- **按请求账号精确解析**：Chat 请求用当前租用账号的 `client.resolve_model()` 解析真实动态模型对象及其运行时模型头，不硬编码 Google 内部 model ID
- **避免假成功**：显式请求 `gemini-4-*` / `argon` 但当前账号模型列表未发现时，返回清晰 HTTP 400 与该账号当前可用模型列表，不再静默回落到 Flash/Pro
- **升级版本标记**：界面与 `VERSION` 更新为 v2.1

### 兼容性与验证说明

- 当前依赖 `gemini_webapi 2.1.1` 已是 PyPI 最新版，支持从 Gemini 网页会话动态发现模型并按 `model_id` 构造请求头，无需依赖库更新/猜测内部 ID
- Google 官方于 2026-10-13 发布 Gemini 4 Argon，并宣布向 Gemini app 的 Google AI Pro / Ultra 用户开放；网页反代能否选择取决于账号当前网页会话返回的动态模型注册表
- **实测通过(2026-10-17)**:刷新 Gemini 网页 Cookie 后，`GET /v1/models` 返回 `id=gemini-argon`, `display_name=4 Argon`；通过本项目 `model=gemini-4-argon` 发起真实 chat completion，HTTP 200，约 45.4s 返回 `GEMINI4_ARGON_OK`。

## English

### Added

- **Gemini 4 Argon web-model support**: `/v1/models` now includes models, display names, and aliases dynamically discovered from the signed-in Gemini web account. When Argon is exposed to that account, call its real discovered ID or the friendly aliases `gemini-4-argon` / `gemini-4-argon-latest`.
- **Per-request account-aware routing**: chat requests resolve the requested name via the leased account's `client.resolve_model()`, using its runtime model object and generated headers instead of hard-coded Google internal IDs.
- **No silent downgrade**: an explicit Gemini 4/Argon request returns HTTP 400 with the current account's available model names if Argon is not actually present; it will not silently run on Flash/Pro.
- UI badge and `VERSION` updated to v2.1.

### Compatibility and verification

- Installed `gemini_webapi 2.1.1` is the latest PyPI release and already supports runtime model discovery and model-ID-based headers.
- Google announced Gemini 4 Argon on 2026-10-13 and says it is available in the Gemini app to Google AI Pro / Ultra subscribers; web-proxy availability depends on the model registry returned to the signed-in account.
- **Live verification passed (2026-10-17)**: after refreshing the Gemini web Cookie, `GET /v1/models` returned `id=gemini-argon`, `display_name=4 Argon`; a real chat completion using `model=gemini-4-argon` returned HTTP 200 in about 45.4s with `GEMINI4_ARGON_OK`.
