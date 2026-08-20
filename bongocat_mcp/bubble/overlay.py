"""bridge 自绘聊天气泡：透明置顶、点击穿透、跟随猫窗口、打字机动画。

供 cdp / mver 等没有应用内气泡 UI 的 driver 使用。
tkinter 跑在独立线程（MCP stdio 占用主线程），命令经 queue 投递。
气泡默认在打字动画结束后 AUTO_HIDE_DEFAULT_MS 毫秒自动消失
（show 传 auto_hide_ms=None 可常驻，等显式 hide()）。
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

from bongocat_mcp.drivers import win32_utils as w32

TRANSPARENT = "#010203"
BG = "#fffdf7"
BORDER = "#4a3728"
TEXT_COLOR = "#4a3728"

TRACK_INTERVAL_MS = 30
TYPE_INTERVAL_MS = 60
AUTO_HIDE_DEFAULT_MS = 8000
# 置顶重申间隔。-topmost 只在启动时设一次的话，之后任何窗口置顶
# （猫重启、全屏游戏、别的 overlay 进程）都会把本窗口永久压到 z 序下层：
# 窗口一切状态正常但不再被 DWM 合成，气泡从此看不见。周期重申可自愈。
TOPMOST_REASSERT_S = 1.0

# Win32 扩展样式：置顶分层 + 点击穿透 + 不抢焦点
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000


class _OverlayThread:
    def __init__(self) -> None:
        self.q: queue.Queue[tuple[str, object]] = queue.Queue()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(10)

    # ---- 主线程 API ----

    def show(
        self, text: str, *, pid: int | None = None, title_contains: str | None = None,
        auto_hide_ms: int | None = AUTO_HIDE_DEFAULT_MS,
    ) -> None:
        self.q.put(("show", (text, pid, title_contains, auto_hide_ms)))

    def hide(self) -> None:
        self.q.put(("hide", None))

    # ---- tk 线程 ----

    def _run(self) -> None:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001 旧系统无 shcore
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:  # noqa: BLE001
                pass

        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", TRANSPARENT)
        root.config(bg=TRANSPARENT)
        # 隐藏 = 移到屏幕外（不再 withdraw/deiconify：deiconify 会重置扩展样式，
        # 与点击穿透/分层属性产生拉锯，导致气泡显示不出或关不掉）
        root.geometry("1x1+-32000+-32000")

        canvas = tk.Canvas(
            root, highlightthickness=0, bg=TRANSPARENT,
            bd=0, width=10, height=10,
        )
        canvas.pack(fill="both", expand=True)

        font = tkfont.Font(family="Microsoft YaHei UI", size=13, weight="bold")

        self._root = root
        self._canvas = canvas
        self._font = font
        self._text = ""
        self._shown = 0
        self._target_pid: int | None = None
        self._target_title: str | None = None
        self._auto_hide_ms: int | None = AUTO_HIDE_DEFAULT_MS
        self._typed_at: float | None = None  # 打字动画完成的时刻（monotonic）
        self._visible = False
        self._topmost_at = 0.0  # 上次重申置顶的时刻（0 = 下一帧立即重申）
        self._make_click_through()
        self.ready.set()

        root.after(TRACK_INTERVAL_MS, self._track)
        root.after(TYPE_INTERVAL_MS, self._tick_type)
        root.after(30, self._drain_queue)
        root.mainloop()

    def _winfo_int(self) -> int:
        """winfo_id 的整数值：tk 返回 '0x..' 十六进制或纯十进制字符串（版本相关）。"""
        raw = str(self._root.winfo_id())
        return int(raw, 16) if raw.lower().startswith("0x") else int(raw)

    def _toplevel_hwnd(self) -> int:
        """tk 顶层 wrapper 的真实 HWND（winfo id 给的是 wrapper 内的子窗口）。"""
        return w32.root_window(self._winfo_int())

    def _make_click_through(self) -> None:
        """启动时设置完整扩展样式；此后由 _assert_window_effects 周期重申维持。"""
        self._assert_window_effects()

    def _assert_window_effects(self) -> None:
        """重申完整扩展样式 + 颜色键分层（均幂等）。

        必须周期重申而非只设一次：tk 重配 wrapper 时会整体重写 exstyle
        （只保留它自己认识的位），外部追加的点击穿透/不抢焦点位会被抹掉；
        颜色键属性同理偶发被重置。
        """
        hwnd = self._toplevel_hwnd()
        style = w32.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        want = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        if (style & want) != want:
            w32.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | want)
            style = w32.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not style & WS_EX_LAYERED:
            return  # 样式位设置失败（不应发生），单设属性无效
        COLORREF = 0x030201  # BGR(#010203)
        w32.user32.SetLayeredWindowAttributes(hwnd, COLORREF, 0, 1)

    def _drain_queue(self) -> None:
        try:
            while True:
                cmd, arg = self.q.get_nowait()
                if cmd == "show":
                    text, pid, title, auto_hide_ms = arg  # type: ignore[misc]
                    self._text = text
                    self._shown = 0
                    self._target_pid = pid
                    self._target_title = title
                    self._auto_hide_ms = auto_hide_ms
                    self._typed_at = None
                    self._visible = True
                    self._hidden = False
                    self._topmost_at = 0.0  # 新气泡下一帧立即重申置顶
                elif cmd == "hide":
                    self._visible = False
                    self._hidden = True
        except queue.Empty:
            pass
        self._root.after(30, self._drain_queue)

    def _tick_type(self) -> None:
        if self._visible and self._shown < len(self._text):
            self._shown += 1
        self._root.after(TYPE_INTERVAL_MS, self._tick_type)

    def _measure(self, text: str) -> tuple[int, int]:
        max_w = 0
        lines = text.split("\n")
        for line in lines:
            w = self._font.measure(line)
            if w > max_w:
                max_w = w
        return max_w, self._font.metrics("linespace") * len(lines)

    def _track(self) -> None:
        # 注意：本函数的续期只允许发生在 finally 里。try 体内的提前 return
        # 若也 root.after 一次，finally 还会再调度一次，_track 定时器将按
        # 2 的幂自我复制（30ms 一翻倍），几秒内事件队列被淹没、气泡再也画不出来。
        try:
            if not self._visible or getattr(self, "_hidden", False):
                # 隐藏：钉在屏幕外（1x1，不触发样式重置）
                if getattr(self, "_hidden", False):
                    self._root.geometry("1x1+-32000+-32000")
                    self._hidden = False
                return

            # 置顶与窗口样式自愈（见 TOPMOST_REASSERT_S）：被猫重启/全屏游戏/
            # 其他置顶窗口压下去后，下一秒把自己重新插回 topmost 组顶部；
            # tk 重配 wrapper 会整体重写 exstyle，扩展样式同样需要重申
            if time.monotonic() - self._topmost_at >= TOPMOST_REASSERT_S:
                self._root.attributes("-topmost", True)
                self._assert_window_effects()
                self._topmost_at = time.monotonic()

            # 自动隐藏：打字动画全部显示完后再停留 auto_hide_ms（None=常驻等显式 hide）
            if self._auto_hide_ms is not None:
                if self._shown < len(self._text):
                    self._typed_at = None  # 还在打字，计时起点随动画走
                else:
                    now = time.monotonic()
                    if self._typed_at is None:
                        self._typed_at = now
                    if (now - self._typed_at) * 1000 >= self._auto_hide_ms:
                        self._visible = False
                        self._root.geometry("1x1+-32000+-32000")
                        return

            win = w32.find_window(pid=self._target_pid, title_contains=self._target_title)
            if not win or not w32.window_exists(win.hwnd):
                self._root.geometry("1x1+-32000+-32000")
                return

            shown_text = self._text[: self._shown]
            text_w, text_h = self._measure(shown_text or " ")

            pad_x, pad_y = 16, 10
            width = max(text_w + pad_x * 2 + 8, 60)
            height = text_h + pad_y * 2 + 8

            # 简单折行：过宽时按字符截断换行重新测量
            if width > 360:
                wrapped, line = [], ""
                for ch in shown_text:
                    if self._font.measure(line + ch) > 320 or ch == "\n":
                        wrapped.append(line)
                        line = "" if ch == "\n" else ch
                    else:
                        line += ch
                wrapped.append(line)
                shown_text = "\n".join(wrapped)
                text_w, text_h = self._measure(shown_text)
                width = max(text_w + pad_x * 2 + 8, 60)
                height = text_h + pad_y * 2 + 8

            left, top, right, bottom = win.rect
            x = (left + right) // 2 - width // 2
            y = top - height - 6
            if y < 0:  # 猫窗口贴屏幕顶部时放窗口内部
                y = top + 6

            self._root.geometry(f"{width}x{height}+{x}+{y}")

            c = self._canvas
            c.config(width=width, height=height)
            c.delete("all")

            r = min(14, width // 4, height // 4)
            # 圆角气泡主体
            c.create_polygon(
                r, 2, width - r, 2, width - 2, r, width - 2, height - r,
                width - r, height - 2, r, height - 2, 2, height - r, 2, r,
                fill=BG, outline=BORDER, width=2, smooth=True,
            )
            # 气泡尾巴
            cx = width // 2
            c.create_polygon(
                cx - 8, height - 4, cx + 8, height - 4, cx, height + 6,
                fill=BG, outline=BORDER, width=2,
            )
            c.create_polygon(
                cx - 6, height - 3, cx + 6, height - 3, cx, height + 4,
                fill=BG, outline=BG,
            )
            c.create_text(
                pad_x + 2, height / 2, anchor="w", justify="left",
                text=shown_text, fill=TEXT_COLOR, font=self._font,
            )
        except Exception:  # noqa: BLE001  绘制异常不能杀死 tk 循环
            pass
        finally:
            self._root.after(TRACK_INTERVAL_MS, self._track)


_instance: _OverlayThread | None = None
_instance_lock = threading.Lock()


def duration_to_auto_hide_ms(duration: object) -> int | None:
    """driver payload 的 duration（秒）-> auto_hide_ms；<=0 表示常驻（None）。"""
    try:
        seconds = float(duration)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return AUTO_HIDE_DEFAULT_MS
    return None if seconds <= 0 else int(seconds * 1000)


def bubble_overlay() -> _OverlayThread:
    global _instance
    with _instance_lock:
        if _instance is None or not _instance.thread.is_alive():
            _instance = _OverlayThread()
        return _instance


def ensure_overlay() -> _OverlayThread | None:
    """取渲染线程就绪的 overlay 单例；tk 起不来（构造内等 10s 仍未 ready）返回 None。

    调用方（driver 的 show-bubble）拿到 None 应如实返回 ok=False，
    而不是往死队列里塞消息后假报成功。
    """
    ov = bubble_overlay()
    return ov if ov.ready.is_set() else None
