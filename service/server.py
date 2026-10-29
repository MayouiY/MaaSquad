"""aiohttp 服务：按开关挂载 HTTP API、WebSocket 与网页控制台。"""

import asyncio
import hmac
import json
import logging
from pathlib import Path
from typing import Any, Optional

from aiohttp import WSMsgType, web

from api import WS_METHODS, Api
from runtime import MaaRuntime, ServiceError

logger = logging.getLogger("maa.web")

WEBUI_DIR = Path(__file__).parent / "webui"
EVENT_TYPES = ("status", "run", "maa")


def ok(data: Any, status: int = 200) -> web.Response:
    return web.json_response({"ok": True, "data": data}, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def fail(message: str, status: int = 400, code: str = "bad_request") -> web.Response:
    return web.json_response(
        {"ok": False, "error": {"code": code, "message": message}},
        status=status,
        dumps=lambda o: json.dumps(o, ensure_ascii=False),
    )


class WsClient:
    """一个 WebSocket 连接：消息经队列由单一协程按顺序发送，保证事件与响应的先后顺序。"""

    MAX_PENDING = 1000

    def __init__(self, ws: web.WebSocketResponse):
        self.ws = ws
        self.subscriptions: set[str] = set(EVENT_TYPES)
        self._queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._writer = asyncio.ensure_future(self._write())

    def send(self, message: dict[str, Any]) -> None:
        if self._queue.qsize() >= self.MAX_PENDING:  # 客户端读得太慢，丢弃以免内存无限增长
            return
        self._queue.put_nowait(json.dumps(message, ensure_ascii=False))

    async def _write(self) -> None:
        while True:
            text = await self._queue.get()
            if text is None or self.ws.closed:
                return
            try:
                await self.ws.send_str(text)
            except (ConnectionError, RuntimeError):
                return

    async def close(self) -> None:
        self._queue.put_nowait(None)
        await self._writer


class EventHub:
    """把运行时（任意线程）产生的事件转发给所有 WebSocket 客户端。"""

    def __init__(self):
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.clients: set[WsClient] = set()

    def emit(self, event: str, data: dict[str, Any]) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._broadcast, event, data)

    def _broadcast(self, event: str, data: dict[str, Any]) -> None:
        for client in list(self.clients):
            if event in client.subscriptions:
                client.send({"type": "event", "event": event, "data": data})


