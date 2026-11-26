# cbn-transit

把 CNB 平台的模型转成一个**域名固定**、可直连的 OpenAI / Anthropic 兼容 API。

工作区内的适配层见 [cnb2dsh](https://cnb.cool/Dawn2766/cnb2dsh)；本仓库负责它之外的中转层。

## 为什么需要它

工作区端口代理打印的 `https://<子域名>-9001.cnb.run` 有三个问题：**子域名约每 18 小时漂移一次**、**网关有 10 MiB 请求体上限且会拦截部分提示词/图片编码**、**工作区会休眠**。本仓库用一个永久域名包住它们。

## 能力

| 能力 | 说明 |
|---|---|
| 固定域名 | 永久别名 + 工作区自注册；切换前探测健康、原子改写、失败自动回滚 |
| 双重自愈 | CNB 侧 5 分钟 cron 重注册；服务器侧看门狗每分钟探活，确认代际未变且两路都不健康才重启工作区 |
| 图片预算 | 按 base64 长度计算，超预算时压缩、仍超则省略最旧图片，让请求在到达网关前就合法 |
| 指纹中和 | 仅替换 `system`/`developer`/`assistant` 中两处会触发 `400/11128` 的子串；用户轮次与图片字节不动 |
| 配对修复 | 仅在本地确实修复了工具配对时重试一次 `11133`，不盲目重放 |
| 回收期语义 | 把回收期的 302/伪 401/gateway 错误转成可重试的 `503` + `Retry-After` |
| 协议 | OpenAI `/v1/chat/completions`（含 SSE）、`/v1/models`；Anthropic `/v1/messages`、`/v1/messages/count_tokens` |

不含图片且未命中指纹的请求**字节级原样转发**。不绕过鉴权/额度/模型权限，不提升上下文窗口，不缓存对话。

## 架构

```
客户端  baseURL=https://<域名>/v1  + Bearer <PROXY_KEY>
   │
   ▼  nginx :443
      ├─ /v1/chat/completions ─→ imgsanitize.py :9004 ─┐
      ├─ /health /v1/models /v1/messages ──→ 直连 ─────┤
      └─ /ops/register ─→ cnb-register.py :9003        │
   cnb-ai-watchdog.sh（每分钟）                          │
   ▼  proxy_pass https://$cnb_ai_upstream ◄────────────┘
CNB 工作区  node server.mjs :9001 → api.cnb.cool（携带 CNB_TOKEN）
```

只有 `/v1/chat/completions` 经过中继进程；其余路径直连工作区。

## 部署

### 1. CBN 账号与权限

- 注册 <https://cnb.cool> 并创建**组织**，代码仓库与秘钥仓库都放在其下。
- 上游是 `https://api.cnb.cool/<org>/<repo>/-/ai-ide/v2/chat/completions`，需要**工作区网络 + 流水线注入的 `CNB_TOKEN`**（平台凭证，不是页面 API Key）。
- AI credits、开发核时、CI 核时**分开计量**，各自有额度。

要生成的三类密钥（统一 `openssl rand -hex 24`）：

| 密钥 | 使用者 | 存放位置 |
|---|---|---|
| `PROXY_KEY` | 你的客户端 | 秘钥仓库 |
| `REG_TOKEN` | 工作区 → 中转端注册 | 秘钥仓库 **和** 服务器 `/root/.cnb/register.env` |
| CNB 个人令牌 | 看门狗调 CNB API | 仅服务器 |

### 2. 云原生构建任务（工作区）

复制 [cnb2dsh](https://cnb.cool/Dawn2766/cnb2dsh) 到你的组织，其 `.cnb.yml` 有两个任务：

| 任务 | 触发 | 作用 |
|---|---|---|
| `$: vscode:` | 页面点「云开发」 | 跑 `deploy/start.sh` 启动 `server.mjs`，打印 `PROXY_URI` |
| `main: "crontab: */5 * * * *":` | 每 5 分钟 | 无运行工作区则启动，有则重注册 |

```yaml
$:
  vscode:
    - docker:
        image: cnbcool/default-dev-env:latest
      imports:
        - https://cnb.cool/<org>/<secrets>/-/blob/main/proxy.yml
      env:
        REGISTER_URL: https://<域名>/ops/register
      runner:
        cpus: 2
      services:
        - name: vscode
          options:
            keepAliveTimeout: 18h   # 不设会被约 10 分钟回收一次
        - docker
      stages:
        - name: start-ai-proxy
          script: |
            bash "$(pwd)/deploy/start.sh"
```

启动后日志应出现 `proxy self-check: health OK` 与 `registered upstream to relay`。注册失败**非致命**（cron 兜底），不要改成 `exit 1`。

> cron 键必须写在与默认分支对应的分支键下，否则静默不触发。

### 3. 秘钥仓库隔离

私有仓库 `proxy.yml`：

```yaml
PROXY_KEY: "sk-<随机>"
REG_TOKEN: "<与服务器 register.env 一致>"
REGISTER_URL: "https://<域名>/ops/register"
```

设置 → 保护/导入范围 → `allow_slugs: <org>/<code-repo>`。

> **`env:` 会静默覆盖 `imports:`**。凭据只能出现在 `imports:`；两处都写时流水线看似正常，明文却已进 git 历史。

推送前检查（工作区与历史都要查）：

```bash
git grep -nE 'sk-[0-9a-f]{16,}|REG_TOKEN:\s*\S' -- . || echo clean
git log -p -- .cnb.yml | grep -E 'sk-[0-9a-f]{16,}' && echo "历史已泄露 → 直接轮换"
```

### 4. 中转与固定域名

前置：一个可编辑 DNS 的域名、一台公网 VPS（443 对公网开放，供工作区回连注册），装有 `nginx`、`python3`、`pillow`。

```bash
git clone https://github.com/dawn2766/cbn-transit.git && cd cbn-transit
sudo ./install.sh --domain ai.example.com --email you@example.com
```

装代码 → 生成并打印 `REG_TOKEN`（**立刻抄进秘钥仓库**）→ 装两个 systemd 服务 → 按域名渲染 nginx vhost → 申请证书 → 启动并自检。幂等，可重复执行。工作区代理不在默认端口时加 `--port`，它会写进 `cnb-register` 的 systemd drop-in。

<details>
<summary>手动步骤</summary>

```bash
sudo install -d /opt/cnb-imgre
sudo install -m 755 deploy/imgsanitize.py  /opt/cnb-imgre/
sudo install -m 644 deploy/image_budget.py /opt/cnb-imgre/
sudo install -m 644 deploy/systemd/cnb-imgsanitize.service /etc/systemd/system/

sudo install -m 755 deploy/cnb-register.py /usr/local/bin/
sudo install -m 644 deploy/systemd/cnb-register.service /etc/systemd/system/
sudo mkdir -p /root/.cnb
printf 'REG_TOKEN=%s\n' "$(openssl rand -hex 24)" | sudo tee /root/.cnb/register.env
sudo chmod 600 /root/.cnb/register.env

# 先建 map 文件再建 vhost，否则 nginx -t 报 unknown variable
sudo install -m 644 deploy/nginx/conf.d/*.conf /etc/nginx/conf.d/
sudo sed 's/ai\.example\.com/你的域名/g' deploy/nginx/sites-available/ai.example.com \
  | sudo tee /etc/nginx/sites-available/你的域名
sudo ln -sfn /etc/nginx/sites-available/你的域名 /etc/nginx/sites-enabled/

sudo certbot --nginx -d 你的域名
sudo systemctl daemon-reload && sudo systemctl enable --now cnb-register cnb-imgsanitize
sudo nginx -t && sudo systemctl reload nginx
```

</details>

vhost 里四处不能省：`resolver`（`proxy_pass` 用变量，否则全 502）、`proxy_ssl_server_name on`、`proxy_buffering off`（否则流式卡住）、`client_max_body_size 0` 与 `proxy_request_buffering off`（准入交给中继与工作区的 JSON 413/503，nginx 不要抢先回 HTML 413、也不要把上传落到磁盘）。同时 `/v1/chat/completions` 必须指向 `cnb_imgsanitize` 并把 502/504 转成可重试 503，否则图片预算与指纹中和全部失效。

看门狗（建议开启）：

```bash
sudo cp deploy/env/cnb-ai-watchdog.env.example /etc/cnb-ai-watchdog.env
sudo chmod 600 /etc/cnb-ai-watchdog.env      # 填写 CNB_REPO / FIXED_URL / REGISTER_URL / CNB_TOKEN
sudo install -m 755 deploy/cnb-ai-watchdog.sh /usr/local/bin/
echo '* * * * * /usr/local/bin/cnb-ai-watchdog.sh >/dev/null 2>&1' | sudo crontab -
```

部署前的判断项、密钥隔离与轮换顺序、证书续期会静默改坏 vhost 的坑，
见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 客户端接入

```python
# OpenAI SDK
OpenAI(base_url="https://<域名>/v1", api_key="sk-...")

# Anthropic SDK：源站地址不带 /v1
Anthropic(base_url="https://<域名>", api_key="sk-...")
```

DSH 的桌面系统提示词含两处会被网关判为「未通过渠道校验」的指纹，**必须走中继层**；直连工作区 `PROXY_URI` 会得到 `400 / 11128`。

## 验证

```bash
BASE_URL=https://<域名> PROXY_KEY=sk-... ./smoke-test.sh
```

依次检查 health → models → 错密钥 401 → 补全 → SSE 分块 → Anthropic，首个失败即定位到具体层。

## 边界与排障

**硬边界**：网关请求体 **10 MiB 不可配置**；`11128` 由提示词指纹触发且不重试；工作区约 18 小时回收一次；额度分三类且模型价格可能调整。

| 现象 | 处理 |
|---|---|
| 构建日志 `register skipped` | `imports:` 未生效或键名不一致 |
| `/ops/register` → 403 | 两处 `REG_TOKEN` 不一致 |
| 冻结域名 401/302 跳登录页 | `map` 指向旧子域名，手动重注册或等 cron |
| 502 + `no resolver defined` | vhost 丢了 `resolver`（常见于 certbot 续期改写） |
| HTML 413 | 有别的 nginx 层加了 `client_max_body_size`；本仓库 vhost 是 `0` |
| `413 … limit 10.00MB` | 网关硬上限，减少图片或压缩历史 |
| 流式一次性返回 | `proxy_buffering` 未关 |

重注册（不必等 cron）：

```bash
curl -X POST https://<域名>/ops/register -H "X-Reg-Token: <REG_TOKEN>" \
  -H 'Content-Type: application/json' -d '{"uri":"https://<当前子域名>-9001.cnb.run"}'
```

密钥轮换顺序：生成 → 更新秘钥仓库 → 更新服务器 `register.env` 并重启 `cnb-register` → 重启工作区 → 更新客户端 → 验收「新密钥 200 / 旧密钥 401」。

证书续期后务必复查 vhost 仍含 `resolver` 与 `$cnb_ai_upstream`，再 `nginx -t`。

更细的逐层排查与紧急处置见 [docs/OPERATIONS.md](docs/OPERATIONS.md)；部署前的
判断、密钥隔离与轮换顺序、证书续期会毁掉 vhost 这类静默坑，见
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 仓库结构

```
install.sh              一键安装/更新
smoke-test.sh           分层自检
deploy/imgsanitize.py   请求中继
deploy/image_budget.py  图片预算
deploy/cnb-register.py  自注册接收
deploy/cnb-ai-watchdog.sh  恢复控制器
deploy/{systemd,nginx,env}/  部署模板
tests/                  离线回归（无需凭据，全合成数据）
docs/OPERATIONS.md      排障手册
docs/DEPLOYMENT.md      部署判断与密钥轮换
```

## 许可

[MIT](LICENSE)。`cnb-register.py` 与 `cnb-ai-watchdog.sh` 同源于 cnb2dsh；`imgsanitize.py` / `image_budget.py` 是在其基础上按生产环境演化的版本。

CNB 的接口、额度与内容策略由平台决定并可能随时调整；本仓库只做兼容与中转，不代表平台承诺。
