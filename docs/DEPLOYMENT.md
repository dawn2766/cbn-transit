# 部署与密钥手册（从中转仓库视角）

安装步骤见 [README](../README.md)，逐层排障见 [OPERATIONS](OPERATIONS.md)。
本文件只记录**部署前要想清楚的判断**、**容易踩且会静默生效的坑**，以及验收动作。

## 1. 开工前先确认的五件事

答案会改变方案，别假设：

1. **有域名 + 一台能对公网开 443 的 VPS 吗？** 两者都没有就没有固定域名，
   只能用会漂移的 `https://<子域名>-9001.cnb.run` 并手工改 `baseURL`。
   VPS 的 443 必须对公网可达——工作区要回连 `POST /ops/register`。
2. **代码仓库是公开还是私有？** 别凭印象，直接验：

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' "https://cnb.cool/<org>/<repo>/raw/main/.cnb.yml"
   ```

   不带凭据返回 `200` 就是公开，那么里面出现过的任何值都按已泄露处理。
3. **工作区代理占哪个端口？** 默认 `9001`（`PROXY_PORT`）。它同时出现在
   子域名（`-9001`）和 `CNB_VSCODE_PROXY_URI` 里。改端口要同步三处：
   工作区 `PROXY_PORT`、`install.sh --port`（写进 `cnb-register` 的
   systemd drop-in）、看门狗的 `UPSTREAM_PORT`。
4. **默认分支叫什么？** `.cnb.yml` 的 keepalive cron 写在 `main:` 键下；
   分支不叫 `main` 就改名键或改分支，否则 cron **静默不触发**。
5. **这台机器/这个域名上还有别的站点吗？** 永远只新增 `server_name` 块，
   不动既有 vhost，最后确认其它站点仍然正常。

## 2. 密钥隔离（最容易出事的一步）

`.cnb.yml` 里 **`env:` 会静默覆盖 `imports:`**。凭据同时出现在两处时，
流水线看起来一切正常，而明文已经进了 git 历史。所以：

| 仓库 | 可见性 | 放什么 | 允许放密钥 |
|---|---|---|---|
| `<org>/cnb2dsh` | 公开或私有 | `.cnb.yml`（只有 `imports:`） | **绝不** |
| `<org>/cnb2dsh-secrets` | **私有** | `proxy.yml` | 是，这就是它的用途 |

私有仓库的 `proxy.yml`：

```yaml
PROXY_KEY: "sk-<openssl rand -hex 24>"
REG_TOKEN: "<与服务器 /root/.cnb/register.env 相同>"
REGISTER_URL: "https://<域名>/ops/register"
```

再把导入范围收紧到 `allow_slugs: <org>/cnb2dsh`，否则任何仓库都能拉走这些值。

推送前两处都要查（工作区 **和历史**）：

```bash
git grep -nE 'sk-[0-9a-f]{16,}|REG_TOKEN:\s*\S' -- . || echo clean
git log -p -- .cnb.yml | grep -E 'sk-[0-9a-f]{16,}' && echo "历史已泄露 → 直接轮换"
```

历史泄露时**轮换**，不要花力气洗历史：轮换很便宜，判断“有没有被抓走”很贵。

## 3. 证书续期会静默毁掉中转

**这个坑踩过一次。** `certbot --nginx` 续期时可能重写 vhost，把 `resolver`
或整段 `location /` 打回原形；域名几小时后才失效，而且当时没有明显线索。

vhost 定稿之后必须跑一次：

```bash
certbot renew --dry-run
grep -c 'resolver' /etc/nginx/sites-available/<域名>              # ≥ 1
grep -c 'cnb_ai_upstream' /etc/nginx/sites-available/<域名>       # ≥ 2
test -f /etc/nginx/conf.d/cnb-ai-upstream.conf && echo map-ok
nginx -t && systemctl reload nginx
```

之后每次续期都复查这四项。

## 4. 把工作区接到中转端

`.cnb.yml` 两个阶段都要 `imports:`，`env:` 里只放非密钥值
（`REGISTER_URL` 在私密文件里，也可以放 `env:`，它本身不是密钥）：

```yaml
$:
  vscode:
    - docker:
        image: cnbcool/default-dev-env:latest
      imports:
        - https://cnb.cool/<org>/cnb2dsh-secrets/-/blob/main/proxy.yml
      env:
        REGISTER_URL: https://<域名>/ops/register
      services:
        - name: vscode
          options:
            keepAliveTimeout: 18h     # 不设会被约 10 分钟回收一次
        - docker
      stages:
        - name: start-ai-proxy
          script: |
            bash "$(pwd)/deploy/start.sh"

main:
  "crontab: */5 * * * *":
    - name: keepalive
      # …同一份 imports:；非破坏性：无运行工作区才 start，有则重注册，绝不 stop
