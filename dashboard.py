"""bongocat-mcp 仪表盘：本地 Web UI，查看猫状态 / 编辑配置 / 试玩全部控制工具。

用法：
  python dashboard.py            # 默认隐藏窗口后台运行（自动打开浏览器）
  python dashboard.py --stop     # 停止隐藏运行的仪表盘
  python dashboard.py --visible  # 前台调试模式（终端可见）

说明：dashboard 持有自己的 driver 实例，可与 astrbot 的 stdio server 并行使用；
embedded / cdp 无冲突；mver 双镜像为良性叠加（两路相同状态帧），
聊天气泡可能在两个进程各渲染一个（此限制记录于 README）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from bongocat_mcp import config
from bongocat_mcp.detect import current_driver, resolve_driver
from bongocat_mcp.dispatch import dispatch, recent_events
from bongocat_mcp.drivers.base import ALL_COMMANDS, DriverError
from bongocat_mcp.onboard import auto_redirect, onboard

app = FastAPI(title="bongocat-mcp dashboard")

WEB_DIR = Path(__file__).resolve().parent / "web"
PID_FILE = Path(__file__).resolve().parent / ".dashboard.pid"

_last_redirect_check = 0.0
# sync handler 跑在线程池，状态轮询可能并发到达；check-then-act 需互斥
_redirect_lock = threading.Lock()


class ConfigUpdate(BaseModel):
    values: dict


class DriverSwitch(BaseModel):
    name: str  # "auto" | embedded | cdp | mver


class ToolCall(BaseModel):
    cmd: str
    payload: dict = {}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/status")
def api_status():
    global _last_redirect_check

    # 新猫自动识别（节流：至多每 5 秒探测一次运行中的 Mver）
    import time as _time
    now = _time.time()
    redirected = None
    with _redirect_lock:
        if now - _last_redirect_check > 5:
            _last_redirect_check = now
            try:
                redirected = auto_redirect()
            except Exception:  # noqa: BLE001 探测失败不影响状态接口
                redirected = None

    driver = current_driver()
    body: dict = {
        "ok": True,
        "driver": driver.name if driver else None,
        "capabilities": sorted(driver.capabilities()) if driver else [],
        "commands": sorted(ALL_COMMANDS),
        "config": config.snapshot(),
        "configFile": config.config_file_path(),
        "autoRedirect": redirected,
    }

    # 猫状态（探测不到 driver 时不算错误，前端显示引导）
    try:
        d = resolve_driver(refresh=bool(redirected))
        status = d.call("status", {})
        body["cat"] = status
        body["driver"] = d.name
        body["capabilities"] = sorted(d.capabilities())
        mirror = getattr(d, "_thread", None)
        body["mirrorAlive"] = bool(mirror and mirror.is_alive())
    except DriverError as exc:
        body["cat"] = None
        body["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 状态接口不 500，打印堆栈便于排查
        import traceback
        traceback.print_exc()
        body["cat"] = None
        body["error"] = f"状态读取异常: {exc}"

    return body


@app.get("/api/config")
def api_get_config():
    return {"ok": True, "file": config.config_file_path(),
            "fileConfig": config.file_config(), "effective": config.snapshot()}


@app.post("/api/config")
def api_set_config(body: ConfigUpdate):
    saved = config.save(body.values)

    # 立即按新配置重建 driver（mver_dir/app_path 等在 driver 构造时读取，
    # 不重建则状态一直显示旧值）。新配置不可用时保留旧实例并给出提示；
    # 任何异常都不 500（前端要拿 JSON 才能记日志），打印堆栈便于排查。
    rebuilt: dict = {}
    try:
        d = resolve_driver(refresh=True)
        rebuilt = {"driver": d.name, "capabilities": sorted(d.capabilities())}
    except DriverError as exc:
        rebuilt = {"driver": None, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        rebuilt = {"driver": None, "error": f"driver 重建异常: {exc}"}

    return {"ok": True, "saved": saved, "rebuilt": rebuilt,
            "note": "已写入 config.json 并重建 driver"}


@app.post("/api/driver")
def api_switch_driver(body: DriverSwitch):
    name = body.name.strip().lower()
    if name == "auto":
        config.save({"driver": ""})
    else:
        if name not in ("embedded", "cdp", "mver"):
            return JSONResponse({"ok": False, "error": f"未知 driver: {name}"}, 400)
        config.save({"driver": name})

    try:
        d = resolve_driver(forced=None, refresh=True)
        return {"ok": True, "driver": d.name, "capabilities": sorted(d.capabilities())}
    except DriverError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 不 500，打印堆栈便于排查
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": f"driver 重建异常: {exc}"}


@app.post("/api/tool")
def api_tool(body: ToolCall):
    if body.cmd not in ALL_COMMANDS:
        return JSONResponse({"ok": False, "error": f"未知命令: {body.cmd}"}, 400)
    try:
        result = dispatch(body.cmd, body.payload)
        return result
    except DriverError as exc:
        return {"ok": False, "error": str(exc)}
    except (KeyError, ValueError, TypeError) as exc:
        # dispatch 已拦一道；这里兜底未预料的 payload 形状问题，避免 500
        return JSONResponse(
            {"ok": False, "error": f"参数不合法（{type(exc).__name__}: {exc}）"}, 400,
        )


class OnboardRequest(BaseModel):
    dir: str | None = None


@app.post("/api/mver/onboard")
def api_mver_onboard(body: OnboardRequest):
    """一键接入 Mver 新猫：开网络接收（仅改 config.json）+ 提权重启 + 切换 driver。"""
    return onboard(body.dir)


@app.get("/api/events")
def api_events():
    return {"ok": True, "events": recent_events()}


def _port_busy(host: str, port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _stop() -> int:
    if not PID_FILE.exists():
        print("没有找到仪表盘的 PID 文件（可能未在运行）")
        return 1
    pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    ok = subprocess.run(
        ["taskkill", "/F", "/PID", str(pid)], capture_output=True,
    ).returncode == 0
    PID_FILE.unlink(missing_ok=True)
    print(f"结束仪表盘进程 pid={pid}: {'成功' if ok else '失败（可能已退出）'}")
    return 0


def _relaunch_hidden() -> None:
    """把自身重新拉起为无控制台窗口的进程，原进程随即退出（前台调试用 --visible）。"""
    if os.name != "nt" or os.environ.get("BONGOCAT_DASHBOARD_HIDDEN") == "1":
        return
    env = {**os.environ, "BONGOCAT_DASHBOARD_HIDDEN": "1"}
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve())],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=str(Path(__file__).resolve().parent),
    )
    sys.exit(0)


def main() -> None:
    import uvicorn

    host = config.get("dashboard_host") or "127.0.0.1"
    port = int(config.get("dashboard_port") or 8766)

    if "--stop" in sys.argv:
        raise SystemExit(_stop())

    if "--visible" not in sys.argv:
        # 隐藏进程的报错不可见：先代检端口，已在运行就留在本终端提示
        if _port_busy(host, port):
            print(f"仪表盘已在运行（{host}:{port}）。停止：python dashboard.py --stop")
            return
        _relaunch_hidden()

    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    url = f"http://{host}:{port}"
    print(f"bongocat-mcp 仪表盘: {url}（pid {os.getpid()}；停止：python dashboard.py --stop）",
          flush=True)

    threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
