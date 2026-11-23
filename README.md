# cbn-transit

> **把 CNB 平台上可用的模型，变成一个稳定、可直连的 OpenAI / Anthropic 兼容 API。**
>
> 本仓库是我自建服务器（SparkChat）上 **CBN 模型中转服务** 的完整代码与部署手册：从 CBN 注册认证、云原生构建任务拉起工作区，到秘钥仓库隔离、固定域名中转，形成一条可复现的链路。
>
> 工作区内的适配层在 **[cnb2dsh](https://cnb.cool/Dawn2766/cnb2dsh)**；本仓库负责它**之外**的那一层。

---

## 目录

- [1. 它解决什么问题](#1-它解决什么问题)
- [2. 能力介绍](#2-能力介绍)
- [3. 整体架构](#3-整体架构)
- [4. 四步获取可用的 CBN 模型 API](#4-四步获取可用的-cbn-模型-api)
  - [4.1 CBN 注册与认证](#41-cbn-注册与认证)
  - [4.2 部署云原生构建任务（工作区 + 工作区代理）](#42-部署云原生构建任务工作区--工作区代理)
  - [4.3 私密秘钥仓库与密钥隔离](#43-私密秘钥仓库与密钥隔离)
  - [4.4 中转服务部署：形成固定域名](#44-中转服务部署形成固定域名)
- [5. 客户端接入](#5-客户端接入)
- [6. 仓库结构](#6-仓库结构)
- [7. 验证与自检](#7-验证与自检)
- [8. 运维、安全与已知边界](#8-运维安全与已知边界)
- [9. 常见问题速查](#9-常见问题速查)
- [10. 许可证与致谢](#10-许可证与致谢)

---

## 1. 它解决什么问题

CNB（<https://cnb.cool>）的云开发工作区可以调用平台内的模型，工作区的端口代理会打印出形如

```
PROXY_URI=https://<子域名>-9001.cnb.run
```

的地址。直接把 `baseURL` 指向它，能跑，但有三个现实问题：

| 问题 | 现象 |
|---|---|
| **地址会漂移** | 工作区每次回收/重启都会换子域名（实测约 18 小时一次），客户端配置随之失效 |
| **网关有内容与体积硬限制** | 10 MiB 请求体上限；部分客户端提示词会触发 `400 / 11128`「未通过渠道校验」；部分内联图片编码会触发 `400 / 11133` |
| **工作区会休眠** | 长时间无活动工作区会停止，接口直接不可用 |

`cbn-transit` 的答案是：**给你一个永远不变的域名**，并在中转层把上面三个问题消化掉。

```
https://api.你的域名/v1        ← 客户端只需要记住这一个地址
        │
        ├── 工作区换了子域名      → 自动重新注册，客户端无感
        ├── 请求体太大/图片太大   → 中转层先压缩、按预算放行，不做无谓的 400
        ├── 提示词被渠道校验拦住  → 等价替换后放行
        └── 工作区被回收          → 看门狗自动重新注册 / 必要时重启，
                                  回收期的重定向转成可重试的 503
```

---

## 2. 能力介绍

### 2.1 固定域名中转（核心）

- **永久别名**：客户端只配置一次 `baseURL`，永不因工作区漂移而修改。
- **自注册**：工作区每次启动都会把自己的 `PROXY_URI` 推送到中转端（`POST /ops/register`），中转端**先探测候选健康**、再原子改写 nginx `map`、`nginx -t`、reload；任何一步失败自动**回滚**到上一份配置。
- **双重兜底**：CNB 侧 5 分钟 cron 会重新注册运行中的工作区；服务器侧 `cnb-ai-watchdog.sh` 每分钟独立探活，且在确认「代际未变化 + 冻结域名与工作区直连都不健康」后才会停止并重启工作区。

### 2.2 请求中继（`imgsanitize.py`）

| 能力 | 说明 |
|---|---|
| **JSON 直通** | 不含图片、未命中指纹的请求**字节级原样转发**，不解析 JSON，保留客户端所有字段 |
| **图片预算** | 按 base64 长度计算，超预算时先规划、再对图片做**一次**编解码，仍超则从**最旧**消息开始省略；目标是让请求在到达 10 MiB 网关前就合法 |
| **提示词指纹中和** | 仅扫描 `system` / `developer` / `assistant` 文本，把两处会触发 `11128` 的精确子串替换为等价可读表述；`user` / `tool` 轮次、工具 schema、图片字节**完全不动** |
| **工具配对修复** | 仅当上游返回 `11133` 且本地修复**确实改变了**调用/结果配对时才重试**一次**，绝不盲目重放 |
| **上传准入** | 共享内存预算（默认 512 MiB）按上传字节预约，无法容纳时返回带 `max_body_bytes` 的 `413`，压力过大返回 `503` + `Retry-After` |
| **连接池 + 流式透传** | 上游连接复用、SSE 逐块下发且不缓冲；长响应期间**释放**上传额度，长对话不会长期占用准入预算 |
| **回收期语义修正** | 把 CNB 回收期出现的 `302` 跳登录页、伪 `401`、gateway 错误转成**可重试的 JSON 503**，客户端退避后即可恢复 |

### 2.3 协议与模型

- **OpenAI 兼容**：`/v1/chat/completions`（含 SSE 流式、工具调用、`reasoning` 字段）、`/v1/models`。
- **Anthropic 兼容**：`/v1/messages`、`/v1/messages/count_tokens`（工作区代理侧完成协议转换）。
- **鉴权**：客户端统一使用你自己生成的 `PROXY_KEY`（`Authorization: Bearer …` 或 `x-api-key`）；上游凭证 `CNB_TOKEN` 始终留在工作区内，不进入客户端。
- 模型清单是「实测清单 + 被动嗅探名」的并集，**不是**实时权限校验；不可用的模型会把上游错误原样返回，不会静默替换成别的模型。

### 2.4 不做什么（避免误解）

- ❌ 不绕过鉴权、额度或模型权限——这些始终由 CNB 上游决定。
- ❌ 不提升模型的上下文窗口；`max_tokens` 上限与上下文是两件事。
- ❌ 不承诺带宽或可用性 SLA；个人服务器 + 免费额度的组合，请按「尽力而为」看待。
- ❌ 不缓存或持久化你的对话与图片；中转层无会话状态。

---

## 3. 整体架构

```
客户端（DSH / OpenAI SDK / Claude 客户端）
   │  baseURL = https://<冻结域名>/v1        + Authorization: Bearer <PROXY_KEY>
   ▼
┌──────────────── 中转 VPS（本仓库） ─────────────────┐
│ nginx :443                                          │
│   ├─ /v1/chat/completions → imgsanitize.py :9004 ───┼─┐ 中转层
│   ├─ /health /v1/models /v1/messages → 直连上游 ────┼─┤
│   └─ /ops/register → cnb-register.py :9003          │ │
│ cnb-ai-watchdog.sh（cron 每分钟）：探活/重注册/重启    │ │
└─────────────────────────────────────────────────────┘ │
   ▼  proxy_pass https://$cnb_ai_upstream                 │
┌──────────────── CNB 云开发工作区（cnb2dsh）──────────┐◄┘
│ node src/server.mjs :9001                           │  工作区
│   → https://api.cnb.cool/<org>/<repo>/-/ai-ide/v2/  │  适配层
│     chat/completions   （携带 CNB_TOKEN）            │
└─────────────────────────────────────────────────────┘
```

详细请求生命周期、故障语义与硬边界见 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**。

---

## 4. 四步获取可用的 CBN 模型 API

> 下面四步是**完整链路**。只想要「能用」而不在乎域名漂移，可以跳过 4.4，直接用工作区打印的 `PROXY_URI`。

### 4.1 CBN 注册与认证

**目标**：拿到一个可用的 CNB 账号与组织，并理解认证边界。

1. **注册与加入组织**
   - 在 <https://cnb.cool> 注册账号，创建一个**组织**（organization）。
   - 后续的「代码仓库」与「秘钥仓库」都放在这个组织下。

2. **确认 AI 权限与额度**
   - 工作区的上游接口是
     `https://api.cnb.cool/<org>/<repo>/-/ai-ide/v2/chat/completions`。
   - 调用它需要**工作区网络环境** + 流水线注入的 `CNB_TOKEN`——这是**平台凭证**，不是你在页面上创建的 API Key。
   - 平台按 **AI credits / 开发核时 / CI 核时** 分别计量，三者的额度**互不通用**。实测参考：一个 2 核工作区常驻约 `2核 × 24h = 48 核时/天`。

3. **区分三类密钥（最容易搞混的地方）**

   | 密钥 | 谁用 | 在哪里 | 能否外泄 |
   |---|---|---|---|
   | `CNB_TOKEN`（流水线注入） | 工作区进程 | 由平台注入，**不要手动分发** | 不能 |
   | `PROXY_KEY`（你生成） | 你的客户端 | 秘钥仓库 → 工作区进程 | 等同 API Key，泄露需轮换 |
   | `REG_TOKEN`（你生成） | 工作区 → 中转端注册 | 秘钥仓库 **与** 中转端 `register.env` | 泄露会被人篡改上游指向 |
   | CNB 个人访问令牌 | 看门狗调用 CNB API | 中转端 `/etc/cnb-ai-watchdog.env` | 不能，权限较大 |

   > 生成方式统一用：`openssl rand -hex 24`（`PROXY_KEY` 习惯加 `sk-` 前缀）。

4. **确认默认分支**
   后续 `.cnb.yml` 中的 cron 写在哪个分支键下，就决定了定时任务是否生效。若默认分支不是 `main`，要么改名键，要么改分支，否则 cron **静默不触发**。

### 4.2 部署云原生构建任务（工作区 + 工作区代理）

**目标**：让 CNB 自动拉起一个常驻工作区，并在其中运行模型代理。

> 这一步的代码就是 **[cnb2dsh](https://cnb.cool/Dawn2766/cnb2dsh)**。完整细节见它的
> [`docs/SETUP.md`](https://cnb.cool/Dawn2766/cnb2dsh/-/blob/main/docs/SETUP.md)。

1. **复制仓库**
   把 `cnb2dsh` 复制（或 fork）到你的组织下，例如 `你的组织/cnb2dsh`。

2. **`.cnb.yml` 的两个任务块**

   | 任务 | 触发 | 作用 |
   |---|---|---|
   | `$: vscode:` | 页面点「云开发」时 | 启动工作区，执行 `deploy/start.sh` 拉起 `node server.mjs`，打印 `PROXY_URI` |
   | `main: "crontab: */5 * * * *":` | 每 5 分钟 | 检查是否有运行中的工作区；没有则启动，有则重新注册（非破坏性） |

   关键片段（简化自仓库原文）：

   ```yaml
   main:
     "crontab: */5 * * * *":
       - name: keepalive
         docker:
           image: cnbcool/default-dev-env:latest
         imports:
           - https://cnb.cool/<你的组织>/<秘钥仓库>/-/blob/main/proxy.yml
         env:
           REPO: $CNB_REPO_SLUG
           REGISTER_URL: https://<冻结域名>/ops/register
         stages:
           - name: ensure-workspace
             script: |
               # workspace/list → 无运行则 start；有运行则 POST /ops/register
               ...

   $:
     vscode:
       - docker:
           image: cnbcool/default-dev-env:latest
         imports:
           - https://cnb.cool/<你的组织>/<秘钥仓库>/-/blob/main/proxy.yml
         env:
           REGISTER_URL: https://<冻结域名>/ops/register
         runner:
           cpus: 2
         services:
           - name: vscode
             options:
               keepAliveTimeout: 18h     # 让代理自身的连接充当心跳，避免约 10 分钟被回收
           - docker
         stages:
           - name: start-ai-proxy
             script: |
               set -euo pipefail
               bash "$(pwd)/deploy/start.sh"
   ```

3. **启动与确认**
   推送后在仓库页启动「云开发」，构建日志的 `start-ai-proxy` 阶段应依次出现：

   ```
   ===== proxy self-check: health OK =====
   ===== models =====
   PROXY_URI=https://<子域名>-9001.cnb.run
   ===== registered upstream to relay =====        ← 配好中转后才有
   ```

   若显示 `register skipped (REGISTER_URL / REG_TOKEN / PROXY_URI missing)`，说明秘钥没有 import 进来（见 4.3）。
   **注册失败是非致命的**（cron 会兜底），不要为了好看把它改成 `exit 1`。

4. **此时可以先验证工作区代理直连**

   ```bash
   curl https://<子域名>-9001.cnb.run/v1/models -H "Authorization: Bearer $PROXY_KEY"
   ```

### 4.3 私密秘钥仓库与密钥隔离

**目标**：真实凭据**一个字节都不进入代码仓库**，且只允许指定仓库读取。

1. **新建秘钥仓库**：另建一个**私有**仓库，例如 `你的组织/cnb-secrets`，根目录放 `proxy.yml`：

   ```yaml
   PROXY_KEY: "sk-<openssl rand -hex 24>"
   REG_TOKEN: "<与中转端 /root/.cnb/register.env 完全一致>"
   REGISTER_URL: "https://<冻结域名>/ops/register"
   ```

2. **限制导入范围**：秘钥仓库设置 → 保护/导入范围 → `allow_slugs: 你的组织/cnb2dsh`。
   否则任何仓库都能引用你的密钥。

3. **在 `.cnb.yml` 里只用 `imports:`**

   > ⚠️ **最容易踩的坑**：`env:` 会**静默覆盖** `imports:`。
   > 同一个凭据如果两处都写，流水线看起来一切正常，但明文已经进了 git 历史。
   > 规则：**凭据只出现在 imports，绝不出现在 env。** `env:` 只放非敏感项（如 `REGISTER_URL`、`REPO`）。

4. **推送前自检（工作区 + 历史都要查）**

   ```bash
   git grep -nE 'sk-[0-9a-f]{16,}|REG_TOKEN:\s*\S|CNB_TOKEN:\s*\S' -- . || echo clean
   git log -p -- .cnb.yml | grep -E 'sk-[0-9a-f]{16,}' && echo "历史已泄露 → 直接轮换密钥"
   ```

   代码仓库的可见性不能靠 OpenAPI 改，需在「仓库设置 → 通用设置 → 可见性」手动确认。
   **不确定就当作公开**：一旦历史里出现过凭据，轮换比纠结「有没有被抓走」便宜得多。

### 4.4 中转服务部署：形成固定域名

**目标**：把漂移的工作区地址，包装成 `https://<冻结域名>`。

#### 前置条件

- 一个**域名**（DNS 可编辑）和一台**公网 VPS**（本部署跑在 SparkChat 上，2 核 / 1.6 GB 即可）。
- VPS 的 **443 端口对公网开放**——工作区要主动 `POST https://<冻结域名>/ops/register` 回来。
- VPS 需装 `nginx`、`python3`、`pillow`（图片预算必需，`orjson` 可选加速）。

DNS 先把 `A` 记录指向 VPS IP，等解析生效。

#### 一键安装

```bash
git clone https://github.com/dawn2766/cbn-transit.git && cd cbn-transit
sudo ./install.sh --domain ai.example.com --email you@example.com
```

`install.sh` 会依次完成：装代码到 `/opt/cnb-imgre` → 生成并写 `REG_TOKEN`（**屏幕会打印一次，请立刻抄进秘钥仓库**）→ 安装两个 systemd 服务 → 渲染 nginx vhost（域名替换自动完成）→ 申请证书 → 启动并自检。

#### 手动安装（等价步骤）

<details>
<summary>展开查看完整命令</summary>

```bash
# 1) 中继代码
sudo install -d /opt/cnb-imgre
sudo install -m 755 deploy/imgsanitize.py  /opt/cnb-imgre/imgsanitize.py
sudo install -m 644 deploy/image_budget.py /opt/cnb-imgre/image_budget.py
sudo install -m 644 deploy/systemd/cnb-imgsanitize.service /etc/systemd/system/

# 2) 注册接收服务
sudo install -m 755 deploy/cnb-register.py /usr/local/bin/cnb-register.py
sudo install -m 644 deploy/systemd/cnb-register.service /etc/systemd/system/
sudo mkdir -p /root/.cnb
printf 'REG_TOKEN=%s\n' "$(openssl rand -hex 24)" | sudo tee /root/.cnb/register.env
sudo chmod 600 /root/.cnb/register.env      # ← 记下这个值

# 3) nginx：先建 map 文件，再建 vhost（顺序不能反，否则 nginx -t 报 unknown variable）
sudo install -m 644 deploy/nginx/conf.d/cnb-ai-upstream.conf      /etc/nginx/conf.d/
sudo install -m 644 deploy/nginx/conf.d/cnb-imgsanitize.conf      /etc/nginx/conf.d/
sudo install -m 644 deploy/nginx/conf.d/cnb-register-ratelimit.conf /etc/nginx/conf.d/
sudo sed 's/ai\.example\.com/你的域名/g' deploy/nginx/sites-available/ai.example.com \
  | sudo tee /etc/nginx/sites-available/你的域名
sudo ln -sfn /etc/nginx/sites-available/你的域名 /etc/nginx/sites-enabled/你的域名

# 4) 证书（二选一）
sudo certbot --nginx -d 你的域名                 # Let's Encrypt
# 或：Cloudflare Origin 证书放进 /etc/nginx/ssl/，并把 vhost 的 ssl_certificate 指过去

# 5) 启动
sudo systemctl daemon-reload
sudo systemctl enable --now cnb-register cnb-imgsanitize
sudo nginx -t && sudo systemctl reload nginx
```

</details>

#### vhost 里四个不能省的配置

| 配置 | 作用 | 漏掉的后果 |
|---|---|---|
| `resolver …` | `proxy_pass` 用了**变量**，需运行时解析 | 全部 `502`，日志 `no resolver defined` |
| `proxy_ssl_server_name on` | 正确 SNI | 上游握手失败 |
| `proxy_buffering off` | SSE 逐块下发 | 流式响应卡住/一次性吐出 |
| `client_max_body_size 8m` | 大于工作区自身上限 | nginx 先返回 HTML 413 |

另外，`location = /v1/chat/completions` 必须指向 `http://cnb_imgsanitize`，否则**图片预算、指纹中和、配对修复全部失效**。

#### 接线：让工作区把新地址报给中转端

在秘钥仓库的 `proxy.yml` 里把 `REGISTER_URL` 设为 `https://<冻结域名>/ops/register`，`REG_TOKEN` 与 `/root/.cnb/register.env` **保持一致**。下一次工作区启动就会自动注册。

#### 可选但强烈建议：看门狗

```bash
sudo cp deploy/env/cnb-ai-watchdog.env.example /etc/cnb-ai-watchdog.env
sudo chmod 600 /etc/cnb-ai-watchdog.env
# 填写 CNB_REPO / FIXED_URL / REGISTER_URL / CNB_TOKEN
sudo install -m 755 deploy/cnb-ai-watchdog.sh /usr/local/bin/cnb-ai-watchdog.sh
echo '* * * * * /usr/local/bin/cnb-ai-watchdog.sh >/dev/null 2>&1' | sudo crontab -
```

> 看门狗是**唯一被授权停止工作区**的组件，并且只有在「代际未变化 + 冻结域名与工作区直连都不健康」时才动手。
> 若 `FIXED_URL` / `REGISTER_URL` / `CNB_REPO` / `CNB_TOKEN` 任一缺失，它会记录 `CONFIG:` 后直接退出（退出码 78），**不会**把「配置缺失」误判成「工作区死亡」。

---

## 5. 客户端接入

### DSH（DeepSeek Harness）

在 provider 配置里指向固定域名即可（Anthropic 或 OpenAI 两种接法都支持）：

```jsonc
{
  "openai": {
    "baseURL": "https://<冻结域名>/v1",
    "apiKey":  "sk-<你的 PROXY_KEY>",
    "model":   "deepseek-v4.1-flash"
  }
}
```

> DSH 的桌面版系统提示词包含两处会被网关判为「未通过渠道校验」的指纹，**必须经由本仓库的中转层**才能正常调用。
> 若把 `baseURL` 直接指向工作区 `PROXY_URI`，会收到 `400 / 11128`。
> 实测（2026-11-23）：DSH 经 `POST /v1/chat/completions` 连续调用均返回 `200`，中转日志同步记录
> `neutralized client fingerprint tokens=4`，即每轮有 4 处指纹被等价替换。

### OpenAI SDK

```python
from openai import OpenAI
client = OpenAI(base_url="https://<冻结域名>/v1", api_key="sk-...")
print(client.chat.completions.create(
    model="deepseek-v4.1-flash",
    messages=[{"role": "user", "content": "hi"}],
).choices[0].message.content)
```

### Anthropic SDK / Claude 客户端

Anthropic 协议使用**不带** `/v1` 的源站地址（路径本身自带 `/v1/messages`）：

```python
from anthropic import Anthropic
client = Anthropic(base_url="https://<冻结域名>", api_key="sk-...")
```

### curl

```bash
curl https://<冻结域名>/v1/chat/completions \
  -H "Authorization: Bearer $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"hi"}],"stream":false}'
```

---

## 6. 仓库结构

```
cbn-transit/
├── install.sh                      # 一键安装/更新中转服务（幂等）
├── smoke-test.sh                   # 分层自检：健康→鉴权→补全→流式→Anthropic
├── deploy/
│   ├── imgsanitize.py              # 请求中继：预算/图片/指纹/配对修复/连接池
│   ├── image_budget.py             # 图片压缩与省略策略（Pillow）
│   ├── cnb-register.py             # 注册接收：探活→原子改 map→nginx -t→reload，失败回滚
│   ├── cnb-ai-watchdog.sh          # 恢复控制器：探活/重注册/受控重启
│   ├── systemd/                    # 两个 systemd 单元
│   ├── nginx/
│   │   ├── conf.d/                 # 上游 map、中继 upstream、注册限流
│   │   └── sites-available/ai.example.com   # vhost 模板（替换域名即可）
│   └── env/                        # 配置模板（含密钥项，仅模板入库）
├── docs/
│   ├── ARCHITECTURE.md             # 请求生命周期、故障语义、硬边界
│   └── RUNBOOK.md                  # 部署后运维与排障手册
├── .gitignore                      # 屏蔽密钥、证书、运行时状态
├── .gitattributes                  # 统一 LF，避免 Windows 换行破坏脚本
└── LICENSE                         # MIT
```

---

## 7. 验证与自检

```bash
BASE_URL=https://<冻结域名> PROXY_KEY=sk-... MODEL=deepseek-v4.1-flash ./smoke-test.sh
```

| 步骤 | 验证什么 | 失败含义 |
|---|---|---|
| 1 `/health` | 中转端自身存活 | nginx / 系统服务问题，与 CNB 无关 |
| 2 `/v1/models` | 中转 → 工作区 → 上游连通 | 上游未注册、工作区未运行，或 `PROXY_KEY` 不对 |
| 3 错误密钥 | 鉴权确实生效 | 出现 `200` 说明鉴权被绕过，立即停服排查 |
| 4 补全 | 全链路可用 | 看响应体里的 `errcode` 定位是内容还是额度问题 |
| 5 流式分块 | nginx 未缓冲 | 检查 `proxy_buffering off` |
| 6 Anthropic | 协议转换可用 | 客户端若只用 OpenAI 可忽略 |

预期输出（生产环境实测）：

```
1. relay alive            PASS  GET /health -> 200
2. relay -> workspace     PASS  GET /v1/models -> 200
3. auth is enforced       PASS  wrong key -> 401
4. end-to-end completion  PASS  POST /v1/chat/completions -> 200
5. streaming not buffered PASS  received 17 SSE chunks
6. Anthropic format       PASS  POST /v1/messages/count_tokens -> 200
7 passed, 0 failed
```

---

## 8. 运维、安全与已知边界

### 8.1 硬边界（不要试图靠配置绕过）

| 边界 | 值 | 说明 |
|---|---|---|
| CNB AI 网关请求体 | **10 MiB，不可配置** | 超出返回 `413 {"errcode":413,…,"limit 10.00MB"}`；中转层所有上限都远高于它 |
| 渠道校验 | `400 / 11128` | 由提示词指纹触发；中转层做等价替换，**不重试** |
| 工作区回收 | 约 18h | 靠 boot 注册 + 5 分钟 cron + 看门狗三重兜底 |
| 每日额度 | AI credits / 开发核时 / CI 核时 | 三者独立计量，模型价格可能调整，不做「每月固定 token 量」承诺 |

### 8.2 密钥卫生

- 该进秘钥仓库的：`PROXY_KEY`、`REG_TOKEN`、`REGISTER_URL`。
- 该只留服务器上的：`/root/.cnb/register.env`、`/etc/cnb-ai-watchdog.env`（含 CNB 个人令牌），均 `chmod 600`。
- 本仓库的 `.gitignore` 已屏蔽 `*.env`、`register.env`、`*.pem`、`*.key` 等；**请勿用 `git add -f` 强行添加**。
- **轮换顺序**：生成新值 → 更新秘钥仓库 → 更新中转端 `register.env` 并 `systemctl restart cnb-register` → 热重启工作区代理 → 更新客户端 → 验证「新密钥 200 / 旧密钥 401」。

### 8.3 日常巡检

```bash
curl -s https://<冻结域名>/health                          # 别名是否存活
sudo cat /etc/nginx/conf.d/cnb-ai-upstream.conf            # 当前指向哪个子域名
sudo journalctl -u cnb-register -n 20 --no-pager           # 注册记录
tail -n 20 /var/log/cnb-ai-watchdog.log                    # 恢复记录
sudo systemctl is-active nginx cnb-register cnb-imgsanitize
```

### 8.4 已知薄弱点（诚实记录）

- **DNS 解析在请求时进行**：VPS 的 resolver 抖动会直接表现为 `502`，实测出现过上游域名解析超时，看门狗已自动恢复。
- **中继内存预算是「准入估算」，不是 RSS 硬上限**：请按 VPS 实际内存调整 `CNB_RELAY_BODY_BUFFER_BYTES`。
- **注册与恢复都不保证 100% 在线**：两个组件是「尽力自愈」，极端情况下仍需人工介入（见 RUNBOOK）。
- **certbot 续期可能改写 vhost**：续期后务必确认 `resolver` 与 `proxy_pass https://$cnb_ai_upstream;` 仍在，并跑一次 `--dry-run`。

---

## 9. 常见问题速查

| 现象 | 最可能的原因 | 处理 |
|---|---|---|
| 构建日志 `register skipped (…)` | `imports:` 没生效或变量名与 `proxy.yml` 不一致 | 核对秘钥仓库文件名与键名 |
| `POST /ops/register` → `403` | 两处 `REG_TOKEN` 不一致 | 对齐秘钥仓库与 `register.env` |
| `POST /ops/register` → `400 uri not match` | URI 格式不对（含路径/大写/尾斜杠） | 用平台给的 `CNB_VSCODE_PROXY_URI` 原值 |
| 冻结域名 `502` | 上游 map 指向旧子域名，或 DNS 抖动 | 看 `access/error.log`，等 cron/看门狗重注册 |
| `502` + `no resolver defined` | vhost 缺 `resolver` | 补上并 reload |
| `400 / 11128` | 请求绕过了中继层 | 确认 `/v1/chat/completions` 指向 `cnb_imgsanitize` |
| `413` 且为 HTML | nginx `client_max_body_size` 太小 | 调到 `8m` |
| `413 … limit 10.00MB` | 撞上网关硬上限 | 减少图片/压缩历史，中转层会自动压缩但仍有限 |
| 流式一次性返回 | `proxy_buffering` 未关 | 关闭并 reload |
| 工作区每 ~10 分钟被回收 | `keepAliveTimeout` 未提高 | 设为 `18h` |
| cron 从不触发 | cron 键不在默认分支，或仓库长期休眠 | 改分支键/定期提交 |

更完整的逐层排障见 **[docs/RUNBOOK.md](docs/RUNBOOK.md)**。

---

## 10. 许可证与致谢

本项目以 [MIT](LICENSE) 许可发布。

- 工作区适配层：**[cnb2dsh](https://cnb.cool/Dawn2766/cnb2dsh)**（OpenAI/Anthropic 协议适配、配额 CLI、`.cnb.yml` 流水线）。
  本仓库的 `cnb-register.py`、`cnb-ai-watchdog.sh` 与其同源，`imgsanitize.py` / `image_budget.py` 则是在其基础上按生产环境演化出的版本。
- 本仓库中的代码取自实际运行中的 SparkChat 中转服务，并做了三处工程化处理：
  1. **脱敏**：移除真实域名、工作区子域名、个人令牌与共享密钥，改为模板/占位符；
  2. **修复**：修正 vhost 中 `/v1/chat/completions` 的 `return 418` 遗留分支（该分支会导致请求被 nginx 直接以 418 拒绝）；
  3. **加固**：给看门狗补上必填配置校验，避免「配置缺失」被误判为「工作区故障」而触发停机。

> 请注意：CNB 的接口、额度与内容策略由平台决定并可能随时调整；本仓库只做兼容与中转，不代表平台承诺。
