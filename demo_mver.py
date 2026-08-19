"""mver 版猫完整调用演示：以 MCP client（astrbot 同款方式）拉起 server.py，
按剧情顺序调用全部工具。运行时请看着屏幕上的猫。

用法：.venv/Scripts/python.exe demo_mver.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def short(text: str, n: int = 150) -> str:
    return text.replace("\n", " ")[:n]


async def main() -> None:
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().parent / "server.py")],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(name: str, args: dict | None = None, wait: float = 0.0):
                result = await session.call_tool(name, args or {})
                text = getattr(result.content[0], "text", "")
                try:
                    data = json.loads(text)
                except (json.JSONDecodeError, AttributeError):
                    data = None
                if isinstance(data, dict):
                    mark = "OK " if data.get("ok") else (
                        "SKIP" if data.get("supported") is False else "FAIL")
                elif isinstance(data, list):
                    mark = "OK "
                else:
                    mark = "FAIL"
                print(f"  [{mark}] {name}({args or ''}) -> "
                      f"{short(json.dumps(data, ensure_ascii=False))}")
                if wait:
                    await asyncio.sleep(wait)
                return data

            print("== 0. 连接与状态 ==")
            await call("ping")
            status = await call("get_cat_status")
            exprs: list = []
            motion: dict | None = None
            if isinstance(status, dict) and status.get("ok"):
                info = status.get("data", {}).get("modelInfo", {})
                exprs = info.get("expressions") or []
                motions = info.get("motions") or []
                motion = next((m for m in motions if m.get("keys")), None)
                print(f"     模型: {info.get('modelId')} / {info.get('mode')}，"
                      f"表情 {len(exprs)} 组，动作 {len(motions)} 组")

            print("== 1. 显示猫窗口（隐藏状态也能找回；气泡需要它定位）==")
            await call("set_window_visible", {"visible": True}, wait=1)

            print("== 2. 按键动画：KeyA 按住 1.5s 再松开（看爪子）==")
            await call("press_key", {"key": "KeyA"}, wait=1.5)
            await call("release_key", {"key": "KeyA"}, wait=0.5)

            print("== 3. 打字动画：逐字符敲出 hello cat ==")
            await call("type_text", {"text": "hello cat"}, wait=4)

            print("== 4. 表情切换：依次切换前 3 个表情，每个停留 2.5s ==")
            for i, e in enumerate(exprs[:3]):
                print(f"     -> {i} · {e.get('name')}（请观察猫脸）")
                await call("set_expression", {"index": i}, wait=2.5)
            if not exprs:
                print("     此皮肤模型无表情素材，跳过（诚实降级）")

            print("== 5. 聊天气泡：打字机动画显示两句话，随后隐藏 ==")
            await call("show_bubble", {"text": "喵～我是赛琳娜！"}, wait=4)
            await call("show_bubble", {"text": "今天也要元气满满哦 (๑˃ᴗ˂)ﻭ"}, wait=5)
            await call("hide_bubble", wait=0.5)

            print("== 6. 动作播放 ==")
            if motion:
                print(f"     -> {motion.get('name')} keys={motion.get('keys')}")
                await call("play_motion", {"motion": motion}, wait=2.5)
            else:
                print("     此皮肤模型无动作素材，跳过（诚实降级）")

            print("== 7. 能力门控演示：set_hand 不为 mver 支持 ==")
            await call("set_hand", {"left": True, "right": False})

            print("\n演示完成。")


if __name__ == "__main__":
    asyncio.run(main())
