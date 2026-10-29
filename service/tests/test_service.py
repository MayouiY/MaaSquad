"""端到端测试：真实 MaaFramework + 调试控制器，验证 HTTP / WebSocket / 开关 / Agent。"""

import asyncio
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = SERVICE_DIR.parent
sys.path.insert(0, str(SERVICE_DIR))

import aiohttp  # noqa: E402
from maa.toolkit import Toolkit  # noqa: E402

from pi import ProjectInterface  # noqa: E402
from runtime import MaaRuntime  # noqa: E402
from server import EventHub, WebService  # noqa: E402
from settings import DEFAULT_SETTINGS, SettingsError, validate_settings  # noqa: E402

PIPELINE = {
    "Quick": {"post_delay": 0},
    # 纯黑截图上永远找不到该颜色，节点会一直重试直到超时或被停止
    "Slow": {"recognition": "ColorMatch", "lower": [250, 0, 250], "upper": [255, 5, 255], "timeout": 30000},
    "Fail": {"recognition": "OCR", "expected": "不可能出现的文字", "timeout": 100},
    "ClickByAgent": {"action": "Custom", "custom_action": "click_target", "custom_action_param": {"target": [10, 20]}},
    "Clicked": {"action": "Click", "target": [1, 2]},
}


def make_interface(base: Path, with_agent=False) -> Path:
    (base / "resource" / "pipeline").mkdir(parents=True)
    (base / "resource" / "pipeline" / "test.json").write_text(json.dumps(PIPELINE), encoding="utf-8")
    interface = {
        "interface_version": 2,
        "name": "TestProject",
        "version": "0.1.0",
        "controller": [{"name": "调试", "type": "Adb"}],
        "resource": [{"name": "默认", "path": ["./resource"]}],
        "task": [
            {"name": "快速任务", "entry": "Quick", "option": ["点击"]},
            {"name": "慢任务", "entry": "Slow"},
            {"name": "失败任务", "entry": "Fail"},
            {"name": "Agent 任务", "entry": "ClickByAgent"},
        ],
        "option": {
            "点击": {
                "cases": [
                    {"name": "不点", "pipeline_override": {}},
                    {"name": "点", "pipeline_override": {"Quick": {"next": ["Clicked"]}}},
                ]
            }
        },
        "preset": [{"name": "两个快速", "task": [{"name": "快速任务"}, {"name": "快速任务", "option": {"点击": "点"}}]}],
    }
    if with_agent == "broken":
        interface["agent"] = {"child_exec": str(base / "不存在的程序")}
    elif with_agent:
        interface["agent"] = {"child_exec": "python", "child_args": [str(REPO_DIR / "agent" / "main.py")]}
    path = base / "interface.json"
    path.write_text(json.dumps(interface, ensure_ascii=False), encoding="utf-8")
    return path


class ServiceTestBase(unittest.IsolatedAsyncioTestCase):
    with_agent = False

    def settings(self) -> dict:
        s = copy.deepcopy(DEFAULT_SETTINGS)
        s["http"]["port"] = s["websocket"]["port"] = 0
        return s

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        Toolkit.init_option(str(base))
        self.pi = ProjectInterface(make_interface(base, self.with_agent))
        self.conf = self.settings()
        self.hub = EventHub()
        self.runtime = MaaRuntime(self.pi, self.conf, base, emit=self.hub.emit)
        self.service = WebService(self.runtime, self.conf, self.hub)
        await self.service.start()
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.service.stop()
        await asyncio.get_running_loop().run_in_executor(None, self.runtime.close)
        self.tmp.cleanup()

    def url(self, path: str, section: str = "http") -> str:
        return f"http://127.0.0.1:{self.service.port_of(section)}{path}"

    async def call(self, method: str, path: str, body=None, headers=None):
        async with self.session.request(method, self.url(path), json=body, headers=headers) as resp:
            return resp.status, await resp.json()

    async def connect_debug(self):
        status, data = await self.call("POST", "/api/connect", {"controller_type": "Debug"})
        self.assertEqual(status, 200, data)
        return data["data"]