```

`deploy/start.sh` 注册失败**非致命**，cron 是兜底。不要改成 `exit 1`：
中转端抖一下就会让每次工作区启动变红。

## 5. 验收：主动把自愈打断一次

设计说明不算证据，部署当天手动破一次：

```bash
sudo cp /etc/nginx/conf.d/cnb-ai-upstream.conf /tmp/map.bak
sudo sed -i 's/[a-z0-9]*-9001/deadrot0-9001/' /etc/nginx/conf.d/cnb-ai-upstream.conf
sudo nginx -t && sudo systemctl reload nginx
watch -n 15 "curl -s -o /dev/null -w '%{http_code}\n' https://<域名>/health"
```

应当在下一次 cron（≤5 分钟）或下一次看门狗（≤1 分钟）内从 503 恢复 200，
并能在 `journalctl -u cnb-register` 与 `/var/log/cnb-ai-watchdog.log` 看到
对应流水。恢复后删掉 `/tmp/map.bak`。

2026-11-04 的历史观测：cron 路径约 82 秒恢复。这是当时的结果，不是对当前
控制器的时长承诺。

## 6. 密钥轮换顺序

顺序错了会出现“一把钥还活着、另一把已经死了”的中间态。按这个顺序：

1. **生成**：`openssl rand -hex 24`（`PROXY_KEY` 前面加 `sk-`）。
2. **私密仓库先改** `proxy.yml`。
3. **中转端 `REG_TOKEN`**（只在轮换它时）：改 `/root/.cnb/register.env`
   （`chmod 600`）→ `systemctl restart cnb-register`。
4. **工作区重启**：正常重启走 `start.sh` 自己重读 `imports:`。
   热重启容易踩坑：`CNB_TOKEN` / `CNB_REPO_SLUG` / `CNB_VSCODE_PROXY_URI`
   都在 `/etc/profile` 里，非登录 shell 读不到，必须显式传：

   ```bash
   getvar() { grep -m1 "^export $1=" /etc/profile | cut -d'"' -f2; }
   cd /workspace/proxy && pkill -f "node server.mjs"; sleep 1
   CNB_TOKEN=$(getvar CNB_TOKEN) CNB_REPO_SLUG=$(getvar CNB_REPO_SLUG) \
   PROXY_KEY=<新 KEY> REG_TOKEN=<新 TOKEN> REGISTER_URL=https://<域名>/ops/register \
     nohup node server.mjs > proxy.log 2>&1 &
   sleep 5 && curl -s 127.0.0.1:9001/health
   curl -sS -X POST https://<域名>/ops/register -H "X-Reg-Token: <新 TOKEN>" \
     -H 'Content-Type: application/json' -d "{\"uri\":\"$(getvar CNB_VSCODE_PROXY_URI)\"}"
   ```

5. **客户端**换 key 并重启。
6. **双向验收**：

   ```bash
   curl -s -o /dev/null -w 'new key: %{http_code}\n' https://<域名>/v1/models -H "Authorization: Bearer <新>"
   curl -s -o /dev/null -w 'old key: %{http_code}\n' https://<域名>/v1/models -H "Authorization: Bearer <旧>"  # 期望 401
   curl -s -o /dev/null -w 'old reg: %{http_code}\n' -X POST https://<域名>/ops/register \
     -H "X-Reg-Token: <旧 TOKEN>" -H 'Content-Type: application/json' -d '{"uri":"https://aaaa-9001.cnb.run"}'  # 期望 403
   ```

## 7. 全部可配环境变量

除 `.env.example` 模板外，以下变量由代码直接读取（都有默认值，一般不用改）：

| 变量 | 默认 | 读取者 | 作用 |
|---|---|---|---|
| `CNB_RELAY_BODY_BUFFER_BYTES` | `536870912` | imgsanitize.py | 中继共享原始请求体预算 |
| `UPSTREAM_CONF` | `/etc/nginx/conf.d/cnb-ai-upstream.conf` | cnb-register.py、imgsanitize.py | 上游 map 文件（读写指针） |
| `REGISTER_ENV_FILE` | `/root/.cnb/register.env` | cnb-register.py、watchdog | `REG_TOKEN` 所在文件 |
| `REGISTER_PORT` | `9003` | cnb-register.py | 注册服务监听端口（nginx 里要同步） |
| `UPSTREAM_PORT` | `9001` | cnb-register.py、watchdog | 工作区代理端口（`--port` 会写成 systemd drop-in） |
| `REGISTER_HEALTH_TIMEOUT` | `5` | cnb-register.py | 候选工作区 `/health` 探测超时（秒） |
| `REGISTER_COMMAND_TIMEOUT` | `15` | cnb-register.py | `nginx -t` / `systemctl reload` 超时（秒） |
| `REGISTER_READ_TIMEOUT` | `10` | cnb-register.py | 读请求体超时（秒） |
| `FIXED_URL`、`REGISTER_URL`、`CNB_REPO`、`CNB_TOKEN` | 见模板 | watchdog | 四个必填项；缺任一项直接 `exit 78`，不探测 |
| `FAKEDEATH_STOP_WAIT` | `5` | watchdog | stop 与 start 之间的等待（秒） |
| `CNB_API`、`WATCHDOG_LOG`、`WATCHDOG_STATE_DIR` | 见模板 | watchdog | 平台基址、日志、状态目录 |

`cnb-register.py` 与 `cnb-ai-watchdog.sh` 的注释指向本文件（`docs/DEPLOYMENT.md`）。

## 8. 仍然没有验证过的事

诚实记录，免得后续重复推导：

- **工作区回收时 `start.sh` 自注册这条路径没有被直接观测到**：被观测到的是
  cron 重注册（82 秒恢复），两者共用同一套 `imports:` + `POST /ops/register`
  逻辑。2026-11-04→05 夜间确实发生过一次回收且域名跟上了，但在没有抓启动
  日志之前应把它归功于 cron。
- **仓库可见性不能通过 CNB OpenAPI 修改**（`PATCH /{repo}` 只接受
  description/site/topics/license）。把代码仓库设为私有是页面上的手动操作。
- **上游 DNS 是请求时解析的**（变量 `proxy_pass`）。VPS 自己的 resolver 坏掉
  时，即使 map 和工作区都正常，每个请求也会 502。
- 离线回归测试不能证明故障恢复能力；控制器改动必须重新部署并跑 §5 的演练。
