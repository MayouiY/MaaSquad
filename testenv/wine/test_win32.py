"""Win32 端到端测试：启动测试目标窗口（target_app.py），验证 Win32 控制器、项目 pipeline 与 Web 服务。

需要在 Windows 环境中运行，Linux 上通过 Wine 执行：

    testenv/testenv.sh wine-test                                      # Linux + Wine
    python -m unittest discover -s testenv/wine -p "test_*.py" -v     # 真实 Windows

可用环境变量调整要测试的方式（逗号分隔，名称见 MaaWin32ScreencapMethodEnum / MaaWin32InputMethodEnum）：
    MAA_TEST_SCREENCAP   默认 GDI,ScreenDC
    MAA_TEST_INPUT       默认 Seize,SendMessage,PostMessage,LegacyEvent,SendMessageWithCursorPos,PostMessageWithCursorPos
"""

import asyncio
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

if sys.platform != "win32":
    raise unittest.SkipTest("Win32 端到端测试需要在 Windows 或 Wine 中运行")

WINE_DIR = Path(__file__).resolve().parent
REPO_DIR = WINE_DIR.parent.parent
SERVICE_DIR = REPO_DIR / "service"
sys.path.insert(0, str(SERVICE_DIR))

import aiohttp  # noqa: E402
from maa.controller import Win32Controller  # noqa: E402
from maa.define import MaaWin32InputMethodEnum, MaaWin32ScreencapMethodEnum  # noqa: E402
from maa.toolkit import Toolkit  # noqa: E402

from pi import ProjectInterface, load_jsonc  # noqa: E402
from runtime import MaaRuntime  # noqa: E402
from server import EventHub, WebService  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402

sys.path.insert(0, str(WINE_DIR))
from target_app import COLORS, HEIGHT, LAYOUT, TITLE, WIDTH  # noqa: E402

# Wine 中 PrintWindow 截图为纯黑，FramePool / DXGI 不可用，默认只测可用的方式
SCREENCAP_METHODS = os.environ.get("MAA_TEST_SCREENCAP", "GDI,ScreenDC").split(",")
INPUT_METHODS = os.environ.get(
    "MAA_TEST_INPUT",
    "Seize,SendMessage,PostMessage,LegacyEvent,SendMessageWithCursorPos,PostMessageWithCursorPos",
).split(",")
# LegacyEvent 的 input_text 在 Wine 中只产生按键，不产生 WM_CHAR
NO_TEXT_INPUT = {"LegacyEvent"}

OCR_MODEL = REPO_DIR / "assets" / "resource" / "model" / "ocr" / "rec.onnx"
LOG_DIR = Path(tempfile.gettempdir()) / "maasquad-win32-tests"


def center(box: list[int]) -> tuple[int, int]:
    x, y, w, h = box
    return x + w // 2, y + h // 2


def inside(box: list[int], x: int, y: int) -> bool:
    bx, by, bw, bh = box
    return bx <= x < bx + bw and by <= y < by + bh