class HttpApiTest(ServiceTestBase):
    async def test_flow(self):
        status, data = await self.call("POST", "/api/runs", {"tasks": ["快速任务"]})
        self.assertEqual((status, data["error"]["code"]), (409, "not_connected"))

        info = await self.connect_debug()
        self.assertTrue(info["connected"])
        self.assertEqual(info["controller_type"], "Debug")

        status, data = await self.call(
            "POST", "/api/runs?wait=true", {"tasks": ["快速任务", {"name": "快速任务", "option": {"点击": "点"}}, "失败任务"]}
        )
        self.assertEqual(status, 201, data)
        run = data["data"]
        self.assertEqual(run["status"], "failed")
        self.assertEqual([t["status"] for t in run["tasks"]], ["succeeded", "succeeded", "failed"])
        self.assertEqual(run["tasks"][1]["nodes"], ["Quick", "Clicked"])
        self.assertIn(("click", 1, 2), self.runtime._controller.actions)

        status, data = await self.call("GET", f"/api/runs/{run['id']}")
        self.assertEqual(data["data"]["id"], run["id"])
        status, data = await self.call("GET", "/api/runs")
        self.assertEqual(len(data["data"]), 1)

    async def test_preset_and_stop_on_failure(self):
        await self.connect_debug()
        _, data = await self.call("POST", "/api/runs?wait=1", {"preset": "两个快速"})
        self.assertEqual(data["data"]["status"], "succeeded")
        self.assertEqual(len(data["data"]["tasks"]), 2)

        _, data = await self.call("POST", "/api/runs?wait=1", {"tasks": ["失败任务", "快速任务"], "stop_on_failure": True})
        self.assertEqual([t["status"] for t in data["data"]["tasks"]], ["failed", "skipped"])

    async def test_bad_requests(self):
        await self.connect_debug()
        for body in ({}, {"tasks": []}, {"tasks": ["不存在"]}, {"tasks": [{"name": "快速任务", "option": {"点击": "乱填"}}]}, {"tasks": ["快速任务"], "preset": "两个快速"}):
            status, data = await self.call("POST", "/api/runs", body)
            self.assertEqual(status, 400, body)
            self.assertFalse(data["ok"])
        async with self.session.post(self.url("/api/runs"), data="not json") as resp:
            self.assertEqual(resp.status, 400)
        status, _ = await self.call("GET", "/api/runs/nope")
        self.assertEqual(status, 404)

    async def test_stop_and_cancel(self):
        await self.connect_debug()
        _, slow = await self.call("POST", "/api/runs", {"tasks": ["慢任务"]})
        _, queued = await self.call("POST", "/api/runs", {"tasks": ["快速任务"]})
        _, queued2 = await self.call("POST", "/api/runs", {"tasks": ["快速任务"]})

        status, data = await self.call("DELETE", f"/api/runs/{queued2['data']['id']}")
        self.assertEqual(data["data"]["status"], "cancelled")

        status, data = await self.call("POST", "/api/connect", {"controller_type": "Debug"})
        self.assertEqual(status, 409)

        await asyncio.sleep(0.5)
        start = time.monotonic()
        status, data = await self.call("POST", "/api/stop")
        self.assertEqual(data["data"]["stopped"], slow["data"]["id"])
        self.assertEqual(data["data"]["cancelled"], [queued["data"]["id"]])

        _, data = await self.call("GET", f"/api/runs/{slow['data']['id']}?wait=1&timeout=10")
        self.assertEqual(data["data"]["status"], "stopped")
        self.assertLess(time.monotonic() - start, 10)

    async def test_screenshot_and_raw_entry_switch(self):
        await self.connect_debug()
        async with self.session.get(self.url("/api/screenshot")) as resp:
            self.assertEqual(resp.content_type, "image/png")
            self.assertTrue((await resp.read()).startswith(b"\x89PNG"))

        status, data = await self.call("POST", "/api/entry", {"entry": "Quick"})
        self.assertEqual(status, 403)
        self.conf["security"]["allow_raw_entry"] = True
        status, data = await self.call("POST", "/api/entry?wait=1", {"entry": "Quick", "pipeline_override": {"Quick": {"next": ["Clicked"]}}})
        self.assertEqual(data["data"]["tasks"][0]["nodes"], ["Quick", "Clicked"])
        status, _ = await self.call("POST", "/api/entry", {"entry": "NotExist"})
        self.assertEqual(status, 404)

    async def test_webui(self):
        async with self.session.get(self.url("/")) as resp:
            html = await resp.text()
        self.assertIn('window.MAA_WS = {"port"', html)


