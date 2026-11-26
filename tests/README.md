# 离线回归测试

本目录是中转端（`deploy/`）的离线回归套件：**不联网、不需要任何 CNB 凭据**，
请求体、图片与工作区响应全部是合成数据。

## 运行

在仓库根目录执行（Windows / Linux 相同）：

```bash
python -B -m unittest discover -s tests -p 'test_*.py' -v
```

单个文件：

```bash
python -B -m unittest discover -s tests -p 'test_imgsanitize.py' -v
```

依赖：

- Python 3.10+（服务器为 3.10.12；套件也在 3.12 上验证过）
- Pillow（`image_budget.py` 导入 PIL）：`pip install pillow`
- 看门狗用例需要 `bash`。Windows 上会自动找 Git for Windows 自带的
  `bash.exe`（系统自带 `System32\bash.exe` 是 WSL 启动器，不可用）；
  找不到时这些用例 `skipUnless` 跳过，其余用例照常运行。
  也可以用 `DEPLOY_TEST_BASH` 直接指定 bash 路径。

Windows 控制台遇到中文输出乱码时先设置：

```powershell
$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'
```

## 文件

| 文件 | 覆盖对象 |
|---|---|
| `test_imgsanitize.py` | `deploy/imgsanitize.py`：指纹中和、字节级透传、11133 单次配对修复、准入与预算（413/503/408）、`_BodyBuffers` 记账、chunked 透传、401/302→503、5xx 不重放、8 路并发流释放预算；`deploy/image_budget.py`：10 MiB 网关预算的压缩/省略、`wire_size`/`_number_padding`、`MAX_IMAGES`；`_UpstreamPool` 与 `open_upstream` 连接复用 |
| `test_register.py` | `deploy/cnb-register.py`：候选健康检查、原子改写 + 回滚、reload 超时、注册接口的读取超时 |
| `test_watchdog.py` | `deploy/cnb-ai-watchdog.sh`：用假的 `curl`/`systemctl`/`api_call` 驱动 `fake_death`、`list_running_info`、`probe_url`、锁占用 |
| `test_deployment_config.py` | `deploy/nginx/sites-available/ai.example.com`：不重试 POST、不缓冲 SSE、上传准入交给中继、保留工作区的 503 + Retry-After |

## 注意

- 不修改 `deploy/` 下的生产代码；测试只读文件、注入假实现（`mock.patch`）。
- 需要 `deploy/` 在 `sys.path` 上才能 import `image_budget`，`test_imgsanitize.py`
  已在加载中继前插入该路径。
- `relay.Server.daemon_threads = False`（SIGTERM 时要先排空在途请求），
  所以任何启动它的用例都必须 `shutdown()` + `server_close()`，否则进程会挂住。
