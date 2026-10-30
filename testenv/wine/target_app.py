"""Win32 测试目标窗口：给 MaaFramework 的 Win32 控制器一个画面固定、可记录输入的窗口。

只依赖 ctypes，在 Wine 和真实 Windows 上都能运行：

    python target_app.py --log events.jsonl

客户区固定为 1280x720，画面元素的位置见 LAYOUT，与 testenv/wine/test_win32.py 中的断言一一对应。
收到的鼠标 / 键盘输入以 JSON Lines 追加到 --log 指定的文件，同时显示在窗口底部。
"""

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes

if sys.platform != "win32":
    sys.exit("target_app.py 需要在 Windows 或 Wine 中运行")

WIDTH, HEIGHT = 1280, 720
TITLE = "MaaSquad Test Target"
CLASS_NAME = "MaaSquadTestTarget"

# 画面布局（客户区坐标 [x, y, w, h]）
LAYOUT = {
    # 与 assets/resource/pipeline/my_task.json 中 FindSmallestNumber 的 candidate_rois 一致
    "number_a": ([100, 100, 200, 100], "42"),
    "number_b": ([300, 100, 200, 100], "17"),
    "red": [100, 300, 150, 150],
    "green": [300, 300, 150, 150],
    "blue": [500, 300, 150, 150],
    # 点击后在灰色 / 黄色之间切换
    "button": [800, 300, 300, 100],
    "label": [800, 100, 400, 100],
}
COLORS = {  # RGB
    "background": (32, 32, 32),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "button": (128, 128, 128),
    "button_active": (255, 255, 0),
    "white": (255, 255, 255),
}

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY, WM_PAINT, WM_CLOSE = 0x0002, 0x000F, 0x0010
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN, WM_MOUSEWHEEL = 0x0200, 0x0201, 0x0202, 0x0204, 0x020A
WS_OVERLAPPEDWINDOW, WS_VISIBLE = 0x00CF0000, 0x10000000
CS_HREDRAW, CS_VREDRAW = 0x0002, 0x0001
DT_CENTER, DT_VCENTER, DT_SINGLELINE, DT_LEFT = 0x01, 0x04, 0x20, 0x00
TRANSPARENT = 1
SW_SHOW = 5
IDC_ARROW = 32512


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


def _sig(fn, restype, *argtypes):
    fn.restype, fn.argtypes = restype, list(argtypes)


