"""仅依赖 stdlib/ctypes 的 Win32 小工具：进程扫描、窗口枚举、显示/隐藏。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
from dataclasses import dataclass

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom


def list_processes() -> list[tuple[int, str]]:
    """[(pid, image_name)]，失败返回空表。

    tasklist 输出按系统 ANSI 编码（中文系统为 GBK），统一用 errors="replace"
    解码——进程名匹配均为 ASCII 关键字，替换字符不影响。
    """
    try:
        res = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    out = res.stdout.decode("utf-8", errors="replace")

    procs = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            procs.append((int(parts[1]), parts[0]))
    return procs


def find_process(*, name_contains: str, name_excludes: tuple[str, ...] = ()) -> tuple[int, str] | None:
    low = name_contains.lower()
    for pid, name in list_processes():
        nl = name.lower()
        if low in nl and not any(x in nl for x in name_excludes):
            return pid, name
    return None


def process_exe_path(pid: int) -> str | None:
    """进程 exe 完整路径。

    优先 ctypes 直调（PROCESS_QUERY_LIMITED_INFORMATION 对提权进程也有效，
    Mver 这类管理员运行的猫因此可以被自动定位）；
    失败再退回 PowerShell Get-Process。
    """
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            kernel32.CloseHandle(handle)

    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Id {pid}).Path"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out or None


def kill_process(pid: int, timeout: float = 10.0) -> bool:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=timeout,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def enum_windows(*, visible_only: bool = True) -> list[WindowInfo]:
    """枚举顶层窗口。visible_only=False 时包含隐藏窗口——
    显示命令必须能找到已被隐藏的窗口，否则 hide 后无法再 show。"""

    result: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lparam):
        if visible_only and not user32.IsWindowVisible(hwnd):
            return True

        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)

        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))

        result.append(WindowInfo(hwnd, pid.value, buf.value,
                                 (rect.left, rect.top, rect.right, rect.bottom)))
        return True

    user32.EnumWindows(cb, 0)
    return result


def find_window(
    *,
    pid: int | None = None,
    title_contains: str | None = None,
    visible_only: bool = True,
) -> WindowInfo | None:
    """按 pid 或标题（不区分大小写包含）找顶层窗口，优先标题匹配。"""
    wins = enum_windows(visible_only=visible_only)
    if title_contains:
        low = title_contains.lower()
        for w in wins:
            if low in w.title.lower():
                return w
    if pid is not None:
        cands = [w for w in wins if w.pid == pid and w.title]
        if cands:
            # 取面积最大的（主窗口通常最大；设置窗口更小）
            return max(cands, key=lambda w: (w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1]))
    return None


def root_window(hwnd: int) -> int:
    """取窗口所属的顶层窗口 HWND（GA_ROOT）。

    tkinter 的 winfo_id/frame 返回的是 wrapper 内部的 Tk 子窗口，
    扩展样式必须设到顶层 wrapper 上才生效（点击穿透等）。
    """
    user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
    user32.GetAncestor.restype = wt.HWND
    top = user32.GetAncestor(hwnd, 2)  # GA_ROOT
    return int(top) if top else hwnd


def window_exists(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd))


def get_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def set_window_visible(hwnd: int, visible: bool) -> bool:
    return bool(user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE if visible else SW_HIDE))
