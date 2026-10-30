"""HTTP 与 WebSocket 共用的调用层：把阻塞的运行时方法放到线程池里执行。"""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from runtime import MaaRuntime, ServiceError

MAX_WAIT_SECONDS = 3600.0


def _parse_timeout(value: Any) -> Optional[float]:
    if value is None or value == "":
        return MAX_WAIT_SECONDS
    try:
        timeout = float(value)
    except (TypeError, ValueError) as e:
        raise ServiceError(f"timeout 必须是数字（秒）: {value}") from e
    if timeout < 0:
        raise ServiceError("timeout 不能为负数")
    return min(timeout, MAX_WAIT_SECONDS)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "y")
    return bool(value)


class Api:
    def __init__(self, runtime: MaaRuntime):
        self.runtime = runtime
        self._pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="maa-api")

    async def _call(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    @staticmethod
    async def _wait_done(run, timeout: Optional[float]) -> None:
        """异步等待运行结束；客户端断开时随请求一起取消，不占用线程。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (timeout if timeout is not None else MAX_WAIT_SECONDS)
        while not run.done.is_set() and loop.time() < deadline:
            await asyncio.sleep(0.1)
        # 运行时在结束前已把最终事件排入事件循环，让出一次，使事件先于响应发给 WebSocket 客户端
        await asyncio.sleep(0)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # 以下方法的参数均为 dict，返回值可直接 JSON 序列化

    async def interface(self, params: dict[str, Any]) -> Any:
        return self.runtime.pi.describe()

    async def status(self, params: dict[str, Any]) -> Any:
        return self.runtime.status()

    async def devices(self, params: dict[str, Any]) -> Any:
        return await self._call(self.runtime.devices, params.get("type", "adb"))

    async def connect(self, params: dict[str, Any]) -> Any:
        return await self._call(self.runtime.connect, params)

    async def disconnect(self, params: dict[str, Any]) -> Any:
        return await self._call(self.runtime.disconnect)

    async def run(self, params: dict[str, Any]) -> Any:
        """提交编排好的任务；wait=true 时等待执行结束再返回。"""
        run = await self._call(self.runtime.submit, params, params.get("_source", "api"))
        if _truthy(params.get("wait")):
            await self._wait_done(run, _parse_timeout(params.get("timeout")))
        return run.to_dict()

    async def entry(self, params: dict[str, Any]) -> Any:
        run = await self._call(self.runtime.submit_entry, params, params.get("_source", "api"))
        if _truthy(params.get("wait")):
            await self._wait_done(run, _parse_timeout(params.get("timeout")))
        return run.to_dict()

    async def runs(self, params: dict[str, Any]) -> Any:
        return self.runtime.list_runs()

    async def get_run(self, params: dict[str, Any]) -> Any:
        run = self.runtime.get_run(str(params.get("id", "")))
        if _truthy(params.get("wait")):
            await self._wait_done(run, _parse_timeout(params.get("timeout")))
        return run.to_dict()

    async def cancel(self, params: dict[str, Any]) -> Any:
        return (await self._call(self.runtime.cancel, str(params.get("id", "")))).to_dict()

    async def stop(self, params: dict[str, Any]) -> Any:
        return await self._call(self.runtime.stop)

    async def screenshot_png(self) -> bytes:
        return await self._call(self.runtime.screenshot)

    async def screenshot(self, params: dict[str, Any]) -> Any:
        png = await self.screenshot_png()
        return {"format": "png", "base64": base64.b64encode(png).decode()}


# WebSocket 可调用的方法名
WS_METHODS = (
    "interface",
    "status",
    "devices",
    "connect",
    "disconnect",
    "run",
    "entry",
    "runs",
    "get_run",
    "cancel",
    "stop",
    "screenshot",
)
