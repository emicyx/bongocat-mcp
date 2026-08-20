"""Tauri 系成品 BongoCat 的 driver（WebView2 + CDP 注入）。

适用于官方 release 与「只换模型资源的皮肤重打包版」——它们的前端代码与原版一致，
主窗口原生监听 start-motion / set-expression / device-changed 事件，
而偏好窗口本就用 JS `emit()`（即 `__TAURI_INTERNALS__.invoke('plugin:event|emit')`）触发这些事件。

注入链路：本地 CDP(WebSocket) → Runtime.evaluate → emit 原生 Tauri 事件 → 猫。

需要成品以调试端口启动（WebView2 环境变量，无法附加已在运行的实例），
driver 会自动接管：发现猫在跑但无法附加时，结束进程并以环境变量重新拉起。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import TimeoutError as FuturesTimeoutError
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

DEFAULT_CDP_PORT = 9223  # 避开开发者常用的 9222

# 原版前端 useDevice 的按键归一化会处理 F1→Fn、ShiftLeft→Shift 等映射，
# 这里直接发 rdev 风格键名即可复用同一条管线。


def _char_to_key(char: str) -> str | None:
    if char.isalpha() and char.isascii():
        return f"Key{char.upper()}"
    if char.isdigit():
        return f"Num{char}"
    if char == " ":
        return "Space"
    return None


class _CDPSession:
    """后台事件循环线程 + 持久 WebSocket 的 CDP 会话。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self._ws = None
        self._ws_url: str | None = None
        self._msgid = 0

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    # ---- HTTP 探测 ----

    def http_get(self, path: str, timeout: float = 3) -> dict | list | None:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=timeout,
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError):
            return None

    def devtools_ready(self) -> bool:
        return self.http_get("/json/version") is not None

    # ---- 目标选择与连接 ----

    def _pick_main_target(self) -> str | None:
        """主窗口 target：应用页面（排除 devtools/blank），优先主路由/canvas。

        应用刚启动时页面可能还在加载（URL 未定型、canvas 未挂载），重试等待。
        """
        deadline = time.time() + 12

        while time.time() < deadline:
            targets = self.http_get("/json/list") or []

            app_pages = [
                t for t in targets
                if t.get("type") == "page"
                and t.get("webSocketDebuggerUrl")
                and not t.get("url", "").startswith(("devtools://", "about:", "edge://"))
                and ("index.html" in t.get("url", "")
                     or "localhost:1420" in t.get("url", "")
                     or "tauri" in t.get("url", ""))
            ]

            # 主路由（非 preference）优先
            main_route = [
                t for t in app_pages
                if "preference" not in t["url"]
            ]

            for t in (main_route or app_pages):
                try:
                    has_canvas = self.evaluate(
                        "JSON.stringify(!!document.querySelector('canvas'))",
                        ws_url=t["webSocketDebuggerUrl"],
                        timeout=4,
                    )
                    if has_canvas == "true":
                        return t["webSocketDebuggerUrl"]
                except DriverError:
                    continue

            # canvas 还没渲染出来但主路由页面已存在：直接用它
            if main_route:
                return main_route[0]["webSocketDebuggerUrl"]

            time.sleep(0.5)

        return None

    # ---- evaluate ----

    def evaluate(self, js: str, *, ws_url: str | None = None, timeout: float = 15) -> str | None:
        fut = asyncio.run_coroutine_threadsafe(
            self._evaluate(js, ws_url), self.loop,
        )
        try:
            return fut.result(timeout)
        except FuturesTimeoutError:
            raise DriverError("CDP evaluate 超时") from None
        except DriverError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DriverError(f"CDP evaluate 失败: {exc}") from exc

    async def _evaluate(self, js: str, ws_url: str | None) -> str | None:
        import websockets

        target_url = ws_url or self._ws_url

        for attempt in range(2):
            if self._ws is None or target_url != self._ws_url:
                if self._ws is not None:
                    try:
                        await self._ws.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._ws = None

                if target_url is None:
                    target_url = self._pick_main_target()
                if target_url is None:
                    raise DriverError(
                        f"CDP(127.0.0.1:{self.port}) 上没有找到 BongoCat 主窗口 target"
                    )

                self._ws = await websockets.connect(target_url, open_timeout=5)
                self._ws_url = target_url

            self._msgid += 1
            mid = self._msgid

            await self._ws.send(json.dumps({
                "id": mid,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": js,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            }))

            async for raw in self._ws:
                msg = json.loads(raw)
                if msg.get("id") != mid:
                    continue

                result = msg.get("result", {})
                if "exceptionDetails" in result:
                    detail = result["exceptionDetails"]
                    text = detail.get("exception", {}).get("description") \
                        or detail.get("text", "unknown js error")
                    raise DriverError(f"注入 JS 报错: {text}")

                value = result.get("result", {}).get("value")
                return value

            # ws 在等响应时断了：重置后重试一次
            self._ws = None
            if attempt == 0:
                continue
            raise DriverError("CDP WebSocket 在等待响应时断开")

        raise DriverError("CDP evaluate 不可达")

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


def app_path_candidates() -> list[str]:
    """cdp 可启动的 BongoCat exe 候选（env > 运行中的进程 > 仓库构建产物 > 常见安装路径）。

    纯函数无副作用；探测层（detect.py）与 driver 共用，
    冷启动（无猫在跑）时也能凭仓库构建产物走 cdp 自动拉起。
    """
    candidates: list[str] = []

    primary = config.get("app_path")
    if primary and os.path.isfile(primary):
        candidates.append(primary)

    for extra in config.get("app_paths") or []:
        if os.path.isfile(extra):
            candidates.append(extra)

    found = w32.find_process(
        name_contains="bongo",
        # "mcp"：本项目控制器进程（bongocat-mcp.exe）不能被当成猫，接管时会 taskkill
        name_excludes=("mver", "ui", "converter", "mcp"),
    )
    if found:
        exe = w32.process_exe_path(found[0])
        if exe:
            candidates.append(exe)

    localappdata = os.environ.get("LOCALAPPDATA", "")
    if localappdata:
        candidates.append(os.path.join(localappdata, "Programs", "BongoCat", "BongoCat.exe"))

    return [c for c in candidates if c and os.path.isfile(c)]


class CdpWebview2Driver(CatDriver):
    name = "cdp-webview2"

    def __init__(self) -> None:
        self.port = int(config.get("cdp_port") or DEFAULT_CDP_PORT)
        self.cdp = _CDPSession(self.port)
        self.pid: int | None = None
        self._lock = threading.Lock()

    # ---- 应用定位 / 接管 ----

    def _app_path_candidates(self) -> list[str]:
        candidates = app_path_candidates()

        if self.pid is None:
            found = w32.find_process(
                name_contains="bongo",
                name_excludes=("mver", "ui", "converter", "mcp"),
            )
            if found:
                self.pid = found[0]

        return candidates

    def _ensure_attached(self) -> None:
        with self._lock:
            if self.cdp.devtools_ready():
                return

            candidates = self._app_path_candidates()
            if not candidates:
                raise DriverError(
                    "未找到可用的 BongoCat（Tauri 版）程序。"
                    "请设置 BONGOCAT_APP_PATH 指向 BongoCat.exe / bongo-cat.exe。"
                )

            app_path = candidates[0]

            # 猫在跑但没开调试端口 → 重启接管（状态由应用自身持久化恢复）
            if self.pid is not None and w32.kill_process(self.pid):
                for _ in range(50):
                    if w32.find_process(name_contains="bongo", name_excludes=("mver", "ui", "converter", "mcp")) is None:
                        break
                    time.sleep(0.2)

            env = {
                **os.environ,
                "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={self.port}",
            }
            # 必须分离：猫不能继承 MCP server 的 stdio（会污染协议通道/挂住管道）
            subprocess.Popen(
                [app_path], cwd=os.path.dirname(app_path), env=env,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            self.pid = None  # 重新发现

            deadline = time.time() + 25
            while time.time() < deadline:
                if self.cdp.devtools_ready():
                    if self.pid is None:
                        found = w32.find_process(
                            name_contains="bongo",
                            name_excludes=("mver", "ui", "converter", "mcp"),
                        )
                        if found:
                            self.pid = found[0]
                    return
                time.sleep(0.5)

            raise DriverError(
                f"以调试端口启动 {app_path} 后超时未能连接 CDP(127.0.0.1:{self.port})"
            )

    # ---- 注入 ----

    def _emit(self, event: str, payload) -> None:
        self._ensure_attached()
        js = (
            "(async () => {"
            " const inv = window.__TAURI_INTERNALS__ && window.__TAURI_INTERNALS__.invoke;"
            " if (!inv) throw new Error('window.__TAURI_INTERNALS__ 不存在');"
            " const r = await inv('plugin:event|emit',"
            f" {{ event: {json.dumps(event)}, payload: {json.dumps(payload)} }});"
            " return JSON.stringify(r === undefined ? 'ok' : r);"
            "})()"
        )
        self.cdp.evaluate(js)

    def _evaluate_json(self, js: str):
        value = self.cdp.evaluate(js)
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    # ---- CatDriver ----

    def capabilities(self) -> set[str]:
        caps = {
            CMD_PING, CMD_STATUS, CMD_SET_EXPRESSION, CMD_PLAY_MOTION,
            CMD_PRESS_KEY, CMD_RELEASE_KEY, CMD_TYPE_TEXT,
            CMD_SHOW_BUBBLE, CMD_HIDE_BUBBLE, CMD_SET_WINDOW_VISIBLE,
        }
        return caps

    def call(self, cmd: str, payload: dict) -> dict:
        if cmd == CMD_PING:
            self._ensure_attached()
            alive = self._evaluate_json(
                "JSON.stringify(!!(window.__TAURI_INTERNALS__ && window.__TAURI_INTERNALS__.invoke))",
            )
            return {"ok": alive is True or alive == "true"}

        if cmd == CMD_STATUS:
            self._ensure_attached()
            info = self._evaluate_json(_STATUS_JS) or {}
            if not isinstance(info, dict) or info.get("modelId") is None and info.get("motions") is None:
                raise DriverError("无法从成品猫读取模型信息（页面结构可能已改变）")
            win = w32.find_window(pid=self.pid, title_contains="bongo")
            return {
                "ok": True,
                "data": {
                    "modelInfo": {
                        "modelId": info.get("modelId"),
                        "mode": info.get("mode"),
                        "motions": info.get("motions", []),
                        "expressions": info.get("expressions", []),
                        "supportKeys": info.get("supportKeys", []),
                    },
                    "windowVisible": bool(win and w32.window_exists(win.hwnd)),
                },
            }

        if cmd == CMD_SET_EXPRESSION:
            self._emit("set-expression", int(payload["index"]))
            return {"ok": True}

        if cmd == CMD_PLAY_MOTION:
            self._emit("start-motion", payload["motion"])
            return {"ok": True}

        if cmd == CMD_PRESS_KEY:
            self._emit("device-changed", {"kind": "KeyboardPress", "value": payload["key"]})
            return {"ok": True}

        if cmd == CMD_RELEASE_KEY:
            self._emit("device-changed", {"kind": "KeyboardRelease", "value": payload["key"]})
            return {"ok": True}

        if cmd == CMD_TYPE_TEXT:
            for char in payload["text"]:
                key = _char_to_key(char)
                if not key:
                    continue
                self._emit("device-changed", {"kind": "KeyboardPress", "value": key})
                time.sleep(0.12)
                self._emit("device-changed", {"kind": "KeyboardRelease", "value": key})
                time.sleep(0.08)
            return {"ok": True}

        if cmd in (CMD_SHOW_BUBBLE, CMD_HIDE_BUBBLE):
            from bongocat_mcp.bubble.overlay import (
                bubble_overlay, duration_to_auto_hide_ms, ensure_overlay,
            )
            if cmd == CMD_SHOW_BUBBLE:
                self._ensure_attached()
                if self.pid is None:
                    found = w32.find_process(
                        name_contains="bongo", name_excludes=("mver", "ui", "converter", "mcp"),
                    )
                    self.pid = found[0] if found else None
                # 猫不在线直接跳过：气泡是时效性通知，不排队也不在猫恢复后补发
                if not w32.find_window(pid=self.pid, title_contains="bongo"):
                    return {"ok": False,
                            "error": "猫窗口当前不在线，气泡已跳过（不会在猫恢复后补发）"}
                overlay = ensure_overlay()
                if overlay is None:
                    return {"ok": False, "error": "气泡渲染线程未能启动"}
                overlay.show(
                    payload["text"], pid=self.pid, title_contains="bongo",
                    auto_hide_ms=duration_to_auto_hide_ms(payload.get("duration")),
                )
            else:
                bubble_overlay().hide()
            return {"ok": True}

        if cmd == CMD_SET_WINDOW_VISIBLE:
            # 全量枚举（含已隐藏窗口）：否则 hide 之后 find 不到、无法再 show
            win = w32.find_window(pid=self.pid, title_contains="bongo", visible_only=False)
            if not win:
                return {"ok": False, "error": "未找到 BongoCat 主窗口"}
            w32.set_window_visible(win.hwnd, bool(payload["visible"]))
            return {"ok": True}

        raise DriverError(f"cdp driver 不支持 {cmd}")

    def close(self) -> None:
        self.cdp.close()


_STATUS_JS = """(() => {
  const el = document.querySelector('#app');
  const app = el && el.__vue_app__;
  const pinia = app && app.config && app.config.globalProperties && app.config.globalProperties.$pinia;
  const s = pinia && pinia._s && pinia._s.get('model');
  if (!s) return null;
  const m = s.currentModel || {};
  return {
    modelId: m.id ?? null,
    mode: m.mode ?? null,
    motions: (s.currentMotions || []).flatMap(
      ([group, items]) => (items || []).map(
        (it) => ({ group, no: it.no, name: it.name })
      )
    ),
    expressions: (s.currentExpressions || []).map((e, i) => ({
      index: i, name: (e && (e.Name || e.name)) || ('expression-' + i)
    })),
    supportKeys: Object.keys(s.supportKeys || {})
  };
})()"""