_sig(user32.DefWindowProcW, LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.RegisterClassExW, wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
_sig(
    user32.CreateWindowExW,
    wintypes.HWND,
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
)  # fmt: skip
_sig(user32.AdjustWindowRectEx, wintypes.BOOL, ctypes.POINTER(wintypes.RECT), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_sig(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
_sig(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
_sig(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
_sig(user32.BeginPaint, wintypes.HDC, wintypes.HWND, ctypes.POINTER(PAINTSTRUCT))
_sig(user32.EndPaint, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(PAINTSTRUCT))
_sig(user32.FillRect, ctypes.c_int, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH)
_sig(user32.DrawTextW, ctypes.c_int, wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT)
_sig(user32.InvalidateRect, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT), wintypes.BOOL)
_sig(user32.LoadCursorW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPVOID)
_sig(user32.ShowWindow, wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_sig(user32.UpdateWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.PostQuitMessage, None, ctypes.c_int)
_sig(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
_sig(gdi32.CreateSolidBrush, wintypes.HBRUSH, wintypes.COLORREF)
_sig(gdi32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
_sig(gdi32.SelectObject, wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
_sig(gdi32.SetTextColor, wintypes.COLORREF, wintypes.HDC, wintypes.COLORREF)
_sig(gdi32.SetBkMode, ctypes.c_int, wintypes.HDC, ctypes.c_int)
_sig(
    gdi32.CreateFontW,
    wintypes.HFONT,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
)  # fmt: skip
_sig(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)


def rgb(color: tuple[int, int, int]) -> int:
    r, g, b = color
    return r | (g << 8) | (b << 16)


def rect(x: int, y: int, w: int, h: int) -> wintypes.RECT:
    return wintypes.RECT(x, y, x + w, y + h)


def lparam_xy(lparam: int) -> tuple[int, int]:
    x, y = lparam & 0xFFFF, (lparam >> 16) & 0xFFFF
    return (x - 0x10000 if x & 0x8000 else x), (y - 0x10000 if y & 0x8000 else y)


class TargetApp:
    def __init__(self, log_path: str, title: str):
        self.log_file = open(log_path, "a", encoding="utf-8") if log_path else None
        self.title = title
        self.clicks = 0
        self.active = False
        self.typed = ""
        self.last = "waiting"
        self.font_big = gdi32.CreateFontW(64, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 4, 0, "Arial")
        self.font_small = gdi32.CreateFontW(28, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 4, 0, "Arial")
        self._proc = WNDPROC(self._wndproc)  # 保持引用，避免被回收
        self.hwnd = None

    def log(self, event: dict) -> None:
        event["t"] = round(time.time(), 3)
        if self.log_file:
            self.log_file.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.log_file.flush()

    def create(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        wc.lpszClassName = CLASS_NAME
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())

        frame = wintypes.RECT(0, 0, WIDTH, HEIGHT)
        user32.AdjustWindowRectEx(ctypes.byref(frame), WS_OVERLAPPEDWINDOW, False, 0)
        self.hwnd = user32.CreateWindowExW(
            0, CLASS_NAME, self.title, WS_OVERLAPPEDWINDOW | WS_VISIBLE,
            0, 0, frame.right - frame.left, frame.bottom - frame.top,
            None, None, hinst, None,
        )  # fmt: skip
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        user32.ShowWindow(self.hwnd, SW_SHOW)
        user32.UpdateWindow(self.hwnd)
        user32.SetForegroundWindow(self.hwnd)
        self.log({"type": "ready", "hwnd": self.hwnd, "width": WIDTH, "height": HEIGHT})

    def run(self) -> int:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        return 0

    # ------------------------------------------------------------ 绘制

    def _fill(self, hdc, box, color) -> None:
        brush = gdi32.CreateSolidBrush(rgb(color))
        r = rect(*box)
        user32.FillRect(hdc, ctypes.byref(r), brush)
        gdi32.DeleteObject(brush)

    def _text(self, hdc, box, text, font, color=(0, 0, 0), flags=DT_CENTER | DT_VCENTER | DT_SINGLELINE) -> None:
        gdi32.SelectObject(hdc, font)
        gdi32.SetTextColor(hdc, rgb(color))
        r = rect(*box)
        user32.DrawTextW(hdc, text, -1, ctypes.byref(r), flags)

    def paint(self, hdc) -> None:
        gdi32.SetBkMode(hdc, TRANSPARENT)
        self._fill(hdc, [0, 0, WIDTH, HEIGHT], COLORS["background"])
        self._text(hdc, [20, 10, 1240, 70], TITLE, self.font_small, COLORS["white"], DT_LEFT | DT_VCENTER | DT_SINGLELINE)
        for key in ("number_a", "number_b"):
            box, text = LAYOUT[key]
            inner = [box[0] + 10, box[1] + 10, box[2] - 20, box[3] - 20]
            self._fill(hdc, inner, COLORS["white"])
            self._text(hdc, inner, text, self.font_big)
        self._fill(hdc, LAYOUT["label"], COLORS["white"])
        self._text(hdc, LAYOUT["label"], "MaaSquad", self.font_big)
        for key in ("red", "green", "blue"):
            self._fill(hdc, LAYOUT[key], COLORS[key])
        self._fill(hdc, LAYOUT["button"], COLORS["button_active" if self.active else "button"])
        self._text(hdc, LAYOUT["button"], f"clicks: {self.clicks}", self.font_small)
        status = f"last: {self.last}    typed: {self.typed[-40:]}"
        self._text(hdc, [20, 640, 1240, 60], status, self.font_small, COLORS["white"], DT_LEFT | DT_VCENTER | DT_SINGLELINE)

    def _redraw(self) -> None:
        user32.InvalidateRect(self.hwnd, None, False)

    # ------------------------------------------------------------ 消息

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_PAINT:
            ps = PAINTSTRUCT()
            hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
            self.paint(hdc)
            user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg in (WM_LBUTTONDOWN, WM_RBUTTONDOWN):
            x, y = lparam_xy(lparam)
            button = "left" if msg == WM_LBUTTONDOWN else "right"
            bx, by, bw, bh = LAYOUT["button"]
            hit = bx <= x < bx + bw and by <= y < by + bh
            if msg == WM_LBUTTONDOWN:
                self.clicks += 1
                if hit:
                    self.active = not self.active
            self.last = f"{button} down ({x}, {y})"
            self.log({"type": "mouse_down", "button": button, "x": x, "y": y, "button_hit": hit})
            self._redraw()
            return 0
        if msg == WM_LBUTTONUP:
            x, y = lparam_xy(lparam)
            self.log({"type": "mouse_up", "button": "left", "x": x, "y": y})
            return 0
        if msg == WM_MOUSEWHEEL:
            delta = ctypes.c_short((wparam >> 16) & 0xFFFF).value
            self.log({"type": "wheel", "delta": delta})
            return 0
        if msg == WM_KEYDOWN:
            self.last = f"key down {wparam}"
            self.log({"type": "key_down", "vk": wparam})
            self._redraw()
            return 0
        if msg == WM_KEYUP:
            self.log({"type": "key_up", "vk": wparam})
            return 0
        if msg == WM_CHAR:
            ch = chr(wparam)
            if ch.isprintable():
                self.typed += ch
            self.log({"type": "char", "char": ch})
            self._redraw()
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            self.log({"type": "exit"})
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def main() -> int:
    p = argparse.ArgumentParser(description="MaaSquad Win32 测试目标窗口")
    p.add_argument("--log", default="", help="输入事件日志（JSON Lines）")
    p.add_argument("--title", default=TITLE, help="窗口标题")
    args = p.parse_args()

    try:
        user32.SetProcessDPIAware()
    except AttributeError:
        pass
    app = TargetApp(args.log, args.title)
    app.create()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
