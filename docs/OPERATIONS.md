# 排障手册

按层排查，**在第一个失败的层停下**。部署步骤见 [README](../README.md)，
部署前的判断项、密钥隔离与轮换见 [DEPLOYMENT](DEPLOYMENT.md)。

## 谁在探谁

四套健康探测各自用途不同，看到"健康"要先确认是哪一套说的：

| 探测者 | 探什么 | 超时 | 判定 |
|---|---|---|---|
| 客户端 / 你的 curl | `https://<域名>/health` → nginx → 工作区 | — | HTTP 200 |
| `cnb-register.py` | 候选工作区 `https://<bid>-<port>.cnb.run/health` | 5s | 200 + JSON `status=="ok"`，不跟随跳转 |
| `cnb-ai-watchdog.sh` | 固定域名 +（判定停机前）目标工作区直连 | 8–10s | 200 + JSON `status=="ok"` |
| `imgsanitize.py` | 当前 `$cnb_ai_upstream/health` | 2s | 200 + `status=="ok"`，顺带记 `max_body_bytes` |

## 体检

```bash
D=https://ai.example.com; K=$PROXY_KEY

curl -sS -o /dev/null -w 'health=%{http_code}\n' "$D/health"
curl -sS "$D/v1/models" -H "Authorization: Bearer $K" | head -c 300
```

服务器侧：

```bash
sudo cat /etc/nginx/conf.d/cnb-ai-upstream.conf     # 当前指向的子域名
sudo journalctl -u cnb-register -n 20 --no-pager    # 注册流水
sudo journalctl -u cnb-imgsanitize -n 20 --no-pager # 中继流水
tail -n 20 /var/log/cnb-ai-watchdog.log             # 自愈流水
sudo systemctl is-active nginx cnb-register cnb-imgsanitize
```

## 第 1 层：`/health` 不通

`/health` 走直连路径，只说明 nginx 是否存活。

| 检查 | 命令 |
|---|---|
| nginx 状态 | `systemctl is-active nginx` |
| 配置合法性 | `nginx -t` |
| 证书有效期 | `openssl s_client -connect <域名>:443 -servername <域名> </dev/null \| openssl x509 -noout -dates` |
| DNS | `dig +short <域名>` |
| 端口监听 | `ss -lntp \| grep ':443'` |

## 第 2 层：`/v1/models` 异常

**401**：先绕开中转端直接问工作区，区分是密钥问题还是链路问题。

```bash
curl -sS "https://<当前子域名>-9001.cnb.run/v1/models" -H "Authorization: Bearer $K"
```

直连 200、经域名 401 → `map` 指向旧子域名（旧地址会 302 到登录页）。手动重注册：

```bash
curl -sS -X POST https://<域名>/ops/register \
  -H "X-Reg-Token: $(grep -m1 '^REG_TOKEN=' /root/.cnb/register.env | cut -d= -f2)" \
  -H 'Content-Type: application/json' \
  -d '{"uri":"https://<当前子域名>-9001.cnb.run"}'
# 期望 {"ok": true, "switched": true, ...}
```

**503 `upstream_unavailable`**：nginx 把 302/502/503/504 统一转成可重试 503，通常表示工作区正在回收/启动，等 30–90 秒。

**502 且日志 `no resolver defined`**：vhost 丢了 `resolver`（常见于 certbot 续期改写）。

```bash
grep -c 'resolver' /etc/nginx/sites-available/<域名>       # ≥1
grep -c 'cnb_ai_upstream' /etc/nginx/sites-available/<域名> # ≥2
sudo nginx -t && sudo systemctl reload nginx
```

## 第 3 层：工作区没在跑

```bash
curl -sS "https://api.cnb.cool/workspace/list?slug=<org>/<repo>" \
  -H "Authorization: Bearer <CNB 个人令牌>" | head -c 500
```

全 `stopped` → 页面点「云开发」，或等 cron（≤5 分钟）/ 看门狗（≤1 分钟）。长期 `starting` → 看该次构建日志。

## 第 4 层：补全被拒

链路上有**四层不同的请求体上限**，看清是哪一层回的，否则会在错误的层上找问题：

| 层 | 上限 | 超限表现 |
|---|---|---|
| nginx | 无（`client_max_body_size 0`） | — |
| 中继（本仓库） | 共享预算 `CNB_RELAY_BODY_BUFFER_BYTES`（默认 512 MiB）；含图请求另有 **10 MiB 网关线路预算** | JSON `413` + `max_body_bytes`；并发争用 `503 relay_busy` + `Retry-After` |
| 工作区代理 | `PROXY_BODY_BUFFER_BYTES`（默认 1 GiB），单请求容量为 1/4 | JSON `413` + `max_body_bytes`；`503 server_busy` |
| **CNB AI 网关** | **10 MiB，不可配置** | `413 … limit 10.00MB` |