class WebService:
    def __init__(self, runtime: MaaRuntime, settings: dict[str, Any], hub: EventHub):
        self.runtime = runtime
        self.settings = settings
        self.hub = hub
        self.api = Api(runtime)
        self._runners: list[web.AppRunner] = []
        self.bound: dict[tuple[str, int], int] = {}

    # ------------------------------------------------------------ 中间件

    def _token_ok(self, request: web.Request) -> bool:
        token = self.settings["auth"]["token"]
        if not token:
            return True
        given = request.headers.get("X-Maa-Token") or request.query.get("token") or ""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            given = auth[len("Bearer ") :]
        return hmac.compare_digest(given.encode(), token.encode())

    def _protected(self, request: web.Request) -> bool:
        path = request.path
        ws = self.settings["websocket"]
        if ws["enabled"] and path == ws["path"]:
            return True
        return path.startswith("/api/") and path != "/api/health"

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        origin = request.headers.get("Origin")
        allowed = self.settings["http"]["cors_origins"]
        cors = origin and ("*" in allowed or origin in allowed)

        if request.method == "OPTIONS" and cors:
            response: web.StreamResponse = web.Response(status=204)
        elif self._protected(request) and not self._token_ok(request):
            response = fail("缺少或错误的访问令牌", 401, "unauthorized")
        else:
            try:
                response = await handler(request)
            except ServiceError as e:
                response = fail(e.message, e.status, e.code)
            except web.HTTPException:
                raise
            except Exception as e:
                logger.exception("请求处理失败: %s %s", request.method, request.path)
                response = fail(f"服务器内部错误: {e}", 500, "internal_error")

        if cors and not response.prepared:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Maa-Token"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        return response

    # ------------------------------------------------------------ HTTP

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        if not request.can_read_body:
            return {}
        try:
            body = await request.json()
        except json.JSONDecodeError as e:
            raise ServiceError(f"请求体不是合法的 JSON: {e}") from e
        if not isinstance(body, dict):
            raise ServiceError("请求体必须是 JSON 对象")
        return body

    def _add_http_routes(self, app: web.Application) -> None:
        api = self.api

        async def health(request):
            return ok({"status": "ok", "name": self.runtime.pi.name, "version": self.runtime.pi.version})

        async def interface(request):
            return ok(await api.interface({}))

        async def status(request):
            return ok(await api.status({}))

        async def devices(request):
            return ok(await api.devices({"type": request.query.get("type", "adb")}))

        async def connect(request):
            return ok(await api.connect(await self._body(request)))

        async def disconnect(request):
            return ok(await api.disconnect({}))

        async def create_run(request):
            body = await self._body(request)
            body["_source"] = "http"
            for key in ("wait", "timeout"):
                if key in request.query:
                    body[key] = request.query[key]
            return ok(await api.run(body), status=201)

        async def entry(request):
            body = await self._body(request)
            body["_source"] = "http"
            for key in ("wait", "timeout"):
                if key in request.query:
                    body[key] = request.query[key]
            return ok(await api.entry(body), status=201)

        async def list_runs(request):
            return ok(await api.runs({}))

        async def get_run(request):
            return ok(await api.get_run({"id": request.match_info["run_id"], **request.query}))

        async def cancel(request):
            return ok(await api.cancel({"id": request.match_info["run_id"]}))

        async def stop(request):
            return ok(await api.stop({}))

        async def screenshot(request):
            return web.Response(body=await api.screenshot_png(), content_type="image/png")

        app.router.add_get("/api/health", health)
        app.router.add_get("/api/interface", interface)
        app.router.add_get("/api/status", status)
        app.router.add_get("/api/devices", devices)
        app.router.add_post("/api/connect", connect)
        app.router.add_post("/api/disconnect", disconnect)
        app.router.add_post("/api/runs", create_run)
        app.router.add_get("/api/runs", list_runs)
        app.router.add_get("/api/runs/{run_id}", get_run)
        app.router.add_post("/api/runs/{run_id}/cancel", cancel)
        app.router.add_delete("/api/runs/{run_id}", cancel)
        app.router.add_post("/api/stop", stop)
        app.router.add_post("/api/entry", entry)
        app.router.add_get("/api/screenshot", screenshot)

    def _add_webui_routes(self, app: web.Application) -> None:
        async def index(request):
            ws = self.settings["websocket"]
            conf = {"port": self.port_of("websocket"), "path": ws["path"]} if ws["enabled"] else None
            html = (WEBUI_DIR / "index.html").read_text(encoding="utf-8")
            html = html.replace("<!--MAA_CONFIG-->", f"<script>window.MAA_WS = {json.dumps(conf)};</script>", 1)
            return web.Response(text=html, content_type="text/html")

        app.router.add_get("/", index)

    # ------------------------------------------------------------ WebSocket

    def _add_ws_route(self, app: web.Application) -> None:
        app.router.add_get(self.settings["websocket"]["path"], self._ws_handler)

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        client = WsClient(ws)
        self.hub.clients.add(client)
        pending: set[asyncio.Task] = set()

        client.send({"type": "hello", "data": {"status": self.runtime.status(), "methods": [*WS_METHODS, "subscribe", "ping"]}})
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    # 每个请求独立处理，耗时调用（如 wait=true）不阻塞后续消息
                    task = asyncio.ensure_future(self._ws_dispatch(client, msg.data))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.hub.clients.discard(client)
            for task in pending:
                task.cancel()
            await client.close()
        return ws

    async def _ws_dispatch(self, client: WsClient, raw: str) -> None:
        req_id = None
        try:
            try:
                req = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ServiceError(f"消息不是合法的 JSON: {e}") from e
            if not isinstance(req, dict):
                raise ServiceError("消息必须是 JSON 对象")
            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params") or {}
            if not isinstance(params, dict):
                raise ServiceError("params 必须是对象")

            if method == "ping":
                data: Any = "pong"
            elif method == "subscribe":
                events = params.get("events", list(EVENT_TYPES))
                if not isinstance(events, list) or any(e not in EVENT_TYPES for e in events):
                    raise ServiceError(f"events 必须是 {list(EVENT_TYPES)} 的子集")
                client.subscriptions = set(events)
                data = {"events": events}
            elif method in WS_METHODS:
                if method in ("run", "entry"):
                    params["_source"] = "websocket"
                data = await getattr(self.api, method)(params)
            else:
                raise ServiceError(f"未知方法: {method}", 404, "not_found")
            reply = {"type": "response", "id": req_id, "ok": True, "data": data}
        except ServiceError as e:
            reply = {"type": "response", "id": req_id, "ok": False, "error": {"code": e.code, "message": e.message}}
        except Exception as e:
            logger.exception("WebSocket 请求处理失败")
            reply = {"type": "response", "id": req_id, "ok": False, "error": {"code": "internal_error", "message": str(e)}}
        client.send(reply)

    # ------------------------------------------------------------ 启动

    def build_apps(self) -> dict[tuple[str, int], web.Application]:
        """按 (host, port) 分组创建应用；http 与 websocket 地址相同时共用一个端口。"""
        http, ws, webui = self.settings["http"], self.settings["websocket"], self.settings["webui"]
        apps: dict[tuple[str, int], web.Application] = {}

        def app_for(host: str, port: int) -> web.Application:
            key = (host, port)
            if key not in apps:
                apps[key] = web.Application(middlewares=[self._middleware])
            return apps[key]

        if http["enabled"]:
            app = app_for(http["host"], http["port"])
            self._add_http_routes(app)
            if webui["enabled"]:
                self._add_webui_routes(app)
        if ws["enabled"]:
            self._add_ws_route(app_for(ws["host"], ws["port"]))
        return apps

    async def start(self) -> None:
        self.hub.loop = asyncio.get_running_loop()
        for (host, port), app in self.build_apps().items():
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()
            self._runners.append(runner)
            # 端口为 0 时由系统分配，记录实际端口
            sockets = site._server.sockets if site._server else []
            self.bound[(host, port)] = sockets[0].getsockname()[1] if sockets else port

    async def stop(self) -> None:
        for client in list(self.hub.clients):
            await client.ws.close()
        for runner in self._runners:
            await runner.cleanup()
        self._runners.clear()
        self.api.close()

    def port_of(self, section: str) -> int:
        conf = self.settings[section]
        return self.bound.get((conf["host"], conf["port"]), conf["port"])

    def endpoints(self) -> list[str]:
        http, ws, webui = self.settings["http"], self.settings["websocket"], self.settings["webui"]
        lines = []
        if http["enabled"]:
            base = f"http://{http['host']}:{self.port_of('http')}"
            lines.append(f"HTTP API   : {base}/api")
            if webui["enabled"]:
                lines.append(f"网页控制台 : {base}/")
        if ws["enabled"]:
            lines.append(f"WebSocket  : ws://{ws['host']}:{self.port_of('websocket')}{ws['path']}")
        return lines
