# Web 服务（HTTP / WebSocket）

除了通用 UI，MaaSquad 还可以作为一个本地 Web 服务运行，让脚本、其他程序或浏览器通过 **HTTP API** 和 **WebSocket** 调用 `interface.json` 中编排好的任务。

服务代码位于 [`service/`](/service) 目录，基于 MaaFramework 的 Python 绑定实现，与通用 UI 使用同一份 `interface.json` 和 `resource`：

- 任务、选项（`select` / `switch` / `checkbox` / `input` / `hotkey`）、预设、`global_option`、`import` 按 ProjectInterface V2 协议解析为 `pipeline_override`；
- 配置了 `agent` 时会自动启动 AgentServer 子进程，自定义识别 / 动作照常可用；
- 所有任务进入同一个队列按顺序执行，可以随时停止或取消。

## 服务开关

服务由几类可独立开关的部分组成：

| 开关 | 作用 | 默认 |
| --- | --- | --- |
| `http.enabled` | HTTP API，路径前缀 `/api` | 开启 |
| `websocket.enabled` | WebSocket 接口：双向调用 + 实时事件推送 | 开启 |
| `webui.enabled` | 网页控制台（`/`），依赖 HTTP 服务 | 开启 |
| `events.task` | 推送任务级事件 `Tasker.Task.*` | 开启 |
| `events.node` | 推送节点级事件 `Node.PipelineNode.*` / `Node.Recognition.*` / `Node.Action.*` | 开启 |
| `security.allow_raw_entry` | 允许通过 `/api/entry` 直接执行任意 pipeline 节点（绕过 `interface.json` 编排） | 关闭 |
| `security.allow_remote` | 允许监听非本机地址，开启时必须设置 `auth.token` | 关闭 |

`http` 与 `websocket` 的 `host` / `port` 相同时共用一个端口（默认 `127.0.0.1:8765`），不同时分别监听。

## 快速开始

1. 安装依赖（Python 3.9 及以上）：

    ```bash
    pip install -r service/requirements.txt
    ```

2. 准备 OCR 模型等资源，参考 [如何开发](./how_to_develop.md)。

3. 启动服务：

    ```bash
    # 开发环境：在仓库根目录执行，自动使用 assets/interface.json
    python service/main.py

    # 打包后：在安装目录（interface.json 所在目录）执行
    python service/main.py
    ```

    启动后会输出各服务地址：

    ```text
    HTTP API   : http://127.0.0.1:8765/api
    网页控制台 : http://127.0.0.1:8765/
    WebSocket  : ws://127.0.0.1:8765/ws
    ```

4. 浏览器打开网页控制台，或通过下文的 API 调用。

> [!TIP]
> 没有设备时，可以在连接时指定 `"controller_type": "Debug"` 使用虚拟设备（黑屏截图、操作直接成功），用于联调接口。

## 配置文件

首次启动会生成 `config/web_service.json`（位于 `--user-dir`，默认当前目录，已被 `.gitignore` 忽略）。完整默认值见 [`service/settings.py`](/service/settings.py)，常用项：

```jsonc
{
    "http": { "enabled": true, "host": "127.0.0.1", "port": 8765, "cors_origins": [] },
    "websocket": { "enabled": true, "host": "127.0.0.1", "port": 8765, "path": "/ws" },
    "webui": { "enabled": true },
    "events": { "task": true, "node": true },
    "auth": { "token": "" },
    "security": { "allow_remote": false, "allow_raw_entry": false },
    "maa": {
        "controller": "",       // 默认控制器名称，留空取 interface.json 中第一个
        "resource": "",         // 默认资源名称
        "auto_connect": false,  // 启动后立即连接；开启后未连接时提交任务也会自动连接
        "adb": { "path": "", "address": "", "config": {} },
        "win32": { "class_regex": "", "window_regex": "" },
        "agent": { "enabled": true, "timeout": 30000 },
        "history_limit": 100
    }
}
```

命令行参数优先于配置文件：

