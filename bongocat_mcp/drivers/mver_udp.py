"""BongoCatMver 系成品（C++/SFML，如各「皮肤修改版」）的 driver。

协议为实证逆向（对 v0.1.6 二进制抓包 + 对真实接收端做原始帧实验，2026-08-17）：
  - 每帧 312 字节全量状态，60fps 连发（16.5ms），无握手
  - bytes[0..255]  按 Windows VK 码索引的按键状态：
        0x81 = 按下（**整个按住期间持续发送**，只发数帧接收端只闪一帧动画）
        0x80 = 松开边缘帧（1-2 帧后归 0x00）
        0x00 = 空闲；0x01 为真实 sender 松开后的余晖态，接收端不需要
        VK 0x01/0x02 即鼠标左右键
  - bytes[256..311] 14 个 float：fl[8]=0.8*光标x/屏宽，fl[9]=0.8*光标y/屏高
  - 恒定槽位 0x90/0xF0/0xF3/0xF6/0xFB = 0x01（真实 sender 常亮，照抄）
  - 组合键绑定（如 [18,219]=Alt+[）有时序要求：修饰键先按住 ≥0.3s
    再按触发键，同时按下不触发热键

表情/动作：读取 Mver 皮肤目录 config.json 中 键位→l2d_expression/l2d_motion 的绑定，
通过「按下绑定键」间接触发。

前置条件：Mver 需在设置中开启 网络同步 并处于 接收(is_sender=false) 模式。
"""

from __future__ import annotations

import json
import os
import re
import socket
import struct
import threading
import time
from pathlib import Path

from bongocat_mcp import config
from bongocat_mcp.drivers import win32_utils as w32
from .base import (
    CMD_HIDE_BUBBLE,
    CMD_PING,
    CMD_PLAY_MOTION,
    CMD_PRESS_KEY,
    CMD_RELEASE_KEY,
    CMD_SET_EXPRESSION,
    CMD_SET_WINDOW_VISIBLE,
    CMD_SHOW_BUBBLE,
    CMD_STATUS,
    CMD_TYPE_TEXT,
    CatDriver,
    DriverError,
)

FRAME_LEN = 312
FRAME_INTERVAL = 1 / 60
FLOATS_OFFSET = 256
MOUSE_X_FLOAT = 8
MOUSE_Y_FLOAT = 9
ALWAYS_ON_SLOTS = (0x90, 0xF0, 0xF3, 0xF6, 0xFB)

# 按键状态编码（详见模块 docstring 的协议笔记）
ST_PRESSED_FRAME = 0x81  # 按下（持续整个按住时长）
ST_HELD = 0x01  # 真实 sender 松开后的余晖态；driver 不使用，仅记录协议
ST_RELEASED_FRAME = 0x80  # 松开边缘帧
ST_IDLE = 0x00
TRANSIENT_FRAMES = 2  # 松开边缘帧持续帧数

DEFAULT_HOST = "127.0.0.1"
DEFAULT_RECEIVE_PORT = 50001

MVER_WINDOW_TITLE = "Bongo Cat Mver"

# mode 数字含义来自 BongoCatMverUI 源码 setting_cat.xaml.cs：
# 标准模式=1、键盘模式=2、手柄模式=3（0 无效；98 是标准模式设置页的暂存值）
_MODE_NAMES = {1: "standard", 2: "keyboard", 3: "gamepad"}


def _key_name_to_vk(key: str) -> int | None:
    """'KeyA' / 'Num1' / 'Space' / 单字符 / 十进制 VK 都接受。"""
    if not key:
        return None
    if key.startswith("Key") and len(key) == 4 and key[3].isalpha():
        return ord(key[3].upper())
    if key.startswith("Num") and key[3:].isdigit():
        return 0x30 + int(key[3:])
    if key == "Space":
        return 0x20
    if len(key) == 1:
        if key.isalpha():
            return ord(key.upper())
        if key.isdigit():
            return ord(key)
        return ord(key) if key.isprintable() else None
    if key.isdigit():
        return int(key)
    return None


def _char_to_vk(char: str) -> int | None:
    return _key_name_to_vk(char)


