"""Mver 透明镜像进程：不开 astrbot 时单独运行，让接收模式的猫恢复本机键鼠跟随。

用法（需 Mver 已开网络接收模式）：
  python mver-mirror.py            # 默认隐藏运行（自动转为无终端窗口的后台进程）
  python mver-mirror.py --visible  # 前台保留终端（调试用），Ctrl+C 退出
  python mver-mirror.py --stop     # 结束隐藏运行的镜像进程

隐藏进程的 pid 记录在 .mver-mirror.pid，--stop 据此结束；也可在任务管理器结束
对应 python 进程。镜像停止后猫会失去键鼠响应（重新运行即恢复）。
配置读取 config.json（mver_dir / mver_port），也可用环境变量 BONGOCAT_* 覆盖。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

PID_FILE = Path(__file__).resolve().parent / ".mver-mirror.pid"


def _stop() -> int:
    if not PID_FILE.exists():
        print("没有找到隐藏镜像的 PID 文件（可能未在运行）")
        return 1
    pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    ok = subprocess.run(
        ["taskkill", "/F", "/PID", str(pid)], capture_output=True,
    ).returncode == 0
    PID_FILE.unlink(missing_ok=True)
    print(f"结束镜像进程 pid={pid}: {'成功' if ok else '失败（可能已退出）'}")
    return 0


def _relaunch_hidden() -> None:
    """把自身重新拉起为无控制台窗口的进程，原进程随即退出。

    这样无论用户双击、终端还是 start 启动，都不会留下常驻终端窗口；
    前台调试用 --visible 跳过。
    """
    if os.name != "nt" or os.environ.get("BONGOCAT_MIRROR_HIDDEN") == "1":
        return
    env = {**os.environ, "BONGOCAT_MIRROR_HIDDEN": "1"}
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


def main() -> int:
    if "--stop" in sys.argv:
        return _stop()

    if "--visible" not in sys.argv:
        _relaunch_hidden()

    from bongocat_mcp.drivers.mver_udp import MverUdpDriver

    d = MverUdpDriver()
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    print(f"镜像中 -> {d.host}:{d.port}（pid {os.getpid()}；停止：python mver-mirror.py --stop）",
          flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        d.close()
        PID_FILE.unlink(missing_ok=True)
        print("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
