"""MaaSquad Web 服务入口。

    python service/main.py                      # 按 config/web_service.json 启动
    python service/main.py --no-websocket       # 临时关闭 WebSocket
    python service/main.py --http-port 9000 --token xxx

命令行参数优先于配置文件。
"""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from maa.toolkit import Toolkit

from pi import InterfaceError, ProjectInterface
from runtime import MaaRuntime, ServiceError
from server import EventHub, WebService
from settings import SettingsError, load_settings, validate_settings

SERVICE_DIR = Path(__file__).parent.resolve()


def find_interface(explicit: str) -> Path:
    candidates = [Path(explicit)] if explicit else [
        Path.cwd() / "interface.json",
        SERVICE_DIR.parent / "interface.json",  # 打包后：install/service/../interface.json
        SERVICE_DIR.parent / "assets" / "interface.json",  # 开发时：仓库根目录/assets/interface.json
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise InterfaceError("找不到 interface.json，请通过 --interface 或 maa.interface 指定")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MaaSquad Web 服务（HTTP / WebSocket）")
    p.add_argument("--config", help="配置文件路径，默认 <user-dir>/config/web_service.json")
    p.add_argument("--user-dir", default=".", help="用户数据目录（日志、配置），默认当前目录")
    p.add_argument("--interface", help="interface.json 路径")

    for name, dest in (("http", "http"), ("websocket", "websocket"), ("webui", "webui")):
        g = p.add_mutually_exclusive_group()
        g.add_argument(f"--{name}", dest=dest, action="store_true", default=None, help=f"开启 {name}")
        g.add_argument(f"--no-{name}", dest=dest, action="store_false", help=f"关闭 {name}")

    p.add_argument("--host", help="同时设置 http 与 websocket 的监听地址")
    p.add_argument("--http-port", type=int)
    p.add_argument("--ws-port", type=int)
    p.add_argument("--token", help="访问令牌")
    p.add_argument("--allow-remote", action="store_true", default=None, help="允许监听非本机地址（需要 --token）")
    p.add_argument("--controller", help="默认控制器名称")
    p.add_argument("--resource", help="默认资源名称")
    p.add_argument("--auto-connect", action="store_true", default=None, help="启动后立即连接设备")
    return p.parse_args(argv)


def apply_args(settings: dict, args: argparse.Namespace) -> None:
    for section in ("http", "websocket", "webui"):
        value = getattr(args, section)
        if value is not None:
            settings[section]["enabled"] = value
    if args.host:
        settings["http"]["host"] = settings["websocket"]["host"] = args.host
    if args.http_port is not None:
        settings["http"]["port"] = args.http_port
    if args.ws_port is not None:
        settings["websocket"]["port"] = args.ws_port
    if args.token is not None:
        settings["auth"]["token"] = args.token
    if args.allow_remote:
        settings["security"]["allow_remote"] = True
    if args.interface:
        settings["maa"]["interface"] = args.interface
    if args.controller:
        settings["maa"]["controller"] = args.controller
    if args.resource:
        settings["maa"]["resource"] = args.resource
    if args.auto_connect:
        settings["maa"]["auto_connect"] = True


async def serve(runtime: MaaRuntime, service: WebService, auto_connect: bool, log: logging.Logger) -> None:
    await service.start()
    for line in service.endpoints():
        log.info(line)

    if auto_connect:
        try:
            await service.api.connect({})
            log.info("设备已连接，资源加载完成")
        except ServiceError as e:
            log.error("自动连接失败: %s", e.message)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    await stop.wait()

    log.info("正在停止服务…")
    await service.stop()


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s][%(levelname)s] %(message)s")
    log = logging.getLogger("maa.web")
    args = parse_args(argv)

    user_dir = Path(args.user_dir).resolve()
    config_path = Path(args.config) if args.config else user_dir / "config" / "web_service.json"

    try:
        settings = load_settings(config_path)
        apply_args(settings, args)
        for warning in validate_settings(settings):
            log.warning(warning)
        pi = ProjectInterface(find_interface(settings["maa"]["interface"]))
    except (SettingsError, InterfaceError) as e:
        log.error("%s", e)
        return 1

    log.info("配置文件: %s", config_path)
    log.info("项目: %s %s (%s)", pi.name, pi.version, pi.path)
    Toolkit.init_option(str(user_dir))

    hub = EventHub()
    runtime = MaaRuntime(pi, settings, user_dir, emit=hub.emit)
    service = WebService(runtime, settings, hub)
    try:
        asyncio.run(serve(runtime, service, settings["maa"]["auto_connect"], log))
    except OSError as e:
        log.error("端口监听失败: %s", e)
        return 1
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
