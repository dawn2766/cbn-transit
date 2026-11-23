# 运维与排障手册（RUNBOOK）

面向「已经部署好、现在出问题」的场景。按**层的顺序**排查，**在第一个失败的层停下**。

- 架构与请求生命周期：[ARCHITECTURE.md](ARCHITECTURE.md)
- 首次部署：[README](../README.md) 第 4 节

---

## 1. 五分钟体检

```bash
D=https://ai.example.com     # ← 你的冻结域名
K=$PROXY_KEY                 # ← 你的客户端密钥

# 1) 中转端自身（不涉及 CNB）
curl -sS -o /dev/null -w 'relay health=%{http_code}\n' "$D/health"

# 2) 中转 → 工作区 → 上游
curl -sS "$D/v1/models" -H "Authorization: Bearer $K" | head -c 300

# 3) 全链路
curl -sS "$D/v1/chat/completions" -H "Authorization: Bearer $K" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"hi"}]}' | head -c 300
```

服务器侧：

```bash
sudo cat /etc/nginx/conf.d/cnb-ai-upstream.conf        # 当前指向的子域名
sudo journalctl -u cnb-register -n 20 --no-pager       # 注册流水
sudo journalctl -u cnb-imgsanitize -n 20 --no-pager    # 中继流水
tail -n 20 /var/log/cnb-ai-watchdog.log                # 自愈流水
sudo systemctl is-active nginx cnb-register cnb-imgsanitize
```

`/health` 返回示例（工作区代理上报的预算信息）：

```json
{"status":"ok","uptime_sec":49438,"max_body_bytes":268435456,
 "request_buffer_bytes":1073741824,"request_buffer_used_bytes":0}
```

---

## 2. 按层排障

### 第 1 层：`/health` 就不通

| 检查 | 命令 | 判读 |
|---|---|---|
| nginx 是否在跑 | `systemctl is-active nginx` | 非 `active` → 看 `journalctl -u nginx` |
| 配置是否合法 | `nginx -t` | 报错就直接改，别 reload |
| 证书是否过期 | `openssl s_client -connect ai.example.com:443 -servername ai.example.com </dev/null \| openssl x509 -noout -dates` | 过期 → `certbot renew` |
| DNS | `dig +short ai.example.com` | 空 → DNS 没生效 |
| 防火墙 | `ss -lntp \| grep ':443'` | 未监听 → nginx 没起来 |

> `/health` 是**直连路径**（不经过中继进程）。它能通只说明 nginx 活着；上游是否正常看第 2 层。

### 第 2 层：`/v1/models` 401 / 503 / 502

**401**：客户端密钥不对，或请求根本没到工作区。

```bash
# 直接问工作区，绕开中转，确认是密钥问题还是链路问题
curl -sS -o /dev/null -w '%{http_code}\n' "https://<当前子域名>-9001.cnb.run/health"      # 期望 200
curl -sS "https://<当前子域名>-9001.cnb.run/v1/models" -H "Authorization: Bearer $K"      # 期望 200
```

工作区直连 200、经冻结域名 401 → 说明 `map` 指向的是**旧子域名**（旧地址会 302 到登录页）。
处理：手动重新注册，不必等 cron。

```bash
sudo cat /root/.cnb/register.env            # 取 REG_TOKEN
curl -sS -X POST https://ai.example.com/ops/register \
  -H "X-Reg-Token: <REG_TOKEN>" -H 'Content-Type: application/json' \
  -d '{"uri":"https://<当前子域名>-9001.cnb.run"}'
# 期望 {"ok": true, "switched": true, "sub": "<当前子域名>"}
```

**503 `upstream_unavailable`**：nginx 把 `302/502/503/504` 统一转成了可重试的 503。含义是「上游此刻不可用」。
通常是工作区正在回收/启动。等待 30–90 秒；若持续超过 5 分钟，走第 3 层。

**502 且日志 `no resolver defined`**：vhost 丢了 `resolver` 行（常因 certbot 续期改写配置）。

```bash
grep -n 'resolver' /etc/nginx/sites-available/ai.example.com
grep -n 'cnb_ai_upstream' /etc/nginx/sites-available/ai.example.com   # 也不能丢
sudo nginx -t && sudo systemctl reload nginx
```

### 第 3 层：工作区没在跑

```bash
# 用个人令牌列出工作区状态
curl -sS "https://api.cnb.cool/workspace/list?slug=<org>/<repo>" \
  -H "Authorization: Bearer <CNB 个人令牌>" | head -c 500
```

- 全是 `stopped` → CNB 页面手动点「云开发」，或等 cron（≤5 分钟）/ 看门狗（≤1 分钟）拉起。
- 一直 `starting` → 看该次构建的日志，通常是镜像拉取或依赖安装失败。

### 第 4 层：补全请求被拒（拿到 4xx）

