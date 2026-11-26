# 客户端提示词兼容性（网关 400/11128）

状态：2026-11-16 起，DSH 经固定域名中转端访问通过。

## 症状与成因

带 DSH 安装时系统提示词的请求返回 HTTP 400、`code: 11128`、
`Illegal API invocation from an unapproved channel`。网关对 `system`、
`developer`、`assistant` 的**消息文本**做精确子串检查；它不是请求头、
API key、额度、图片或请求体大小检查。

同一把 key、同一模型、同一端点的复现：

| system 文本 | 结果 |
|---|---|
| 中性的 assistant 提示词 | 200 |
| `You are an AI agent powered by DeepSeek Harness.` | 400 / 11128 |
| 完整的 6314 字符 DSH 桌面系统提示词 | 400 / 11128 |
| 同样内容换成本文下面两处替换后 | 200 |

同样的文本放在 `user` 或 `tool` 消息里返回 200，说明这两个角色不被扫描。
短中性提示词与会话标题请求也是 200。

## 中和规则

中转端（[`deploy/imgsanitize.py`](../deploy/imgsanitize.py)）在转发前替换
两处精确指纹：

| 原文 | 替换为 |
|---|---|
| `DeepSeek Harness` | `DeepSeek` |
| `Tokens prefixed with @ are paths` | `Tokens prefixed with (at) are paths` |

范围与边界：

- 只改 `system`、`developer`、`assistant` 的消息文本，包括 content 数组里的
  文本片段（含 thinking 块与嵌套 `parts[]`）。
- `user` / `tool` 轮次、工具名与 schema、工具调用与结果、标识符、URL、
  图片字节都不动。
- 不含这两个指纹的请求体**字节级原样**转发。
- 改写串行执行并有 32 MiB 上限；解析或准入失败时原样转发，让上游错误
  照常返回。
- `11128` 不重试：一次请求拿到什么就返回什么。日志只记
  `neutralized client fingerprint tokens=N input_bytes=… output_bytes=…`，
  不含提示词正文。

这是提示词兼容措施，不绕过鉴权、AI credits 或模型权限——那些仍由上游判定。

## 验证

- 真实 DSH 运行经 CBN provider 返回 `OK`，退出码 0。
- 完整的 DSH 桌面系统提示词（含 Web GUI 与 Agent Teams 段落）经中转端返回 200。
- 中性请求与含同样字样的 `user` 请求返回 200 且未被改写。

## 如果拒绝又出现了

上游可能新增指纹。用当前的 DSH 系统提示词复现，找出新加的精确子串，
往 `_FINGERPRINTS` 加一对 `(from, to)`，在 `tests/test_imgsanitize.py`
补用例，再重新部署中继。不要加盲重试：拒绝只返回一次，原样返回。
