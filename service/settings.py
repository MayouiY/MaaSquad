"""Web 服务配置与开关。

配置文件默认位于 ``<用户目录>/config/web_service.json``，首次运行时自动生成。
命令行参数优先于配置文件，详见 ``main.py``。
"""

import copy
import ipaddress
import json
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS: dict[str, Any] = {
    # HTTP API 开关：REST 接口，路径前缀 /api
    "http": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8765,
        # 允许跨域访问的来源，例如 ["http://localhost:3000"]，"*" 表示全部
        "cors_origins": [],
    },
    # WebSocket 开关：双向调用 + 实时事件推送
    # 与 http 的 host/port 相同时共用同一端口
    "websocket": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/ws",
    },
    # 网页控制台开关：由 HTTP 服务提供，需同时开启 http
    "webui": {
        "enabled": True,
    },
    # 事件推送开关（经 WebSocket 推送）
    "events": {
        # 任务级事件：Tasker.Task.Starting / Succeeded / Failed
        "task": True,
        # 节点级事件：Node.PipelineNode / Node.Recognition / Node.Action
        "node": True,
    },
    "auth": {
        # 非空时，所有 API 与 WebSocket 请求都需要携带该令牌
        "token": "",
    },
    "security": {
        # 是否允许监听非本机地址。开启时必须设置 auth.token
        "allow_remote": False,
        # 是否允许直接按 pipeline 入口节点执行（绕过 interface.json 中编排好的任务）
        "allow_raw_entry": False,
    },
    "maa": {
        # interface.json 路径，留空则自动查找
        "interface": "",
        # 默认使用的控制器 / 资源名称（对应 interface.json），留空取第一个
        "controller": "",
        "resource": "",
        # 服务启动后立即连接设备并加载资源
        "auto_connect": False,
        "adb": {
            # 留空则自动搜索设备，取第一个
            "path": "",
            "address": "",
            "config": {},
        },
        "win32": {
            # 留空则按 interface.json 中的 class_regex / window_regex 查找窗口
            "class_regex": "",
            "window_regex": "",
            # 留空则使用 interface.json 中的截图 / 鼠标 / 键盘方式，例如在 Wine 中改用 "GDI" 截图
            "screencap": "",
            "mouse": "",
            "keyboard": "",
        },
        "agent": {
            # interface.json 配置了 agent 时，是否随服务一起启动
            "enabled": True,
            # 等待 AgentServer 连接的超时时间（毫秒）
            "timeout": 30000,
        },
        # 保留的已完成运行记录数量
        "history_limit": 100,
    },
}


class SettingsError(ValueError):
    pass


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并字典，override 中的值覆盖 base；返回新字典。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_settings(path: Path, create_if_missing: bool = True) -> dict[str, Any]:
    if not path.exists():
        if create_if_missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_SETTINGS, f, ensure_ascii=False, indent=4)
        return copy.deepcopy(DEFAULT_SETTINGS)

    with open(path, "r", encoding="utf-8") as f:
        try:
            user = json.load(f)
        except json.JSONDecodeError as e:
            raise SettingsError(f"配置文件 {path} 不是合法的 JSON: {e}") from e

    if not isinstance(user, dict):
        raise SettingsError(f"配置文件 {path} 顶层必须是对象")
    return deep_merge(DEFAULT_SETTINGS, user)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_settings(settings: dict[str, Any]) -> list[str]:
    """校验配置，返回警告列表；配置不安全或不可用时抛出 SettingsError。"""
    warnings = []
    http = settings["http"]
    ws = settings["websocket"]
    security = settings["security"]
    token = settings["auth"]["token"]

    if not http["enabled"] and not ws["enabled"]:
        raise SettingsError("http 与 websocket 均未开启，没有可启动的服务")

    for name, section in (("http", http), ("websocket", ws)):
        if not section["enabled"]:
            continue
        port = section["port"]
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise SettingsError(f"{name}.port 无效: {port}")
        if not is_loopback(section["host"]):
            if not security["allow_remote"]:
                raise SettingsError(
                    f"{name}.host={section['host']} 不是本机地址。"
                    "如确需对外暴露，请开启 security.allow_remote 并设置 auth.token"
                )
            if not token:
                raise SettingsError("开启 security.allow_remote 时必须设置 auth.token")

    if ws["enabled"] and not str(ws["path"]).startswith("/"):
        raise SettingsError("websocket.path 必须以 / 开头")

    if settings["webui"]["enabled"] and not http["enabled"]:
        warnings.append("webui 依赖 http 服务，http 未开启时 webui 不会启动")

    return warnings