| 参数 | 说明 |
| --- | --- |
| `--http` / `--no-http` | 开启 / 关闭 HTTP API |
| `--websocket` / `--no-websocket` | 开启 / 关闭 WebSocket |
| `--webui` / `--no-webui` | 开启 / 关闭网页控制台 |
| `--host` | 同时设置 http 与 websocket 的监听地址 |
| `--http-port` / `--ws-port` | 端口 |
| `--token` | 访问令牌 |
| `--allow-remote` | 允许监听非本机地址 |
| `--controller` / `--resource` | 默认控制器 / 资源 |
| `--auto-connect` | 启动后立即连接设备 |
| `--config` / `--user-dir` / `--interface` | 配置文件、用户数据目录、`interface.json` 路径 |

## 安全

- 默认只监听 `127.0.0.1`，仅本机可访问。
- 如需让局域网其他设备调用，需同时开启 `security.allow_remote` 并设置 `auth.token`，否则服务拒绝启动。
- 设置 `auth.token` 后，除 `/api/health` 和网页控制台页面外，所有 HTTP 请求与 WebSocket 握手都要携带令牌，任选一种方式：
  - 请求头 `Authorization: Bearer <token>`
  - 请求头 `X-Maa-Token: <token>`
  - 查询参数 `?token=<token>`（浏览器建立 WebSocket 时使用）
- 服务没有 HTTPS，暴露到公网前请自行在前面加反向代理。

## HTTP API

所有接口返回 JSON：成功时为 `{"ok": true, "data": ...}`，失败时为 `{"ok": false, "error": {"code": "...", "message": "..."}}` 并带对应的 HTTP 状态码。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（无需令牌） |
| GET | `/api/interface` | 项目信息：控制器、资源、任务、选项、预设 |
| GET | `/api/status` | 连接状态、当前运行、排队数量 |
| GET | `/api/devices?type=adb` | 搜索设备，`type` 为 `adb` 或 `win32` |
| POST | `/api/connect` | 连接设备并加载资源 |
| POST | `/api/disconnect` | 断开连接 |
| POST | `/api/runs` | 提交编排好的任务 |
| GET | `/api/runs` | 运行记录（新的在前） |
| GET | `/api/runs/{id}` | 查询单次运行，`?wait=1&timeout=秒` 可等待结束 |
| POST | `/api/runs/{id}/cancel` | 取消排队中的运行 / 停止运行中的运行（也可用 `DELETE /api/runs/{id}`） |
| POST | `/api/stop` | 停止当前运行并取消所有排队中的运行 |
| GET | `/api/screenshot` | 当前画面截图（PNG） |
| POST | `/api/entry` | 直接执行 pipeline 节点，需开启 `security.allow_raw_entry` |

### 连接设备

```jsonc
// POST /api/connect，所有字段可选
{
    "controller": "安卓端",            // interface.json 中的控制器名称
    "resource": "官服",                // interface.json 中的资源名称
    "adb": { "path": "C:/adb.exe", "address": "127.0.0.1:16384" },  // 留空则自动搜索
    "win32": { "hwnd": 123456 },       // 留空则按 interface.json 的正则查找窗口
    "controller_type": "Debug"         // 可选：使用虚拟设备联调
}
```

目前支持 `Adb`、`Win32` 类型的控制器以及用于联调的 `Debug`。

### 提交任务

```jsonc
// POST /api/runs
{
    "tasks": [
        "普通任务",                                     // 只写任务名则所有选项取默认值
        { "name": "选项任务", "option": { "任务选项1": "文本2" } }
    ],
    "stop_on_failure": false,   // 某个任务失败后是否跳过剩余任务
    "wait": false,              // true 时等待执行结束再返回（也可用查询参数 ?wait=1）
    "timeout": 600              // wait 的最长等待秒数，默认且最大 3600
}
```

也可以直接执行 `interface.json` 中的预设：`{"preset": "预设名"}`。

`option` 的取值规则：

| 选项类型 | 取值 | 示例 |
| --- | --- | --- |
| `select` | case 名 | `"文本2"` |
| `switch` | case 名或布尔值 | `"Yes"` / `true` |
| `checkbox` | case 名数组 | `["自动战斗", "自动拾取"]` |
| `input` | `{字段名: 值}`，未填写的字段使用 `default` | `{"章节号": "4"}` |
| `hotkey` | `{字段名: 快捷键}`，快捷键可以是 `"Ctrl+E"`（按 Win32 虚拟键码转换）、整数键码或 `{"primary": 69, "modifier1": 17}` | `{"技能": "E"}` |

