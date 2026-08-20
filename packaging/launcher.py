"""bongocat-mcp 打包版入口（仅被 PyInstaller 打包的 exe 使用，不影响源码运行）。

exe 子命令（不带参数 = 仪表盘）：
  bongocat-mcp.exe              # 仪表盘：自动隐藏后台运行并打开浏览器
  bongocat-mcp.exe server       # MCP stdio server（供 astrbot / ZCode 等 client 拉起）
  bongocat-mcp.exe mirror       # Mver 独立镜像（等价 python mver-mirror.py）
  bongocat-mcp.exe stop         # 停止后台仪表盘与镜像（等价 stop.bat）

打包适配只做三件事，全部通过环境变量 / 数据文件解决，不改源码：
1. BONGOCAT_MCP_CONFIG_FILE 指向 exe 旁的 config.json（首次运行从内置示例生成）
2. web/index.html、mver-mirror.py、config.example.json 按原相对路径打进 _internal
3. 源码里 dashboard / mver-mirror 的「隐藏重启」会把自身脚本路径作为参数重新拉起
   exe（frozen 下 sys.executable 即 exe），launcher 识别这类参数路由回对应入口
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def _resource_dir() -> Path:
    """打包后的资源目录（onedir 为 exe 旁的 _internal）。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _ensure_local_config() -> None:
    """配置固定放 exe 旁：解压即用、删目录即卸载，不碰系统其他位置。"""
    exe_dir = _exe_dir()
    os.environ["BONGOCAT_MCP_CONFIG_FILE"] = str(exe_dir / "config.json")
    cfg = exe_dir / "config.json"
    if not cfg.exists():
        example = _resource_dir() / "config.example.json"
        try:
            if example.exists():
                cfg.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass  # 目录不可写等场景交给 config 层默认值兜底


def _run_dashboard() -> None:
    import dashboard

    dashboard.main()


def _run_server() -> None:
    import server

    server.mcp.run()


def _run_mirror(args: list[str]) -> None:
    """runpy 执行打包为数据文件的 mver-mirror.py（模块名带连字符，无法 import）。"""
    script = _resource_dir() / "mver-mirror.py"
    saved_argv = sys.argv
    sys.argv = [str(script), *args]
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        sys.argv = saved_argv


def _stop_all() -> None:
    # 顺序与 stop.bat 一致：先镜像后仪表盘；一个失败不影响另一个
    try:
        _run_mirror(["--stop"])
    except SystemExit:
        pass
    except Exception as exc:  # noqa: BLE001 停止流程尽量走完
        print(f"停止镜像失败: {exc}")
    try:
        import dashboard

        dashboard._stop()
    except Exception as exc:  # noqa: BLE001
        print(f"停止仪表盘失败: {exc}")


def main() -> None:
    _ensure_local_config()
    args = sys.argv[1:]
    cmd = args[0] if args else "dashboard"

    if cmd == "server":
        _run_server()
    elif cmd == "mirror" or "mver-mirror" in cmd:
        # 第二个条件：mver-mirror 隐藏重启时会以自身脚本路径为参数拉起 exe
        _run_mirror(args[1:] if cmd == "mirror" else [])
    elif cmd == "stop":
        _stop_all()
    elif cmd == "dashboard" or cmd.startswith("-") or cmd.endswith((".py", ".pyc")):
        # dashboard.main 自行解析 --stop/--visible；
        # endswith 分支：dashboard 隐藏重启时以 dashboard.pyc 路径为参数拉起 exe
        _run_dashboard()
    else:
        print(__doc__)
        print(f"未知子命令: {cmd}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