class MverUdpDriver(CatDriver):
    name = "mver-udp"

    def __init__(self) -> None:
        self.host = config.get("host") or DEFAULT_HOST
        self.mver_dir = self._resolve_mver_dir()
        self._config_cache: dict | None = None
        self.port = self._resolve_port()

        self._lock = threading.Lock()
        self._raw_config: str | None = None
        # AI 覆盖层：vk -> 逻辑按下。发送循环每帧以「覆盖 > 真实键鼠镜像」合成状态，
        # 平时猫完全跟随真实键鼠（接收模式下本机输入被应用忽略，由本层代为转发）。
        self._override: dict[int, bool] = {}
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._thread = threading.Thread(target=self._send_loop, daemon=True)
        self._thread.start()

    # ---- 定位 Mver 安装目录 ----

    @staticmethod
    def _resolve_mver_dir() -> Path | None:
        env = config.get("mver_dir")
        if env:
            return Path(env)

        found = w32.find_process(name_contains="mver")
        if found:
            exe = w32.process_exe_path(found[0])
            if exe:
                return Path(exe).parent
        return None

    # ---- 接收端口解析 ----

    def _resolve_port(self) -> int:
        """优先级：环境变量 > 皮肤 config.json 的 network.receive_port > 默认。

        UDP 发送是发完即忘，端口不匹配不会产生任何错误反馈，
        因此默认值必须来自猫自己的配置，而不是拍脑袋的常量。
        """
        env = config.get("mver_port")
        if env and str(env).isdigit():
            return int(env)
        try:
            port = int(self._load_skin_config().get("network", {}).get("receive_port", 0))
            if 0 < port < 65536:
                return port
        except (DriverError, ValueError, TypeError):
            pass
        return DEFAULT_RECEIVE_PORT

    # ---- 皮肤 config.json（带 // 注释的 JSON）----

    def _load_skin_config(self, *, refresh: bool = False) -> dict:
        if self._config_cache is not None and not refresh:
            return self._config_cache

        if not self.mver_dir:
            raise DriverError(
                "未定位到 Mver 安装目录，无法解析表情/动作绑定。"
                "请设置 BONGOCAT_MVER_DIR 指向皮肤目录（含 config.json）。"
            )

        path = self.mver_dir / "config.json"
        if not path.exists():
            raise DriverError(f"找不到皮肤配置 {path}")

        raw = path.read_text(encoding="utf-8", errors="replace")
        self._raw_config = raw
        clean = re.sub(r"^\s*//.*$", "", raw, flags=re.M)
        clean = re.sub(r"([,\[\]\d\"'\}])\s*//.*$", r"\1", clean, flags=re.M)

        try:
            self._config_cache = json.loads(clean)
        except json.JSONDecodeError as exc:
            raise DriverError(f"解析皮肤配置失败（{path}）: {exc}") from exc

        return self._config_cache

    def _annotated_names(self, key: str) -> list[str]:
        """从皮肤作者写的行尾注释提取名字，如 `[18,219]//正常眼` -> "正常眼"。

        仅对当前 mode 小节内、指定键（l2d_expression / l2d_motion 等）的数组有效，
        按出现顺序返回；没注释的条目返回空串。
        """
        if not self.mver_dir:
            return []

        raw = getattr(self, "_raw_config", None)
        if raw is None:
            try:
                raw = (self.mver_dir / "config.json").read_text(encoding="utf-8", errors="replace")
            except OSError:
                return []

        sec_start = raw.find(f'"{self._mode_name()}"')
        key_at = raw.find(f'"{key}"', sec_start + 1 if sec_start >= 0 else 0)
        if key_at < 0:
            return []

        # 从 key 后第一个 '[' 起做括号配平，截取数组原文
        depth, i, arr_start = 0, key_at, -1
        for i in range(key_at, len(raw)):
            ch = raw[i]
            if ch == "[":
                if arr_start < 0:
                    arr_start = i
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0 and arr_start >= 0:
                    break
        if arr_start < 0:
            return []

        body = raw[arr_start:i + 1]
        # 数组内逐项（按顶层逗号分割），取行尾注释
        names: list[str] = []
        item, item_depth = "", 0
        items: list[str] = []
        for ch in body[1:-1]:
            if ch == "[":
                item_depth += 1
            elif ch == "]":
                item_depth -= 1
            if ch == "," and item_depth == 0:
                items.append(item)
                item = ""
            else:
                item += ch
        items.append(item)

        for it in items:
            m = re.search(r"//\s*([^\r\n,]+)", it)
            names.append(m.group(1).strip() if m else "")

        return names

    def _mode_name(self) -> str:
        cfg = self._load_skin_config()
        return _MODE_NAMES.get(cfg.get("mode", 1), "standard")

    def _mode_section(self) -> dict:
        cfg = self._load_skin_config()
        return cfg.get(self._mode_name(), {}) or {}

    # ---- 发送循环：真实键鼠镜像 + AI 覆盖层 ----

    def _send_loop(self) -> None:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        # 物理像素坐标（与抓包观测的归一化基准一致）
        try:
            user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
        get_async = user32.GetAsyncKeyState
        get_async.restype = ctypes.c_short
        get_cursor = user32.GetCursorPos
        metrics = user32.GetSystemMetrics
        pt = wt.POINT()

        prev_down: dict[int, bool] = {}   # 上一帧逻辑按下状态
        release_left: dict[int, int] = {}  # 松开边缘帧剩余数
        next_tick = time.perf_counter()

        while not self._stop.is_set():
            # 镜像层：读取真实键鼠状态
            real_down = [bool(get_async(vk) & 0x8000) for vk in range(256)]
            get_cursor(ctypes.byref(pt))
            sw, sh = metrics(0), metrics(1)

            with self._lock:
                overrides = dict(self._override)

            keys = bytearray(256)
            for vk in range(256):
                down = overrides.get(vk, real_down[vk])
                if down:
                    keys[vk] = ST_PRESSED_FRAME
                    prev_down[vk] = True
                    release_left.pop(vk, None)
                elif prev_down.get(vk):
                    keys[vk] = ST_RELEASED_FRAME
                    release_left[vk] = TRANSIENT_FRAMES
                    prev_down[vk] = False
                elif release_left.get(vk, 0) > 0:
                    keys[vk] = ST_RELEASED_FRAME
                    release_left[vk] -= 1
                else:
                    keys[vk] = ST_IDLE

            for slot in ALWAYS_ON_SLOTS:
                keys[slot] = 0x01

            floats = bytearray(struct.pack("<14f", *([0.0] * 14)))
            if sw > 0 and sh > 0:
                struct.pack_into("<f", floats, MOUSE_X_FLOAT * 4, 0.8 * pt.x / sw)
                struct.pack_into("<f", floats, MOUSE_Y_FLOAT * 4, 0.8 * pt.y / sh)

            try:
                self._sock.sendto(bytes(keys) + bytes(floats), (self.host, self.port))
            except OSError:
                pass

            next_tick += FRAME_INTERVAL
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.perf_counter()  # 落后过多则重新对齐

    def _set_key(self, vk: int, pressed: bool) -> None:
        """AI 覆盖层：按下/松开由发送循环与真实键鼠镜像合成（覆盖优先）。"""
        with self._lock:
            self._override[vk] = pressed

    def _hold_key(self, vk: int, seconds: float) -> None:
        self._hold_chord([vk], seconds)

    def _hold_chord(self, vks: list[int], seconds: float) -> None:
        """按下整组键（支持 Alt+X 这类组合键绑定）。

        时序要求（实测）：修饰键先按住 ≥0.3s，再按最后一个触发键，
        同时按下不会触发表情/动作热键。
        """
        modifiers, trigger = vks[:-1], vks[-1]

        for vk in modifiers:
            self._set_key(vk, True)
        if modifiers:
            time.sleep(0.35)

        self._set_key(trigger, True)
        time.sleep(seconds)
        self._set_key(trigger, False)

        time.sleep(0.05)
        for vk in reversed(modifiers):
            self._set_key(vk, False)

    def _tap_chord(self, vks: list[int]) -> None:
        """轻触式触发热键：满足组合键时序（修饰键 ≥0.3s）的前提下，
        触发键只保持数帧——猫爪几乎不做可视动作，表情直接切换。

        协议没有直接设表情的通道，这是视觉干扰最小的触发方式；
        触发帧时长经实测校准（过短热键不触发）。
        """
        modifiers, trigger = vks[:-1], vks[-1]

        for vk in modifiers:
            self._set_key(vk, True)
        if modifiers:
            time.sleep(0.32)

        self._set_key(trigger, True)
        time.sleep(0.1)
        self._set_key(trigger, False)

        time.sleep(0.04)
        for vk in reversed(modifiers):
            self._set_key(vk, False)

    def _model_supports(self, kind: str) -> bool:
        """当前模式的模型是否真的具备表情/动作素材。

        config 里的 l2d_expression / l2d_motion 绑定可能是皮肤作者从别处抄来的
        无效遗留（实测 A-千咲：标准模式 model3.json 无 Expressions 段、模型目录
        无 exp3.json，但 config 注释却写着「正常眼/哭泣眼」）。以模型目录的
        实际素材为准，避免 advertise 一个不可能生效的能力。
        """
        if not self.mver_dir:
            return False
        model_dir = self.mver_dir / "img" / self._mode_name() / "cat_model"
        if not model_dir.is_dir():
            return False

        suffix = "*.exp3.json" if kind == "expressions" else "*.motion3.json"
        if any(model_dir.glob(suffix)):
            return True

        for model_json in model_dir.glob("*.model3.json"):
            try:
                refs = json.loads(
                    model_json.read_text(encoding="utf-8-sig"),
                ).get("FileReferences", {})
            except (OSError, ValueError):
                continue
            if kind == "expressions" and refs.get("Expressions"):
                return True
            if kind == "motions" and refs.get("Motions"):
                return True

        return False

    # ---- 猫存活校验 ----

    @staticmethod
    def _cat_process_alive() -> bool:
        """UDP 发送是即发即忘，接收端不存在也不会有任何错误反馈；
        唯一可靠的存活信号是猫进程本身（排除 UI/转换器等伴随进程）。"""
        return w32.find_process(
            name_contains="mver", name_excludes=("ui", "converter"),
        ) is not None

    # ---- CatDriver ----

    def capabilities(self) -> set[str]:
        caps = {
            CMD_PING, CMD_STATUS, CMD_PRESS_KEY, CMD_RELEASE_KEY, CMD_TYPE_TEXT,
            CMD_SHOW_BUBBLE, CMD_HIDE_BUBBLE, CMD_SET_WINDOW_VISIBLE,
        }
        if self._model_supports("expressions"):
            caps.add(CMD_SET_EXPRESSION)
        if self._model_supports("motions"):
            caps.add(CMD_PLAY_MOTION)
        return caps

    def call(self, cmd: str, payload: dict) -> dict:
        if cmd == CMD_PING:
            alive = self._thread.is_alive() and self._cat_process_alive()
            note = ""
            if not self._cat_process_alive():
                note = "注意：Bongo Cat Mver 进程未运行，发出的 UDP 帧没有接收端。"
            else:
                if self.mver_dir:
                    try:
                        cfg = self._load_skin_config()
                        net = cfg.get("network", {})
                        if not net.get("network"):
                            note = "注意：皮肤 config.json 中 network 未开启，接收端不会响应。"
                        elif net.get("is_sender"):
                            note = "注意：皮肤 config.json 中 is_sender=true（发送模式），需改为接收模式。"
                    except DriverError as exc:
                        note = f"注意：{exc}"
            return {"ok": alive, "target": f"{self.host}:{self.port}", "note": note}

        if cmd == CMD_STATUS:
            # 诚实降级：猫进程没在跑时如实报离线（status 读取的皮肤配置是静态文件，
            # 不能作为"在线"证据），并给出恢复指引
            if not self._cat_process_alive():
                return {
                    "ok": False,
                    "error": "Bongo Cat Mver 进程未运行。"
                             "请启动猫（或用仪表盘「一键接入 Mver 新猫」自动提权拉起）。",
                }

            section = self._mode_section()
            supports_expr = self._model_supports("expressions")
            supports_motion = self._model_supports("motions")

            expressions = []
            if supports_expr:
                expr_names = self._annotated_names("l2d_expression")
                expressions = [
                    {
                        "index": i,
                        "name": expr_names[i] if i < len(expr_names) and expr_names[i] else f"expression-{i}",
                        "keys": keys,
                    }
                    for i, keys in enumerate(section.get("l2d_expression", []) or [])
                ]

            motions = []
            if supports_motion:
                motion_names = self._annotated_names("l2d_motion_lockhand") \
                    + self._annotated_names("l2d_motion")
                motions = [
                    {
                        "index": i,
                        "name": motion_names[i] if i < len(motion_names) and motion_names[i] else f"motion-{i}",
                        "keys": keys,
                    }
                    for i, keys in enumerate(
                        (section.get("l2d_motion_lockhand", []) or [])
                        + (section.get("l2d_motion", []) or []),
                    )
                ]

            notes = []
            if not supports_expr:
                notes.append("模型无表情素材文件，此皮肤不支持表情切换")
            if not supports_motion:
                notes.append("模型无动作素材文件，此皮肤不支持动作播放")

            win = w32.find_window(title_contains=MVER_WINDOW_TITLE)
            return {
                "ok": True,
                "data": {
                    "modelInfo": {
                        "modelId": self.mver_dir.name if self.mver_dir else "mver",
                        "mode": self._mode_name(),
                        "motions": motions,
                        "expressions": expressions,
                    },
                    "assets": {"expressions": supports_expr, "motions": supports_motion},
                    "note": "；".join(notes) if notes else "",
                    "windowVisible": bool(win and w32.window_exists(win.hwnd)),
                },
            }

        if cmd == CMD_SET_EXPRESSION:
            if not self._model_supports("expressions"):
                return {
                    "ok": False,
                    "supported": False,
                    "error": "此皮肤当前模式的模型没有表情素材（config 中的 l2d_expression 绑定为无效遗留），表情切换不可用",
                }
            index = int(payload["index"])
            section = self._mode_section()
            groups = section.get("l2d_expression", []) or []
            if not 0 <= index < len(groups):
                return {"ok": False, "error": f"表情索引 {index} 超出范围（共 {len(groups)} 组）"}
            # 轻触式触发：猫爪几乎不敲键盘，表情直接切换（见 _tap_chord）
            self._tap_chord([int(k) for k in groups[index]])
            return {"ok": True}

        if cmd == CMD_PLAY_MOTION:
            if not self._model_supports("motions"):
                return {
                    "ok": False,
                    "supported": False,
                    "error": "此皮肤当前模式的模型没有动作素材（config 中的 l2d_motion 绑定为无效遗留），动作播放不可用",
                }
            # payload 与 status 返回的 motion 对象兼容
            keys = payload.get("motion", {}).get("keys") or payload.get("keys") or []
            if not keys:
                return {"ok": False, "error": "motion 对象缺少 keys 绑定"}
            self._hold_chord([int(k) for k in keys], 1.0)
            return {"ok": True}

        if cmd in (CMD_PRESS_KEY, CMD_RELEASE_KEY):
            vk = _key_name_to_vk(str(payload["key"]))
            if vk is None or not 0 < vk < 256:
                return {"ok": False, "error": f"无法识别按键 {payload['key']!r}"}
            self._set_key(vk, cmd == CMD_PRESS_KEY)
            return {"ok": True}

        if cmd == CMD_TYPE_TEXT:
            for char in payload["text"]:
                vk = _char_to_vk(char)
                if vk is None:
                    continue
                self._hold_key(vk, 0.25)
                time.sleep(0.15)
            return {"ok": True}

        if cmd in (CMD_SHOW_BUBBLE, CMD_HIDE_BUBBLE):
            from bongocat_mcp.bubble.overlay import (
                bubble_overlay, duration_to_auto_hide_ms, ensure_overlay,
            )
            if cmd == CMD_SHOW_BUBBLE:
                # 猫不在线直接跳过：气泡是时效性通知，不排队也不在猫恢复后补发
                # （否则只能在屏幕外空转过期，且调用侧还会拿到假 ok）
                if not w32.find_window(title_contains=MVER_WINDOW_TITLE):
                    return {"ok": False,
                            "error": "猫窗口当前不在线，气泡已跳过（不会在猫恢复后补发）"}
                overlay = ensure_overlay()
                if overlay is None:
                    return {"ok": False, "error": "气泡渲染线程未能启动"}
                overlay.show(
                    payload["text"], title_contains=MVER_WINDOW_TITLE,
                    auto_hide_ms=duration_to_auto_hide_ms(payload.get("duration")),
                )
            else:
                bubble_overlay().hide()
            return {"ok": True}

        if cmd == CMD_SET_WINDOW_VISIBLE:
            # 全量枚举（含已隐藏窗口）：否则 hide 之后 find 不到、无法再 show
            win = w32.find_window(title_contains=MVER_WINDOW_TITLE, visible_only=False)
            if not win:
                return {"ok": False, "error": "未找到 Mver 窗口"}
            w32.set_window_visible(win.hwnd, bool(payload["visible"]))
            return {"ok": True}

        raise DriverError(f"mver driver 不支持 {cmd}")

    def close(self) -> None:
        self._stop.set()