未传的选项使用 `default_case` / `default`：`select` / `switch` 未设置默认值时取第一个 case，`checkbox` 默认不选，`input` 字段既未传值又没有 `default` 时返回错误。选项名、case 名写错或输入不符合 `verify` 时返回 400，任务不会被提交。

返回的运行记录：

```json
{
    "id": "05f4036dc4b6",
    "source": "http",
    "status": "succeeded",
    "error": null,
    "created_at": 1793275144.126,
    "started_at": 1793275144.126,
    "finished_at": 1793275144.936,
    "tasks": [
        { "name": "普通任务", "entry": "MyTask1", "status": "succeeded", "task_id": 200000001, "nodes": ["MyTask1"] }
    ]
}
```

运行状态：`queued` → `running` → `succeeded` / `failed` / `stopped`，排队中被取消为 `cancelled`。单个任务状态：`pending` / `running` / `succeeded` / `failed` / `stopped` / `skipped`。

### curl 示例

```bash
curl -X POST http://127.0.0.1:8765/api/connect -H "Content-Type: application/json" -d '{"controller": "安卓端"}'
```

```bash
curl -X POST "http://127.0.0.1:8765/api/runs?wait=1" -H "Content-Type: application/json" -d '{"tasks": ["普通任务"]}'
```

## WebSocket

连接 `ws://127.0.0.1:8765/ws` 后服务端先发送一条 `hello` 消息，之后双方都用 JSON 文本消息通信。

请求：

```json
{ "id": 1, "method": "run", "params": { "tasks": ["普通任务"], "wait": true } }
```

响应（`id` 与请求对应）：

```json
{ "type": "response", "id": 1, "ok": true, "data": { "id": "05f4036dc4b6", "status": "succeeded" } }
```

可用方法：`interface`、`status`、`devices`、`connect`、`disconnect`、`run`、`runs`、`get_run`、`cancel`、`stop`、`screenshot`（返回 base64 PNG）、`entry`、`subscribe`、`ping`。参数与同名 HTTP 接口的请求体 / 查询参数一致，`get_run` 与 `cancel` 使用 `{"id": "运行 id"}`。

每个请求独立处理，`wait: true` 的请求不会阻塞同一连接上的其他请求。

### 事件推送

服务端会主动推送事件：

```json
{ "type": "event", "event": "run", "data": { "id": "05f4036dc4b6", "status": "running", "tasks": [] } }
```

| 事件 | 说明 |
| --- | --- |
| `status` | 连接状态变化 |
| `run` | 运行记录变化（提交、开始、每个任务开始 / 结束、运行结束） |
| `maa` | MaaFramework 原始回调，`data` 为 `{"msg": "Node.Action.Succeeded", "details": {...}}`，受 `events.task` / `events.node` 开关控制 |

默认推送全部事件，可以用 `subscribe` 只订阅需要的事件：

```json
{ "id": 2, "method": "subscribe", "params": { "events": ["run"] } }
```

### Python 示例

```python
import asyncio
import json

import aiohttp


async def main():
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect("ws://127.0.0.1:8765/ws") as ws:
            await ws.send_json({"id": 1, "method": "connect", "params": {}})
            await ws.send_json({"id": 2, "method": "run", "params": {"tasks": ["普通任务"], "wait": True}})
            async for msg in ws:
                data = json.loads(msg.data)
                if data["type"] == "event" and data["event"] == "maa":
                    print(data["data"]["msg"])
                elif data["type"] == "response":
                    print("响应", data["id"], data["ok"], data.get("data") or data.get("error"))
                    if data["id"] == 2:
                        break


asyncio.run(main())
```

## 打包

`tools/install.py` 会把 `service/` 复制到安装目录。打包产物中没有附带 Python 运行时，用户需要自行安装 Python 和 `service/requirements.txt` 中的依赖；如需随包附带便携式 Python，可参考 [Agent 打包说明](./agent.md#打包)。

## 测试

```bash
pip install -r service/requirements.txt
python -m unittest discover -s service/tests -v
```

测试使用真实的 MaaFramework 和虚拟设备，覆盖 HTTP、WebSocket、令牌校验、服务开关与 Agent 子进程，CI（`check.yml`）中也会运行。