class TargetWindow:
    """在子进程中运行 target_app.py，并读取它记录的输入事件。"""

    def __init__(self, workdir: Path):
        self.log_path = workdir / "events.jsonl"
        self.proc = subprocess.Popen([sys.executable, str(WINE_DIR / "target_app.py"), "--log", str(self.log_path)])
        ready = self.wait_for(lambda e: e["type"] == "ready", timeout=30, start=0)
        if ready is None:
            self.close()
            raise RuntimeError("测试目标窗口启动超时")
        self.hwnd = ready["hwnd"]

    def events(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        with open(self.log_path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def mark(self) -> int:
        return len(self.events())

    def wait_for(self, pred, timeout: float = 5, start: int = 0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events()[start:]:
                if pred(event):
                    return event
            if self.proc.poll() is not None:
                return None
            time.sleep(0.05)
        return None

    def typed_since(self, start: int) -> str:
        return "".join(e["char"] for e in self.events()[start:] if e["type"] == "char")

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class TargetTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Toolkit.init_option(str(LOG_DIR))
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.target = TargetWindow(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.target.close()
        cls.tmp.cleanup()

    def controller(self, screencap="GDI", input_method="PostMessage") -> Win32Controller:
        ctrl = Win32Controller(
            self.target.hwnd,
            screencap_method=MaaWin32ScreencapMethodEnum[screencap],
            mouse_method=MaaWin32InputMethodEnum[input_method],
            keyboard_method=MaaWin32InputMethodEnum[input_method],
        )
        ctrl.set_screenshot_use_raw_size(True)
        self.assertTrue(ctrl.post_connection().wait().succeeded, f"连接失败 screencap={screencap} input={input_method}")
        return ctrl


class Win32ControllerTest(TargetTestBase):
    def test_find_window(self):
        windows = [w for w in Toolkit.find_desktop_windows() if w.window_name == TITLE]
        self.assertTrue(windows, "find_desktop_windows 没有找到测试窗口")
        self.assertIn(self.target.hwnd, [getattr(w.hwnd, "value", w.hwnd) for w in windows])
        self.assertEqual(windows[0].class_name, "MaaSquadTestTarget")

    def test_screencap(self):
        for method in SCREENCAP_METHODS:
            with self.subTest(screencap=method):
                job = self.controller(screencap=method).post_screencap().wait()
                self.assertTrue(job.succeeded)
                image = job.get()  # BGR
                self.assertEqual(image.shape[:2], (HEIGHT, WIDTH))
                for key in ("red", "green", "blue"):
                    x, y = center(LAYOUT[key])
                    self.assertEqual(tuple(int(c) for c in image[y, x][::-1]), COLORS[key], key)
                self.assertEqual(tuple(int(c) for c in image[HEIGHT - 5, 5][::-1]), COLORS["background"])

    def test_input(self):
        for method in INPUT_METHODS:
            with self.subTest(input=method):
                ctrl = self.controller(input_method=method)
                x, y = center(LAYOUT["blue"])

                start = self.target.mark()
                self.assertTrue(ctrl.post_click(x, y).wait().succeeded)
                event = self.target.wait_for(lambda e: e["type"] == "mouse_down", start=start)
                self.assertIsNotNone(event, "没有收到点击")
                self.assertEqual((event["x"], event["y"]), (x, y))

                start = self.target.mark()
                self.assertTrue(ctrl.post_click_key(0x41).wait().succeeded)  # VK_A
                self.assertIsNotNone(self.target.wait_for(lambda e: e.get("vk") == 0x41, start=start), "没有收到按键")

                if method in NO_TEXT_INPUT:
                    continue
                start = self.target.mark()
                self.assertTrue(ctrl.post_input_text("maa").wait().succeeded)
                deadline = time.monotonic() + 5
                while self.target.typed_since(start) != "maa" and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(self.target.typed_since(start), "maa")

    def test_click_changes_screen(self):
        """点击按钮后画面变化，截图能看到。"""
        ctrl = self.controller()
        x, y = center(LAYOUT["button"])

        def button_color():
            image = ctrl.post_screencap().wait().get()
            return tuple(int(c) for c in image[y + 40, x - 140][::-1])  # 避开按钮上的文字

        before = button_color()
        ctrl.post_click(x, y).wait()
        deadline = time.monotonic() + 5
        while button_color() == before and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertNotEqual(button_color(), before)
        self.assertIn(button_color(), (COLORS["button"], COLORS["button_active"]))


def make_project_interface(base: Path) -> Path:
    """以项目的 interface.json 为基础，资源指向仓库中的 assets/resource，并启用 agent/main.py。"""
    data = load_jsonc(REPO_DIR / "assets" / "interface.json")
    for res in data["resource"]:
        res["path"] = [str(REPO_DIR / "assets" / "resource")]
    data["agent"] = {"child_exec": "python", "child_args": [str(REPO_DIR / "agent" / "main.py")]}
    path = base / "interface.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@unittest.skipUnless(OCR_MODEL.exists(), "缺少 OCR 模型，请先执行 testenv/testenv.sh setup 或 python tools/configure.py")
class WebServiceWin32Test(TargetTestBase, unittest.IsolatedAsyncioTestCase):
    """通过 Web 服务连接「桌面端」控制器，执行项目里的任务。"""

    async def asyncSetUp(self):
        self.workdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(self.workdir.name)
        self.conf = copy.deepcopy(DEFAULT_SETTINGS)
        self.conf["http"]["port"] = self.conf["websocket"]["port"] = 0
        self.conf["security"]["allow_raw_entry"] = True
        self.conf["maa"]["win32"].update(
            {"window_regex": f"^{TITLE}$", "screencap": "GDI", "mouse": "PostMessage", "keyboard": "PostMessage"}
        )
        hub = EventHub()
        self.runtime = MaaRuntime(ProjectInterface(make_project_interface(base)), self.conf, base, emit=hub.emit)
        self.service = WebService(self.runtime, self.conf, hub)
        await self.service.start()
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.service.stop()
        await asyncio.get_running_loop().run_in_executor(None, self.runtime.close)
        self.workdir.cleanup()

    async def call(self, method: str, path: str, body=None):
        url = f"http://127.0.0.1:{self.service.port_of('http')}{path}"
        async with self.session.request(method, url, json=body) as resp:
            return resp.status, await resp.json()

    async def test_project_tasks(self):
        status, data = await self.call("POST", "/api/connect", {"controller": "桌面端"})
        self.assertEqual(status, 200, data)
        self.assertEqual((data["data"]["controller_type"], data["data"]["agents"]), ("Win32", 1))

        # 截图接口返回的就是测试窗口的画面
        from PIL import Image

        url = f"http://127.0.0.1:{self.service.port_of('http')}/api/screenshot"
        async with self.session.get(url) as resp:
            image = Image.open(io.BytesIO(await resp.read())).convert("RGB")
        self.assertEqual(image.size, (WIDTH, HEIGHT))
        self.assertEqual(image.getpixel(center(LAYOUT["red"])), COLORS["red"])

        # 项目自带的自定义识别 / 动作示例：OCR 读出 42 与 17，选中较小的数字后由 Agent 点击 (10, 20)
        start = self.target.mark()
        status, data = await self.call("POST", "/api/runs?wait=1&timeout=120", {"tasks": ["自定义识别与动作示例"]})
        self.assertEqual(data["data"]["status"], "succeeded", data)
        event = self.target.wait_for(lambda e: e["type"] == "mouse_down", start=start)
        self.assertIsNotNone(event, "Agent 的 click_target 没有点到窗口")
        self.assertEqual((event["x"], event["y"]), (10, 20))

        status, data = await self.call("POST", "/api/runs?wait=1", {"tasks": ["普通任务", {"name": "选项任务", "option": {"任务选项1": "文本2"}}]})
        self.assertEqual(data["data"]["status"], "succeeded", data)

        # 直接执行 pipeline：OCR 找到 "MaaSquad" 文字并点击它
        start = self.target.mark()
        override = {"OcrTask": {"roi": LAYOUT["label"], "expected": "MaaSquad", "action": "Click"}}
        status, data = await self.call("POST", "/api/entry?wait=1&timeout=120", {"entry": "OcrTask", "pipeline_override": override})
        self.assertEqual(data["data"]["status"], "succeeded", data)
        event = self.target.wait_for(lambda e: e["type"] == "mouse_down", start=start)
        self.assertIsNotNone(event, "OCR 命中后没有点击")
        self.assertTrue(inside(LAYOUT["label"], event["x"], event["y"]), event)

        status, data = await self.call("POST", "/api/disconnect")
        self.assertFalse(data["data"]["connected"])


if __name__ == "__main__":
    unittest.main()