class WebSocketTest(ServiceTestBase):
    async def test_ws_flow_and_events(self):
        async with self.session.ws_connect(self.url("/ws", "websocket")) as ws:
            hello = await ws.receive_json()
            self.assertEqual(hello["type"], "hello")
            self.assertIn("run", hello["data"]["methods"])

            await ws.send_json({"id": 1, "method": "connect", "params": {"controller_type": "Debug"}})
            await ws.send_json({"id": 2, "method": "ping"})

            responses, events = {}, []

            async def read_until(pred, timeout=20):
                deadline = time.monotonic() + timeout
                while not pred():
                    msg = await asyncio.wait_for(ws.receive_json(), deadline - time.monotonic())
                    if msg["type"] == "response":
                        responses[msg["id"]] = msg
                    else:
                        events.append(msg)

            await read_until(lambda: 1 in responses and 2 in responses)
            self.assertTrue(responses[1]["ok"], responses[1])
            self.assertEqual(responses[2]["data"], "pong")

            await ws.send_json({"id": 3, "method": "run", "params": {"tasks": ["快速任务"], "wait": True}})
            await read_until(lambda: 3 in responses)
            run = responses[3]["data"]
            self.assertEqual(run["status"], "succeeded")
            self.assertEqual(run["source"], "websocket")

            names = [e["event"] for e in events]
            self.assertIn("status", names)
            run_events = [e["data"]["status"] for e in events if e["event"] == "run"]
            self.assertEqual(run_events[0], "queued")
            self.assertEqual(run_events[-1], "succeeded")
            msgs = [e["data"]["msg"] for e in events if e["event"] == "maa"]
            self.assertIn("Tasker.Task.Starting", msgs)
            self.assertIn("Tasker.Task.Succeeded", msgs)
            self.assertTrue(any(m.startswith("Node.") for m in msgs))

            # 只订阅 run 事件后不再收到 maa 事件
            await ws.send_json({"id": 4, "method": "subscribe", "params": {"events": ["run"]}})
            await read_until(lambda: 4 in responses)
            events.clear()
            await ws.send_json({"id": 5, "method": "run", "params": {"tasks": ["快速任务"], "wait": True}})
            await read_until(lambda: 5 in responses)
            await asyncio.sleep(0.2)
            self.assertTrue(events)
            self.assertEqual({e["event"] for e in events}, {"run"})

            for req in ({"id": 6, "method": "nope"}, {"id": 7, "method": "run", "params": {"tasks": ["不存在"]}}):
                await ws.send_json(req)
            await ws.send_str("garbage")
            await read_until(lambda: 6 in responses and 7 in responses and None in responses)
            self.assertEqual(responses[6]["error"]["code"], "not_found")
            self.assertFalse(responses[7]["ok"])
            self.assertFalse(responses[None]["ok"])

            await ws.send_json({"id": 8, "method": "screenshot"})
            await read_until(lambda: 8 in responses)
            self.assertEqual(responses[8]["data"]["format"], "png")


