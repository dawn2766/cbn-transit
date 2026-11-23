# 架构与运行语义（CBN 模型中转服务）

本文描述 `cbn-transit` 的运行组件、请求生命周期、故障处理语义与已知边界。
部署步骤见 [README](../README.md)，逐条排障见 [RUNBOOK](RUNBOOK.md)。

---

## 1. 分层与职责

```
                    ┌─────────────────────────────────────────────┐
                    │ 客户端：DSH / OpenAI SDK / Claude 客户端      │
                    │ baseURL = https://<冻结域名>/v1              │
                    └───────────────────┬─────────────────────────┘
                                        │ HTTPS(443)
┌───────────────────────────────────────▼──────────────────────────────────┐
│ 中转 VPS（SparkChat）                                                      │
│                                                                           │
│  nginx :443  ── /v1/chat/completions ──> imgsanitize.py :9004             │
│              │                                │                          │
│              │                                └─> 上游（同一个 workspace）│
│              ├── /health, /v1/models, /v1/messages ──> 上游（直连）       │
│              └── /ops/register ──> cnb-register.py :9003                  │
│                                        │                                  │
│  cnb-ai-watchdog.sh（cron 每分钟）──> 探活 / 重新注册 / 必要时重启工作区     │
└───────────────────────────────────────┬──────────────────────────────────┘
                                        │ proxy_pass https://$cnb_ai_upstream
                     ┌──────────────────▼──────────────────┐
                     │ CNB 云开发工作区                     │
                     │  node src/server.mjs  :9001         │
                     │  -> https://api.cnb.cool/<org>/<repo>│
                     │     /-/ai-ide/v2/chat/completions    │
                     └─────────────────────────────────────┘
```