| 响应 | 含义 | 处理 |
|---|---|---|
| `400` + `code 11128` | 渠道校验；提示词指纹没被中和 | 确认 `/v1/chat/completions` 指向中继进程，而不是直连上游 |
| `400` + `code 11133` | 工具调用与结果配对不完整 | 中继会尝试修复并重试一次；仍失败说明修复未改变配对，检查客户端历史 |
| `413` 且 HTML | nginx 层拦截 | `client_max_body_size 8m` |
| `413` + `limit 10.00MB` | 网关硬上限 | 减少图片、压缩会话历史；中继会自动压缩，但存在下限 |
| `403` + `MODEL_NOT_IN_LIST` | 该模型对当前账号不可用 | `/v1/models` 不是权限校验，换模型或确认账号权限 |
| `503` + `relay_busy` | 中继内存预算或图片处理并发打满 | 降低并发，或调大 `CNB_RELAY_BODY_BUFFER_BYTES`（需相应内存） |

### 第 5 层：流式响应异常

```bash
curl -sN https://ai.example.com/v1/chat/completions \
  -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"count to 5"}],"stream":true}' \
  | grep -c '^data: '
```

- 只有 1 块 → 某处开了缓冲（`proxy_buffering` 或中继位置不对）。
- 直接超时 → `proxy_read_timeout` 太小（当前 3600s）。

---

## 3. 密钥轮换

按顺序执行，每步都是安全的中间状态：

```bash
# 1) 生成
openssl rand -hex 24

# 2) 更新 CNB 秘钥仓库 proxy.yml（PROXY_KEY / REG_TOKEN / REGISTER_URL）

# 3) 更新中转端 REG_TOKEN（若轮换它）
sudo sh -c "printf 'REG_TOKEN=%s\n' '<新值>' > /root/.cnb/register.env"
sudo chmod 600 /root/.cnb/register.env
sudo systemctl restart cnb-register

# 4) 热重启工作区内的代理（不要用裸 nohup，变量在 /etc/profile 里）
#    或者最简单：在 CNB 页面重启云开发，由 start.sh 重新读 imports

# 5) 更新客户端 apiKey

# 6) 双向验收：新密钥 200，旧密钥 401
curl -s -o /dev/null -w 'new=%{http_code}\n' https://ai.example.com/v1/models -H "Authorization: Bearer <新>"
curl -s -o /dev/null -w 'old=%{http_code}\n' https://ai.example.com/v1/models -H "Authorization: Bearer <旧>"
```

---

## 4. 证书续期（容易被忽略的故障源）

```bash
sudo certbot renew --dry-run
# 续期后必须复查，certbot 可能改写 vhost
grep -c 'resolver'      /etc/nginx/sites-available/ai.example.com   # ≥1
grep -c 'cnb_ai_upstream' /etc/nginx/sites-available/ai.example.com # ≥2
sudo nginx -t && sudo systemctl reload nginx
curl -s -o /dev/null -w '%{http_code}\n' https://ai.example.com/health
```

---

## 5. 主动验证自愈（部署后做一次）

把 `map` 故意改坏，观察是否自动恢复：

```bash
sudo cp /etc/nginx/conf.d/cnb-ai-upstream.conf /tmp/map.bak
sudo sed -i 's/[a-z0-9]*-9001/deadrot0-9001/' /etc/nginx/conf.d/cnb-ai-upstream.conf
sudo nginx -t && sudo systemctl reload nginx

# 等待下一个 cron（≤5 分钟）或看门狗（≤1 分钟）
watch -n 15 "curl -s -o /dev/null -w '%{http_code}\n' https://ai.example.com/health"
```

恢复后：

```bash
sudo cat /etc/nginx/conf.d/cnb-ai-upstream.conf    # 应指回真实子域名
rm -f /tmp/map.bak
```

---

## 6. 回滚与紧急处置

| 场景 | 动作 |
|---|---|
| 新部署的中继有问题 | `sudo systemctl stop cnb-imgsanitize`，并把 vhost 的 `/v1/chat/completions` 临时改成 `location /` 那套直连（功能降级，但能通话） |
| 怀疑密钥泄露 | 立刻轮换 `PROXY_KEY`（第 3 节），客户端先换 |
| 怀疑 `REG_TOKEN` 泄露 | 轮换并重启 `cnb-register`；确认 `map` 指向的是你自己的子域名 |
| 域名解析异常 | 检查 VPS 的 `resolver` 是否可达；必要时临时把 `resolver` 换成公共 DNS 并 reload |
| 完全重建中继 | 在 VPS 上重新执行 `sudo ./install.sh --domain <域名> --yes`（幂等） |

---

## 7. 日志与状态位置

| 路径 | 内容 |
|---|---|
| `/var/log/nginx/access.log` | 每个请求的状态码与耗时（`302`/`503` 增多即上游异常） |
| `/var/log/nginx/error.log` | `resolver` 失败、握手失败等 |
| `journalctl -u cnb-register` | 每次注册的候选 URI 与切换结果 |
| `journalctl -u cnb-imgsanitize` | 指纹中和、图片压缩统计、配对修复重试、`relay_busy` |
| `/var/log/cnb-ai-watchdog.log` | 探活失败、重新注册、RECOVERED、受控停机 |
| `/var/lib/cnb-ai-watchdog/` | 看门狗状态（失败计数、冷却时间、锁） |
| `/etc/nginx/conf.d/cnb-ai-upstream.conf` | 当前上游指向（唯一可变指针） |

> 中继日志**不记录提示词正文**，只记录计数与字节数；`neutralized client fingerprint tokens=N input_bytes=… output_bytes=…` 即为此类。
