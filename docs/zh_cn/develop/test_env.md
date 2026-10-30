# 本地测试环境（Linux + Wine）

[`testenv/`](/testenv) 提供两套可以直接在 Linux 实例（云服务器、WSL、CI 等）上启动的测试环境，统一由 `testenv/testenv.sh` 管理：

| 环境 | 内容 | 适合测试 |
| --- | --- | --- |
| Linux 原生环境 | 虚拟环境 + Linux 版 MaaFramework | 资源校验、Web 服务、pipeline 逻辑（配合 `Debug` 虚拟设备） |
| Wine 环境 | Wine 中运行 **Windows 版 Python 与 Windows 版 MaaFramework**，外加一个测试目标窗口 | Windows 下的行为、`桌面端`（Win32）控制器的截图与输入、Windows 版 Web 服务 |

Wine 环境相当于一台轻量的「Windows 测试机」：不需要 Windows 镜像、授权和 KVM，几分钟即可就绪，并且可以在没有图形界面的服务器和 CI 中运行（使用 Xvfb 虚拟显示）。

> [!NOTE]
> Wine 是 Windows API 的兼容实现，不是完整的 Windows 虚拟机。它能覆盖大部分 Win32 行为，但部分截图方式不可用（见 [Wine 兼容性](#wine-兼容性)），涉及具体游戏 / 软件的最终验证仍应在真实 Windows 上进行。

## 快速开始

```bash
# 1. 安装系统依赖（Python、Wine、Xvfb 等，使用 apt，需要 root 或 sudo）
testenv/testenv.sh deps

# 2. 搭建两套环境并运行全部测试，最后汇总结果
testenv/testenv.sh all
```

首次运行会创建 `.testenv/` 目录（已被 `.gitignore` 忽略），并拉取 `MaaCommonAssets` 子模块配置 OCR 模型。

已在 Ubuntu 24.04 + Wine 9.0 上验证。非 apt 系发行版请自行安装 `deps` 输出的软件包。

## 命令一览

| 命令 | 说明 |
| --- | --- |
| `deps [--no-wine] [--vnc]` | 安装系统依赖；`--vnc` 额外安装 x11vnc 与 noVNC |
| `setup [--no-ocr]` | 创建 `.testenv/venv`，安装 maafw 与服务依赖，配置 OCR 模型 |
| `check [--no-node]` | JSON Schema 校验 + `maa-tools check` + Web 服务单元测试，与 CI 的 `check.yml` 一致 |
| `serve [参数...]` | 在 Linux 上启动 Web 服务，参数透传给 `service/main.py` |
| `wine-setup [--no-ocr]` | 创建 Wine 前缀，安装 Windows 版 Python、maafw 与服务依赖 |
| `wine-test [all\|service\|win32]` | 在 Wine 中运行 Web 服务单元测试与 Win32 端到端测试 |
| `wine-serve [--vnc] [--no-target] [参数...]` | 在虚拟桌面中启动测试目标窗口与 Windows 版 Web 服务 |
| `wine-target [--vnc]` | 只启动测试目标窗口 |
| `wine-python [参数...]` | 用 Wine 中的 Windows 版 Python 执行任意命令 |
| `all [--no-node]` | `setup` + `check` + `wine-test`，最后汇总结果 |
| `status` | 查看环境状态与版本 |
| `clean [--all]` | 删除虚拟环境与 Wine 前缀，`--all` 连同下载缓存一起删除 |

需要的环境（虚拟环境、Wine 前缀）不存在时，各命令会自动先执行对应的 `setup` / `wine-setup`。

## Linux 原生环境

```bash
testenv/testenv.sh setup     # 搭建
testenv/testenv.sh check     # 运行与 CI 相同的检查
testenv/testenv.sh serve     # 启动 Web 服务，默认 http://127.0.0.1:8765/
```

`serve` 的用户目录为 `.testenv/user`（配置文件、日志都在这里），其余参数与 [Web 服务](./web_service.md) 一致，例如 `testenv/testenv.sh serve --http-port 9000`。

Linux 上没有安卓模拟器或桌面窗口时，可以用 `Debug` 虚拟设备联调接口：

```bash
curl -X POST http://127.0.0.1:8765/api/connect -H "Content-Type: application/json" -d '{"controller_type": "Debug"}'
curl -X POST "http://127.0.0.1:8765/api/runs?wait=1" -H "Content-Type: application/json" -d '{"tasks": ["普通任务"]}'
```

`Debug` 设备默认返回纯黑截图，也可以指定一张截图来测试识别：`{"controller_type": "Debug", "debug": {"image": "/path/to/screen.png"}}`。

## Wine（Windows）环境

### 搭建

```bash
testenv/testenv.sh wine-setup
```

这一步会：

1. 在 `.testenv/wine` 创建 64 位 Wine 前缀（不安装 Wine Mono / Gecko）；
2. 从 NuGet 下载 Python 官方发布的 Windows 版 Python（默认 3.12.10），解压到 `C:\python`；
3. 在宿主机用 `pip download` 下载 [`testenv/wine/requirements.txt`](/testenv/wine/requirements.txt) 中依赖的 `win_amd64` wheel，再在 Wine 中离线安装，不需要为 Wine 单独配置网络代理和证书；
4. 写入 DLL 覆盖，让 VC++ 运行库使用微软原版 DLL，并关闭 Wine 的崩溃对话框。

### 运行测试

```bash
testenv/testenv.sh wine-test          # 全部
testenv/testenv.sh wine-test service  # 只跑 service/tests（Windows 版 Python）
testenv/testenv.sh wine-test win32    # 只跑 Win32 端到端测试
```

Win32 端到端测试（[`testenv/wine/test_win32.py`](/testenv/wine/test_win32.py)）会启动测试目标窗口并验证：

- `Toolkit.find_desktop_windows` 能找到窗口；
- 各截图方式截到的画面颜色、尺寸正确；
- 各输入方式的点击坐标、按键、文本输入能被窗口收到；
- 点击后画面变化能在截图中看到；
- 通过 Web 服务连接 `interface.json` 中的 `桌面端` 控制器，执行项目里的任务：OCR 读出窗口中的 `42` 与 `17`，自定义识别选出较小的数字，Agent 的自定义动作完成点击；以及 OCR 找到文字后点击。

测试的截图 / 输入方式可以用环境变量调整，例如在真实 Windows 上测试更多方式：

```bash
MAA_TEST_SCREENCAP=GDI,FramePool,PrintWindow MAA_TEST_INPUT=Seize,PostMessage python -m unittest discover -s testenv/wine -p "test_*.py" -v
```

### 测试目标窗口

[`testenv/wine/target_app.py`](/testenv/wine/target_app.py) 是一个只依赖 ctypes 的 Win32 窗口，标题为 `MaaSquad Test Target`，客户区固定 1280×720：

| 元素 | 位置 `[x, y, w, h]` | 用途 |
| --- | --- | --- |
| 数字 `42` / `17` | `[100, 100, 200, 100]` / `[300, 100, 200, 100]` | 对应 `my_task.json` 中 `FindSmallestNumber` 的 `candidate_rois` |
| 文字 `MaaSquad` | `[800, 100, 400, 100]` | OCR |
| 红 / 绿 / 蓝色块 | `[100, 300, 150, 150]` 起，间隔 200 | 颜色匹配、截图校验 |
| 按钮 | `[800, 300, 300, 100]` | 点击后在灰色 / 黄色之间切换，并显示点击次数 |

收到的鼠标、键盘输入会以 JSON Lines 追加到 `--log` 指定的文件（`wine-serve` / `wine-target` 写到 `.testenv/logs/target-events.jsonl`），方便在脚本中断言。也可以按照自己的项目修改这个窗口，模拟目标软件的界面。

### 在 Wine 中运行 Web 服务

```bash
testenv/testenv.sh wine-serve          # 测试目标窗口 + Windows 版 Web 服务
testenv/testenv.sh wine-serve --vnc    # 同时开启 VNC / noVNC，浏览器查看虚拟桌面
```

Wine 中的程序直接使用宿主机网络，服务地址仍是 `http://127.0.0.1:8765/`。配置文件为 [`testenv/wine/web_service.json`](/testenv/wine/web_service.json)：默认控制器为 `桌面端`，窗口匹配测试目标窗口，截图改用 `GDI`，并开启了 `security.allow_raw_entry` 便于单独调试节点。

```bash
curl -X POST http://127.0.0.1:8765/api/connect -H "Content-Type: application/json" -d '{}'
curl -o screen.png http://127.0.0.1:8765/api/screenshot
curl -X POST "http://127.0.0.1:8765/api/entry?wait=1" -H "Content-Type: application/json" \
    -d '{"entry": "OcrTask", "pipeline_override": {"OcrTask": {"roi": [800, 100, 400, 100], "expected": "MaaSquad", "action": "Click"}}}'
```

开启 `--vnc` 后，浏览器打开 `http://127.0.0.1:6080/vnc.html` 即可看到虚拟桌面（只监听本机、无密码）。远程服务器上可通过 SSH 端口转发访问：`ssh -L 6080:127.0.0.1:6080 -L 8765:127.0.0.1:8765 <服务器>`。

> [!TIP]
> 项目的 `interface.json` 默认没有启用 `agent`，所以「自定义识别与动作示例」任务在 `wine-serve` 中会因为找不到自定义识别而失败；启用 `agent` 后即可正常执行。端到端测试会自行生成启用了 agent 的 `interface.json`。

## Wine 兼容性

在 Wine 9.0 + MaaFramework v5.14 上的实测结果：

| 截图方式 | 结果 |
| --- | --- |
| `GDI`、`ScreenDC` | 可用 |
| `PrintWindow` | 截图为纯黑（`interface.json` 中的 `桌面端` 默认使用该方式，测试环境中已改为 `GDI`） |
| `FramePool`、`DXGI_DesktopDup`、`DXGI_DesktopDup_Window` | 不可用 |

| 输入方式 | 结果 |
| --- | --- |
| `Seize`、`SendMessage`、`PostMessage`、`SendMessageWithCursorPos`、`PostMessageWithCursorPos`、`SendMessageWithWindowPos`、`PostMessageWithWindowPos` | 点击、按键、文本输入均可用 |
| `LegacyEvent` | 点击、按键可用；`input_text` 只产生按键，不产生字符 |

Web 服务新增了 `maa.win32.screencap` / `mouse` / `keyboard` 配置（连接请求的 `win32` 参数中也可以传），用来在不修改 `interface.json` 的情况下切换截图与输入方式，详见 [Web 服务](./web_service.md)。

搭建过程中处理的 Wine 问题：

- `numpy>=2.1` 的 Windows 版调用了 Wine 9 未实现的 `ucrtbase.crealf`，导入即崩溃，因此 Wine 中固定 `numpy==2.0.2`；
- Wine 自带的 `concrt140.dll` 缺少 MaaFramework 用到的函数（停止任务时崩溃），通过 PyPI 上的 `msvc-runtime` 安装微软原版 VC++ 运行库并设置 DLL 覆盖；
- Windows 版 Python 的标准输出 / 错误是普通文件时在 Wine 9 中无法启动，`testenv.sh` 在非终端环境下统一经管道输出；
- Wine 中没有 DirectML / GPU，OCR 会回退到 CPU 推理，日志中的 `CreateDXGIFactory2 failed`、`Failed to append DML execution provider` 可以忽略；
- Wine 前缀默认没有中文字体，窗口里的中文会显示为方块，测试目标窗口只使用英文。

## Docker

不想在宿主机安装 Wine 时，可以使用预装好两套环境的镜像，仓库在运行时挂载进去：

```bash
docker build -t maasquad-testenv -f testenv/Dockerfile .

docker run --rm -v "$PWD:/workspace" maasquad-testenv all
docker run --rm -it --network host -v "$PWD:/workspace" maasquad-testenv wine-serve --vnc
```

容器默认以 root 运行，在挂载目录中生成的文件（如 OCR 模型）属于 root。

## 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `TESTENV_HOME` | 环境目录 | `<仓库>/.testenv` |
| `TESTENV_WINEPREFIX` | Wine 前缀目录 | `$TESTENV_HOME/wine` |
| `PYTHON` | 创建虚拟环境使用的 Python | `python3` |
| `MAAFW_VERSION` | 固定 maafw 版本，Linux 与 Windows 同时生效 | 最新版 |
| `WIN_PYTHON_VERSION` | Wine 中的 Python 版本 | `3.12.10` |
| `NUGET_URL` | Windows 版 Python 的 NuGet 源 | `https://api.nuget.org/v3-flatcontainer` |
| `TESTENV_XVFB` | 设为 `1` 时即使已有 `DISPLAY` 也使用独立的 Xvfb | 自动 |
| `XVFB_SCREEN` | Xvfb 分辨率 | `1600x900x24` |
| `VNC_PORT` / `NOVNC_PORT` | VNC / noVNC 端口 | `5900` / `6080` |

pip 的镜像源等配置沿用 pip 自身的环境变量，例如 `PIP_INDEX_URL`；`PIP_PRE=1` 可安装 maafw 预发布版（与 CI 的 `--pre` 一致）。

## 常见问题

### `npm ci` 报 403 / 无法访问 registry.npmmirror.com

`package-lock.json` 中锁定的是 npmmirror 源。网络无法访问时可以临时换源，或用 `check --no-node` 跳过 `maa-tools` 检查：

```bash
npm_config_registry=https://registry.npmjs.org/ npm_config_replace_registry_host=always testenv/testenv.sh check
```

### 同时运行多个 Wine 命令

`wine-test`、`wine-serve` 自己启动 Xvfb 时，退出前会结束该 Wine 前缀中的所有进程。需要同时运行时，可以为另一份环境指定不同的 `TESTENV_WINEPREFIX`。

### 已有图形桌面

存在 `DISPLAY` 时 Wine 窗口会直接显示在当前桌面上，可以看到测试过程；设置 `TESTENV_XVFB=1` 改回使用独立的虚拟显示。