10 MiB 是**出站**预算：含图片的请求在被中继压到 10 MiB 以内之后才可能成功，
所以"把中继预算调大"对这个 413 没有帮助。

| 响应 | 含义 | 处理 |
|---|---|---|
| `400` `11128` | 渠道校验，指纹未被中和 | 确认 `/v1/chat/completions` 指向 `cnb_imgsanitize`，而非直连上游 |
| `400` `11133` | 工具配对不完整 | 中继已尝试修复并重试一次；仍失败说明修复未改变配对，检查客户端历史 |
| `413` HTML | 有别的 nginx 层加了 `client_max_body_size`（本仓库 vhost 为 `0`） | 找出并去掉那一层的上限 |
| `413` `remote URL images cannot be resized` | 请求里是远端 URL 图片，中继不能下载/压缩 | 改用内联 base64 附件，或缩短会话历史 |
| `413` `limit 10.00MB` | 网关硬上限 | 减少图片、压缩会话历史 |
| `403` `MODEL_NOT_IN_LIST` | 该模型对账号不可用 | `/v1/models` 不是权限校验，换模型或确认权限 |
| `503` `relay_busy` | 中继内存或图片处理并发打满 | 降低并发，或调大 `CNB_RELAY_BODY_BUFFER_BYTES` |
| `401` 「invalid api key」且域名刚变过 | 旧子域名的回收期响应 | 看 `/var/log/cnb-ai-watchdog.log` 最近一条 `REGISTER`，等重注册或手动重注册 |

## 第 5 层：流式异常

```bash
curl -sN https://<域名>/v1/chat/completions -H "Authorization: Bearer $K" \
  -H 'Content-Type: application/json' \
  -d '{"model":"<模型>","messages":[{"role":"user","content":"count to 5"}],"stream":true}' \
  | grep -c '^data: '
```

只有 1 块 → 某处开了缓冲；直接超时 → `proxy_read_timeout` 太小（当前 3600s）。

## 主动验证自愈

```bash
sudo cp /etc/nginx/conf.d/cnb-ai-upstream.conf /tmp/map.bak
sudo sed -i 's/[a-z0-9]*-9001/deadrot0-9001/' /etc/nginx/conf.d/cnb-ai-upstream.conf
sudo nginx -t && sudo systemctl reload nginx
watch -n 15 "curl -s -o /dev/null -w '%{http_code}\n' https://<域名>/health"
# 恢复后
sudo cat /etc/nginx/conf.d/cnb-ai-upstream.conf && rm -f /tmp/map.bak
```

## 紧急处置

| 场景 | 动作 |
|---|---|
| 新部署的中继有问题 | `systemctl stop cnb-imgsanitize`，并把 `/v1/chat/completions` 临时改回直连（功能降级但可用） |
| 怀疑 `PROXY_KEY` 泄露 | 立即轮换，客户端先换 |
| 怀疑 `REG_TOKEN` 泄露 | 轮换并重启 `cnb-register`，确认 `map` 指向自己的子域名 |
| 完全重建 | 重新执行 `sudo ./install.sh --domain <域名> --yes`（幂等） |

## 常规体检与证书

证书续期可能被 `certbot --nginx` 改坏 vhost（`resolver` / `location /` 被还原），
域名几小时后才失效且没有明显线索。每次续期后复查：

```bash
sudo certbot renew --dry-run
grep -c 'resolver' /etc/nginx/sites-available/<域名>            # ≥ 1
grep -c 'cnb_ai_upstream' /etc/nginx/sites-available/<域名>     # ≥ 2
test -f /etc/nginx/conf.d/cnb-ai-upstream.conf && echo map-ok
sudo nginx -t && sudo systemctl reload nginx
```

证书到期时间：

```bash
echo | openssl s_client -connect <域名>:443 -servername <域名> 2>/dev/null \
  | openssl x509 -noout -dates
```

## 日志位置

| 路径 | 内容 |
|---|---|
| `/var/log/nginx/access.log` | 状态码（`302`/`503` 增多即上游异常） |
| `/var/log/nginx/error.log` | resolver、握手失败 |
| `journalctl -u cnb-register` | 注册候选与切换结果 |
| `journalctl -u cnb-imgsanitize` | 指纹中和、图片压缩、配对修复重试 |
| `/var/log/cnb-ai-watchdog.log` | 探活、重注册、受控停机 |
| `/var/lib/cnb-ai-watchdog/` | 失败计数、冷却时间、锁 |

中继日志不记录提示词正文，仅计数与字节数。