| 组件 | 文件 | 职责 |
|---|---|---|
| 请求中继 | [`deploy/imgsanitize.py`](../deploy/imgsanitize.py) | 上传准入、请求体预算、内联图片压缩、`11128` 指纹中和、`11133` 工具配对修复一次重试、上游连接池、SSE 透传 |
| 图片预算 | [`deploy/image_budget.py`](../deploy/image_budget.py) | 按 base64 长度计算，规划并执行图片压缩/省略（Pillow，可选 orjson） |
| 注册接收 | [`deploy/cnb-register.py`](../deploy/cnb-register.py) | 校验工作区 URI → 探测候选 `/health` → 原子改写 nginx `map` → `nginx -t` → reload，失败回滚 |
| 恢复控制器 | [`deploy/cnb-ai-watchdog.sh`](../deploy/cnb-ai-watchdog.sh) | 探活冻结域名与工作区、重新注册、在确认不变代际仍不健康时才停止工作区 |
| 反向代理 | [`deploy/nginx/`](../deploy/nginx) | TLS 终止、路由分流、把 CNB 回收期的 302/gateway 错误转成可重试的 JSON 503 |
| 工作区侧代理 | 外部仓库 [`cnb2dsh`](https://cnb.cool/Dawn2766/cnb2dsh) | 在 CNB 工作区内把上游接口适配成 OpenAI / Anthropic 两种协议 |

> 关键点：**冻结域名不是把 CNB 子域名改死**，而是给自己一个永久别名，持续把别名重新指向当前子域名。CNB 每次回收工作区都会换 `<子域名>-9001.cnb.run`。

---

## 2. 一次补全请求的生命周期

1. **TLS 终止**于 nginx。`location = /v1/chat/completions` 把请求交给 `cnb_imgsanitize`（`127.0.0.1:9004`），关闭所有缓冲以保证 SSE 逐块下发。
2. **上传准入**：中继先按 `Content-Length`（或分块上传的实收字节 ×2）在共享预算 `CNB_RELAY_BODY_BUFFER_BYTES`（默认 512 MiB）中预留额度。
   - 无法容纳 → `413` + `error.max_body_bytes`，提示客户端压缩会话或图片；
   - 并发预留失败 → `503` + `type: relay_busy` + `Retry-After`。
3. **本地预处理**（仅在必要时，两条快路径都可能直接跳过）：
   - 命中客户端指纹（`system` / `developer` / `assistant` 文本中的两处精确子串）→ 替换为等价可读表述；
   - 含内联图片（`image_url`）或体量接近网关预算 → 一次 JSON 解析，按每条/总量预算压缩或从最旧消息开始省略图片；
   - 其余请求**字节级原样透传**，不解析 JSON。
   - 预处理并发由 `BoundedSemaphore(2)` 限制，内存不足时返回 `503`（`type: relay_busy`）而不是拖垮进程。
4. **上游转发**：写入 workspace 的 `https://<子域名>-9001.cnb.run`，并**释放上传额度**，所以长 SSE 响应不会长期占用准入预算。
5. **失败语义**：
   - `400 / code 11133`（工具调用与结果配对不完整）：仅当本地修复确实改变了配对时，**重试一次**；否则原样返回错误。
   - `413`：记录上游给出的真实上限，供后续准入参考。
   - `401` 且上游 `/health` 同时不可用 → 判定为工作区回收期的伪认证失败，转成 `503 upstream_unavailable`。
   - `301/302/303/307/308`（CNB 回收期把请求重定向到登录页）→ 转成 `503 upstream_unavailable`。
   - 任何情况下都**不做传输层或 5xx 的盲目重放**。

---

## 3. 图片预算的设计取向

- 长度以 **base64 字节**为准（与客户端一致），不是解码后的文件大小。
- 只处理请求里内联的图片，**不下载**外部 URL，不做图片缓存、不持久化会话状态。
- 先规划（不需要真的解码就能判断是否超预算），再做**一次**编解码，最后才按“最旧优先”省略。
- 网关 10 MiB 是硬上限，见下节；预算目标是让请求**在到达网关前**就合法，避免浪费一次 400 往返。

---

## 4. 已知硬边界（不要尝试靠配置绕过）

| 边界 | 值 | 说明 |
|---|---|---|
| CNB AI 网关请求体 | **10 MiB，不可配置** | 超出返回 `413 {"errcode":413,"errmsg":"request body too large: X MB, limit 10.00MB"}` |
| 网关内容校验 | `400 / 11128` | 未通过渠道校验；由客户端提示词指纹触发，中继做等价替换，**不重试** |
| 工作区回收 | 约每 18h | 子域名会变；靠 boot 注册 + 5 分钟 cron + 看门狗三者兜底 |
| 中继内存 | 512 MiB 预留预算 | 预算按加权字节估算，不等同于进程 RSS 硬上限 |
| 端口 | 443 必须对公网开放 | 工作区要主动 `POST /ops/register` 回来 |

---

## 5. 故障恢复的三条路径

| 触发者 | 时机 | 动作 | 失败后果 |
|---|---|---|---|
| `start.sh`（工作区启动） | 每次 boot | 自检 `/health` 后立即注册 | 非致命：仅跳过注册，靠 cron 补救 |
| `.cnb.yml` cron | 每 5 分钟 | 列出工作区 → 无运行则启动，有运行则重新注册 | 非致命：看门狗仍在本地独立工作 |
| `cnb-ai-watchdog.sh` | 每分钟 | 探活冻结域名；不健康则查 CNB 并重新注册；连续失败超过阈值才停止并重启工作区 | 唯一被授权停止工作区的组件 |

三者共同的前提是“注册接收端健康且 `REG_TOKEN` 一致”。`cnb-register.py` 在切换前会探测候选的 `/health`，并且 `nginx -t` / reload 任一步失败都会原子回滚。

---

## 6. 与 `cnb2dsh` 的关系

- `cnb2dsh` 是**工作区内**的适配层（OpenAI/Anthropic 双协议、配额 CLI、`.cnb.yml` 流水线）。
- `cbn-transit` 是**工作区外**的中转层（VPS 上的 nginx + 中继 + 注册 + 看门狗）。
- 两者共享同一套契约：上游依赖 `CNB_TOKEN` 与 `CNB_REPO_SLUG`；中继依赖工作区 `/health` 与 `/v1/*` 的行为；注册依赖 `/ops/register` 与 `REG_TOKEN`。
- 如果只想要一个能用的接口、不在意域名漂移，可以完全不要本仓库：直接用工作区打印的 `PROXY_URI`。
