"""根据 interface.json 的 controller 配置创建 MaaFramework 控制器。"""

import re
from pathlib import Path
from typing import Any, Optional

import numpy
from maa.controller import AdbController, Controller, CustomController, Win32Controller
from maa.define import MaaControllerFeatureEnum, MaaWin32InputMethodEnum, MaaWin32ScreencapMethodEnum
from maa.toolkit import Toolkit


class ControllerError(RuntimeError):
    pass


class DebugController(CustomController):
    """不连接真实设备的调试控制器。

    截图返回 ``image`` 指定的图片（需要 Pillow）或纯黑画面，所有输入操作直接成功。
    用于在没有设备的环境里联调 Web 接口。在请求中使用 ``"controller_type": "Debug"`` 启用。
    """

    def __init__(self, image: Optional[str] = None, width: int = 1280, height: int = 720):
        super().__init__()
        self._frame = self._load(image, width, height)
        self.actions: list[tuple] = []

    @staticmethod
    def _load(image: Optional[str], width: int, height: int) -> numpy.ndarray:
        if image:
            try:
                from PIL import Image
            except ModuleNotFoundError as e:
                raise ControllerError("调试控制器加载图片需要安装 Pillow") from e
            rgb = numpy.asarray(Image.open(image).convert("RGB"))
            return numpy.ascontiguousarray(rgb[:, :, ::-1])  # MaaFramework 使用 BGR
        return numpy.zeros((height, width, 3), dtype=numpy.uint8)

    def connect(self) -> bool:
        return True

    def get_features(self) -> int:
        # 直接使用 click / click_key，便于记录操作
        return MaaControllerFeatureEnum.Null

    def request_uuid(self) -> str:
        return "debug-controller"

    def start_app(self, intent: str) -> bool:
        self.actions.append(("start_app", intent))
        return True

    def stop_app(self, intent: str) -> bool:
        self.actions.append(("stop_app", intent))
        return True

    def screencap(self) -> numpy.ndarray:
        return self._frame

    def click(self, x: int, y: int) -> bool:
        self.actions.append(("click", x, y))
        return True

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int) -> bool:
        self.actions.append(("swipe", x1, y1, x2, y2, duration))
        return True

    def touch_down(self, contact: int, x: int, y: int, pressure: int) -> bool:
        return True

    def touch_move(self, contact: int, x: int, y: int, pressure: int) -> bool:
        return True

    def touch_up(self, contact: int) -> bool:
        return True

    def click_key(self, keycode: int) -> bool:
        self.actions.append(("click_key", keycode))
        return True

    def input_text(self, text: str) -> bool:
        self.actions.append(("input_text", text))
        return True

    def key_down(self, keycode: int) -> bool:
        return True

    def key_up(self, keycode: int) -> bool:
        return True


def _win32_enum(enum_cls, name: Optional[str], default: int) -> int:
    if not name:
        return default
    try:
        return int(enum_cls[name])
    except KeyError as e:
        raise ControllerError(f"未知的 Win32 方式: {name}") from e


def _create_adb(ctrl: dict[str, Any], adb: dict[str, Any]) -> Controller:
    adb_path = adb.get("path") or ""
    address = adb.get("address") or ""
    config = dict(ctrl.get("adb") or {})
    config.update(adb.get("config") or {})

    screencap = input_ = None
    if not adb_path or not address:
        devices = Toolkit.find_adb_devices()
        if address:
            devices = [d for d in devices if d.address == address]
        if not devices:
            raise ControllerError("未找到 ADB 设备，请在请求或配置中指定 adb.path 与 adb.address")
        device = devices[0]
        adb_path = adb_path or str(device.adb_path)
        address = address or device.address
        screencap, input_ = device.screencap_methods, device.input_methods
        config = {**device.config, **config}

    kwargs: dict[str, Any] = {"config": config}
    if screencap is not None:
        kwargs["screencap_methods"] = screencap
        kwargs["input_methods"] = input_
    return AdbController(adb_path, address, **kwargs)


def _create_win32(ctrl: dict[str, Any], win32: dict[str, Any]) -> Controller:
    conf = ctrl.get("win32") or {}
    class_regex = win32.get("class_regex") or conf.get("class_regex") or ".*"
    window_regex = win32.get("window_regex") or conf.get("window_regex") or ".*"

    hwnd = win32.get("hwnd")
    if hwnd is None:
        windows = [
            w
            for w in Toolkit.find_desktop_windows()
            if re.search(class_regex, w.class_name) and re.search(window_regex, w.window_name)
        ]
        if not windows:
            raise ControllerError(f"未找到匹配的窗口 class_regex={class_regex!r} window_regex={window_regex!r}")
        hwnd = windows[0].hwnd

    return Win32Controller(
        hwnd,
        screencap_method=_win32_enum(MaaWin32ScreencapMethodEnum, conf.get("screencap"), MaaWin32ScreencapMethodEnum.Background),
        mouse_method=_win32_enum(MaaWin32InputMethodEnum, conf.get("mouse"), MaaWin32InputMethodEnum.Seize),
        keyboard_method=_win32_enum(MaaWin32InputMethodEnum, conf.get("keyboard"), MaaWin32InputMethodEnum.Seize),
    )


def create_controller(ctrl: dict[str, Any], params: dict[str, Any]) -> Controller:
    """ctrl 为 interface.json 中的控制器对象；params 为配置文件与请求参数合并后的结果。"""
    ctype = params.get("controller_type") or ctrl.get("type")
    if ctype == "Adb":
        controller = _create_adb(ctrl, params.get("adb") or {})
    elif ctype == "Win32":
        controller = _create_win32(ctrl, params.get("win32") or {})
    elif ctype == "Debug":
        debug = params.get("debug") or {}
        image = debug.get("image")
        controller = DebugController(str(Path(image)) if image else None)
    else:
        raise ControllerError(f"Web 服务暂不支持 {ctype} 类型控制器")

    if ctrl.get("display_raw"):
        controller.set_screenshot_use_raw_size(True)
    elif ctrl.get("display_long_side"):
        controller.set_screenshot_target_long_side(int(ctrl["display_long_side"]))
    elif ctrl.get("display_short_side"):
        controller.set_screenshot_target_short_side(int(ctrl["display_short_side"]))
    return controller


def list_devices(kind: str) -> list[dict[str, Any]]:
    if kind == "adb":
        return [
            {"name": d.name, "adb_path": str(d.adb_path), "address": d.address}
            for d in Toolkit.find_adb_devices()
        ]
    if kind == "win32":
        return [
            {"hwnd": getattr(d.hwnd, "value", d.hwnd), "class_name": d.class_name, "window_name": d.window_name}
            for d in Toolkit.find_desktop_windows()
        ]
    raise ControllerError(f"未知的设备类型: {kind}，可选 adb / win32")
