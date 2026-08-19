"""BongoCat MCP server 完整联调测试（多 driver 版）。

用法：
  cd mcp-server
  .venv/Scripts/python.exe test_client.py            # 自动探测 driver
  .venv/Scripts/python.exe test_client.py embedded   # 强制 embedded（需自编译版猫在跑）
  .venv/Scripts/python.exe test_client.py cdp        # 强制 cdp（Tauri 成品）
  .venv/Scripts/python.exe test_client.py mver       # 强制 mver（Mver 皮肤版，需开网络接收）

覆盖全部 MCP tools；不支持的命令按能力降级记为 SKIP。
注意：链路打通以返回 ok 为准，Live2D 实际渲染效果需肉眼观察猫。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main() -> None:
    env = {k: v for k, v in os.environ.items() if k.startswith("BONGOCAT_")}

    if len(sys.argv) > 1:
        env["BONGOCAT_MCP_DRIVER"] = sys.argv[1]

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().parent / "server.py")],
        env=env or None,
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print(f"== tools ({len(tools.tools)}) ==")
            for tool in tools.tools:
                print(f"  - {tool.name}")

            passed: list[str] = []
            failed: list[str] = []
            skipped: list[str] = []

            async def call(name: str, args: dict, *, collect: bool = False):
                try:
                    result = await session.call_tool(name, args)
                    texts = [getattr(c, "text", str(c)) for c in result.content]
                    text = texts[0] if texts else ""
                except Exception as exc:  # noqa: BLE001
                    failed.append(name)
                    print(f"  FAIL {name}({args}) -> {type(exc).__name__}: {exc}")
                    return [] if collect else ""

                parsed = None
                try:
                    parsed = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    pass

                # 能力降级：driver 明确报不支持
                if isinstance(parsed, dict) and parsed.get("supported") is False:
                    skipped.append(name)
                    print(f"  SKIP {name}({args}) -> {parsed.get('error')}")
                    return [] if collect else text

                if isinstance(parsed, dict) and parsed.get("ok") is False \
                        and parsed.get("supported") is not False:
                    failed.append(name)
                    print(f"  FAIL {name}({args}) -> {text[:200]}")
                    return [] if collect else text

                passed.append(name)
                print(f"  OK   {name}({args}) -> {text[:200]}")
                return texts if collect else text

            async def call_json(name: str, args: dict) -> dict:
                text = await call(name, args)
                try:
                    return json.loads(text) if isinstance(text, str) and text else {}
                except json.JSONDecodeError:
                    return {}

            print("== ping / status ==")
            await call("ping", {})
            status = await call_json("get_cat_status", {})

            print(f"     driver={status.get('driver')} "
                  f"capabilities={status.get('capabilities')}")

            model_info = status.get("data", {}).get("modelInfo") or {}
            expressions = model_info.get("expressions") or []
            motions = model_info.get("motions") or []

            print(f"     (expressions={len(expressions)}, motions={len(motions)})")

            print("== 表情 ==")
            await call("list_expressions", {})
            if expressions:
                await call("set_expression", {"index": 0})
                await asyncio.sleep(2)
            else:
                skipped.append("set_expression")
                print("  SKIP set_expression（无表情数据）")

            print("== 动作 ==")
            motion_texts = await call("list_motions", {}, collect=True)

            real_motion = None
            if isinstance(motion_texts, list):
                for block in motion_texts:
                    try:
                        obj = json.loads(block) if isinstance(block, str) else block
                        if isinstance(obj, dict) and (obj.get("group") or obj.get("keys")):
                            real_motion = obj
                            break
                    except (json.JSONDecodeError, TypeError):
                        continue

            if real_motion:
                await call("play_motion", {"motion": real_motion})
                await asyncio.sleep(2)
            else:
                skipped.append("play_motion")
                print("  SKIP play_motion（无可用 motion）")

            print("== 按键 / 打字 / 爪 ==")
            await call("press_key", {"key": "KeyA"})
            await asyncio.sleep(0.5)
            await call("release_key", {"key": "KeyA"})
            await call("type_text", {"text": "Hello BongoCat!"})
            await asyncio.sleep(3)
            await call("set_hand", {"left": True, "right": False})
            await asyncio.sleep(1)
            await call("set_hand", {"left": False, "right": False})

            print("== 参数 ==")
            await call("set_parameter", {"id": "ParamAngleX", "value": 0})

            print("== 气泡 ==")
            await call("show_bubble", {"text": "MCP 完整联调测试"})
            await asyncio.sleep(6)
            await call("hide_bubble", {})

            print("== 窗口 ==")
            # 只测显示：隐藏/显示可能触发渲染上下文丢失
            await call("set_window_visible", {"visible": True})

            print(f"\n== 结果: {len(passed)} 通过, {len(skipped)} 跳过, {len(failed)} 失败 ==")
            if skipped:
                print(f"跳过项(能力降级): {sorted(set(skipped))}")
            if failed:
                print(f"失败项: {sorted(set(failed))}")
                sys.exit(1)

            print("全部通过")


if __name__ == "__main__":
    asyncio.run(main())