class AuthTest(ServiceTestBase):
    def settings(self):
        s = super().settings()
        s["auth"]["token"] = "secret"
        return s

    async def test_token_required(self):
        status, _ = await self.call("GET", "/api/health")
        self.assertEqual(status, 200)  # 健康检查不需要令牌
        status, data = await self.call("GET", "/api/status")
        self.assertEqual((status, data["error"]["code"]), (401, "unauthorized"))
        status, _ = await self.call("GET", "/api/status", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)
        status, _ = await self.call("GET", "/api/status", headers={"Authorization": "Bearer secret"})
        self.assertEqual(status, 200)
        status, _ = await self.call("GET", "/api/status", headers={"X-Maa-Token": "secret"})
        self.assertEqual(status, 200)

        with self.assertRaises(aiohttp.WSServerHandshakeError):
            await self.session.ws_connect(self.url("/ws", "websocket"))
        async with self.session.ws_connect(self.url("/ws?token=secret", "websocket")) as ws:
            self.assertEqual((await ws.receive_json())["type"], "hello")


class SeparatePortsAndSwitchesTest(ServiceTestBase):
    def settings(self):
        s = super().settings()
        s["websocket"]["host"] = "localhost"  # 与 http 不同的地址 → 独立端口
        s["webui"]["enabled"] = False
        return s

    async def test_switches(self):
        self.assertNotEqual(self.service.port_of("http"), self.service.port_of("websocket"))
        async with self.session.get(self.url("/")) as resp:
            self.assertEqual(resp.status, 404)  # webui 关闭
        async with self.session.get(self.url("/ws")) as resp:
            self.assertEqual(resp.status, 404)  # http 端口上没有 ws
        ws_base = f"http://localhost:{self.service.port_of('websocket')}"
        async with self.session.get(ws_base + "/api/status") as resp:
            self.assertEqual(resp.status, 404)  # ws 端口上没有 http api
        async with self.session.ws_connect(ws_base + "/ws") as ws:
            self.assertEqual((await ws.receive_json())["type"], "hello")


class AgentTest(ServiceTestBase):
    with_agent = True

    async def test_agent_custom_action(self):
        info = await self.connect_debug()
        self.assertEqual(info["agents"], 1)
        _, data = await self.call("POST", "/api/runs?wait=1", {"tasks": ["Agent 任务"]})
        self.assertEqual(data["data"]["status"], "succeeded", data)
        self.assertIn(("click", 10, 20), self.runtime._controller.actions)

        status, data = await self.call("POST", "/api/disconnect")
        self.assertFalse(data["data"]["connected"])


class BrokenAgentTest(ServiceTestBase):
    with_agent = "broken"

    async def test_agent_start_failure(self):
        status, data = await self.call("POST", "/api/connect", {"controller_type": "Debug"})
        self.assertEqual((status, data["error"]["code"]), (500, "agent_error"))
        _, data = await self.call("GET", "/api/status")
        self.assertFalse(data["data"]["connected"])


class CorsTest(ServiceTestBase):
    def settings(self):
        s = super().settings()
        s["http"]["cors_origins"] = ["http://allowed.example"]
        return s

    async def test_cors(self):
        async with self.session.options(self.url("/api/runs"), headers={"Origin": "http://allowed.example"}) as resp:
            self.assertEqual(resp.status, 204)
            self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "http://allowed.example")
        async with self.session.get(self.url("/api/status"), headers={"Origin": "http://evil.example"}) as resp:
            self.assertNotIn("Access-Control-Allow-Origin", resp.headers)


class SettingsTest(unittest.TestCase):
    def test_remote_requires_token(self):
        s = copy.deepcopy(DEFAULT_SETTINGS)
        s["http"]["host"] = "0.0.0.0"
        with self.assertRaises(SettingsError):
            validate_settings(s)
        s["security"]["allow_remote"] = True
        with self.assertRaises(SettingsError):
            validate_settings(s)
        s["auth"]["token"] = "t"
        validate_settings(s)

    def test_all_disabled(self):
        s = copy.deepcopy(DEFAULT_SETTINGS)
        s["http"]["enabled"] = s["websocket"]["enabled"] = False
        with self.assertRaises(SettingsError):
            validate_settings(s)

    def test_webui_without_http_warns(self):
        s = copy.deepcopy(DEFAULT_SETTINGS)
        s["http"]["enabled"] = False
        self.assertTrue(validate_settings(s))


if __name__ == "__main__":
    unittest.main()
