"""Mver 新猫自动接入：发现 -> 开启网络接收 -> 重启 -> 切换 driver。

原则：不修改原程序本体；仅用文本级补丁改写猫自己的 config.json
（与其设置界面写入的是同一份文件、同样的键），并以提权重启猫进程。
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from bongocat_mcp import config
from bongocat_mcp.dispatch import log_event
from bongocat_mcp.drivers import win32_utils as w32


def running_mver_cats() -> list[dict]:
    """发现所有运行中的 Mver 猫：[{pid, dir, title, hwnd, rect}]。"""
    cats = []
    seen_dirs: set[str] = set()
    for pid, name in w32.list_processes():
        if "mver" not in name.lower():
            continue
        exe = w32.process_exe_path(pid)
        if not exe:
            continue
        exe_dir = str(Path(exe).parent)
        if exe_dir.lower() in seen_dirs:
            continue
        seen_dirs.add(exe_dir.lower())
        win = w32.find_window(pid=pid)
        cats.append({
            "pid": pid,
            "dir": exe_dir,
            "title": win.title if win else "",
        })
    return cats


def _patch_network_receiver(config_path: Path) -> bool:
    """把 config.json 的 network 段改为 接收模式（保留原作者注释）。"""
    raw = config_path.read_text(encoding="utf-8", errors="replace")
    patched = raw
    patched = re.sub(r'"network"\s*:\s*false', '"network": true', patched)
    patched = re.sub(r'"is_sender"\s*:\s*true', '"is_sender": false', patched)
    if patched == raw:
        return False  # 本已是接收模式
    config_path.write_text(patched, encoding="utf-8")
    return True


def _is_receiver_mode(config_path: Path) -> bool:
    try:
        raw = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    clean = re.sub(r"^\s*//.*$", "", raw, flags=re.M)
    clean = re.sub(r'([,\[\]\d"\'\}])\s*//.*$', r"\1", clean, flags=re.M)
    try:
        import json
        net = json.loads(clean).get("network", {})
    except ValueError:
        return False
    return bool(net.get("network")) and not net.get("is_sender")


def _elevated_kill(pid: int) -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Start-Process taskkill -ArgumentList '/F /T /PID {pid}' -Verb RunAs -Wait"],
        capture_output=True, timeout=30,
    )


def _elevated_start(cat_dir: str) -> int | None:
    ps = (
        f'$p = Start-Process -FilePath "{cat_dir}\\Bongo Cat Mver.exe" '
        f'-WorkingDirectory "{cat_dir}" -Verb RunAs -PassThru; Write-Output $p.Id'
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    try:
        return int(out.splitlines()[-1])
    except (ValueError, IndexError):
        return None


def auto_redirect() -> dict | None:
    """配置的猫没在跑（或未配置）、但有别的 Mver 在跑时，自动切换 mver_dir。

    由 /api/status 周期调用，实现「添加新猫后自动识别接入」。
    返回切换信息（未发生切换时 None）。
    """
    current = (config.get("mver_dir") or "").strip().lower().rstrip("\\/")
    cats = running_mver_cats()
    if not cats:
        return None

    configured_running = any(c["dir"].lower().rstrip("\\/") == current for c in cats)
    if configured_running:
        return None

    other = cats[0]
    config.save({"mver_dir": other["dir"]})
    info = {
        "from": current, "to": other["dir"], "pid": other["pid"],
        "note": "检测到配置的猫未运行，已自动切换到运行中的 Mver",
    }
    log_event("auto-redirect", info)
    return info


def onboard(dir_: str | None = None) -> dict:
    """一键接入一个 Mver 猫：定位 -> 开网络接收 -> 重启 -> 切换 driver。"""
    steps: list[str] = []

    # 1) 定位目标目录：显式指定 > 运行中的猫 > 配置值
    cats = running_mver_cats()
    target = None
    if dir_:
        target = dir_
        steps.append(f"目标目录: {dir_}")
    elif cats:
        target = cats[0]["dir"]
        steps.append(f"自动定位到运行中的猫: {target} (pid {cats[0]['pid']})")
    elif config.get("mver_dir"):
        target = config.get("mver_dir")
        steps.append(f"使用配置目录: {target}")

    if not target:
        return {"ok": False, "steps": steps + ["未找到猫：请先启动它或在配置中填写 mver_dir"]}

    cat_dir = Path(target)
    if not (cat_dir / "Bongo Cat Mver.exe").exists():
        return {"ok": False, "steps": steps + [f"{target} 下没有 Bongo Cat Mver.exe"]}

    # 2) 网络接收模式（只改 config.json，不改程序；保留注释）
    cfg_path = cat_dir / "config.json"
    if not cfg_path.exists():
        return {"ok": False, "steps": steps + [f"缺少 {cfg_path}"]}
    if _is_receiver_mode(cfg_path):
        steps.append("已是网络接收模式，无需修改")
    else:
        _patch_network_receiver(cfg_path)
        steps.append("已开启网络同步（接收模式）——仅改写 config.json，保留注释")

    # 3) 重启猫（exe 清单要求管理员：提权结束 + 提权启动）
    running = [c for c in cats if c["dir"].lower().rstrip("\\/") == str(cat_dir).lower().rstrip("\\/")]
    if running:
        _elevated_kill(running[0]["pid"])
        time.sleep(1.5)
        steps.append(f"已重启猫进程（原 pid {running[0]['pid']}）")
    else:
        steps.append("猫未在运行，直接启动")

    new_pid = _elevated_start(str(cat_dir))
    if new_pid:
        steps.append(f"新进程 pid {new_pid}，等待窗口…")
    else:
        steps.append("警告：启动命令未返回 pid（可能已弹出 UAC）")

    # 4) 等窗口出现
    for _ in range(30):
        time.sleep(0.5)
        win = w32.find_window(title_contains="Bongo Cat Mver")
        if win:
            steps.append(f"猫窗口就绪: rect={w32.get_window_rect(win.hwnd)}")
            break

    # 5) 切换配置并重建 driver
    config.save({"mver_dir": str(cat_dir)})
    from bongocat_mcp.detect import resolve_driver
    from bongocat_mcp.drivers.base import DriverError
    try:
        d = resolve_driver(refresh=True)
        steps.append(f"driver 重建完成: {d.name}")
        result = {"ok": True, "steps": steps, "dir": str(cat_dir), "driver": d.name}
    except DriverError as exc:
        steps.append(f"driver 重建失败: {exc}")
        result = {"ok": False, "steps": steps, "dir": str(cat_dir)}

    log_event("onboard", {"dir": str(cat_dir), "ok": result["ok"]})
    return result
