"""driver 探测与缓存：配置强制指定 > 自动探测（embedded → cdp → mver）。"""

from __future__ import annotations

import sys
import threading

from bongocat_mcp import config
from bongocat_mcp.drivers.base import CatDriver, DriverError

_driver: CatDriver | None = None
# 仪表盘（FastAPI 线程池 + 轮询 + dispatch 失效重试）会并发 resolve；
# close/重建必须互斥，否则可能双 close、或遗留永不停止的 mver 孤儿发送线程
_driver_lock = threading.Lock()


def _create_by_name(name: str) -> CatDriver:
    if name == "embedded":
        from bongocat_mcp.drivers.embedded_http import EmbeddedHttpDriver
        return EmbeddedHttpDriver()
    if name == "cdp":
        from bongocat_mcp.drivers.cdp_webview2 import CdpWebview2Driver
        return CdpWebview2Driver()
    if name == "mver":
        from bongocat_mcp.drivers.mver_udp import MverUdpDriver
        return MverUdpDriver()
    raise DriverError(
        f"未知 driver: {name!r}（可选 embedded / cdp / mver）"
    )


def resolve_driver(*, refresh: bool = False, forced: str | None = None) -> CatDriver:
    """取当前可用的 driver。

    forced 优先（仪表盘运行时切换用），其次配置的 driver 键，
    最后自动探测；refresh 时丢弃缓存重新探测。
    """
    global _driver

    with _driver_lock:
        if _driver is not None and not refresh and forced is None:
            return _driver

        if _driver is not None:
            try:
                _driver.close()
            except Exception:  # noqa: BLE001
                pass
            _driver = None

        name = (forced or config.get("driver") or "").strip().lower()
        if name:
            _driver = _create_by_name(name)
            # stdio server 的 stdout 是 MCP JSONRPC 专线，日志必须走 stderr
            print(f"[detect] forced/name={name!r} -> {_driver.name}",
                  file=sys.stderr, flush=True)
            return _driver

        from bongocat_mcp.drivers.embedded_http import EmbeddedHttpDriver
        if EmbeddedHttpDriver.probe():
            _driver = EmbeddedHttpDriver()
            return _driver

        from bongocat_mcp.drivers import win32_utils as w32
        from bongocat_mcp.drivers.cdp_webview2 import CdpWebview2Driver, app_path_candidates
        if (config.get("app_path")
                or w32.find_process(
                    name_contains="bongo", name_excludes=("mver", "ui", "converter"),
                )
                or app_path_candidates()):
            _driver = CdpWebview2Driver()
            return _driver

        from bongocat_mcp.drivers.mver_udp import MverUdpDriver
        if config.get("mver_dir") or w32.find_process(name_contains="mver"):
            _driver = MverUdpDriver()
            return _driver

        raise DriverError(
            "没有找到可控制的猫。可选："
            "1) 启动带控制通道的自编译 BongoCat（embedded）；"
            "2) 启动任意 Tauri 版 BongoCat 成品或在配置中填写 app_path（cdp，将自动重启接管）；"
            "3) 启动 BongoCatMver 并开网络接收，或配置 mver_dir（mver）。"
            "也可在配置中把 driver 设为 embedded/cdp/mver 强制指定。"
        )


def current_driver() -> CatDriver | None:
    return _driver
