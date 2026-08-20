"""astrbot_plugin_bongocat 离线单元测试。

stub 掉 astrbot 模块后导入插件 main.py，验证核心逻辑：
路由三车道 / 名单过滤 / 摘要触发与冷却 / 发送失败缓冲保留 /
表情能力感知与降级 / LLM 摘要回退 / 消息段占位。

运行：python astrbot-plugin/tests/test_bongocat_plugin.py（bongocat-mcp 仓库内，
任意 Python ≥3.10，无需安装 astrbot/aiohttp）。
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "stubs"))          # 先让 astrbot 指向桩
sys.path.insert(0, os.path.join(HERE, "..", "astrbot_plugin_bongocat"))

import main  # noqa: E402  插件本体（经上面的 sys.path 导入）
from astrbot.api import AstrBotConfig  # noqa: E402  桩

BOT = "10000"


# ---------------- 事件构造 ----------------

class Sender:
    def __init__(self, uid: str, nick: str) -> None:
        self.user_id = uid
        self.nickname = nick


class GroupInfo:
    def __init__(self, gid: str, name: str) -> None:
        self.group_id = gid
        self.group_name = name


class MsgObj:
    def __init__(self, self_id, sender, message, group=None):
        self.type = "group" if group else "friend"
        self.self_id = self_id
        self.sender = sender
        self.message = message
        self.group = group
        self.message_str = " ".join(
            getattr(s, "text", "") for s in message if hasattr(s, "text"))
        self.raw_message = None
        self.timestamp = 0
        self.session_id = "sess"
        self.message_id = "mid"

    @property
    def group_id(self) -> str:
        return self.group.group_id if self.group else ""


def make_event(sender_id="20000", nick="Alice", segs=None,
               group_id="", group_name="", self_id=BOT, private=None):
    group = GroupInfo(group_id, group_name) if group_id else None
    mo = MsgObj(self_id, Sender(sender_id, nick), segs or [], group)
    ev = SimpleNamespace(message_obj=mo)
    ev.is_private_chat = (lambda: private if private is not None
                          else not bool(group_id))
    ev.get_sender_name = lambda: nick
    ev.plain_result = lambda text: ("PLAIN", text)
    return ev


# ---------------- 插件构造 ----------------

DEFAULT_CFG = {
    "dashboard_url": "http://127.0.0.1:8766",
    "routing": {
        "enable_private": True, "enable_group": True,
        "ignore_commands": True, "relay_at_bot": True, "relay_reply": True,
        "direct_keywords": [], "sender_whitelist": [],
    },
    "lists": {
        "group_blacklist": [], "group_whitelist": [],
        "private_blacklist": [], "private_whitelist": [],
        "sender_blacklist": [],
    },
    "digest": {
        "mode": "plain", "trigger_count": 3, "trigger_idle_sec": 180,
        "hot_window_sec": 60, "hot_count": 99, "cooldown_sec": 60,
        "max_buffer": 50, "max_len": 100, "llm_timeout_sec": 5,
        "spacing": 0.0,
    },
    "min_interval": 0.0, "max_text_len": 60,
    "bubble_duration": 8, "expression_duration": 15,
}


def make_plugin(overrides=None, tool_ok=True, status=None, provider=None):
    cfg = copy.deepcopy(DEFAULT_CFG)
    for path, value in (overrides or {}).items():
        cur = cfg
        keys = path.split(".")
        for k in keys[:-1]:
            cur = cur[k]
        cur[keys[-1]] = value

    plugin = main.BongoCatPlugin(None, AstrBotConfig(cfg))
    plugin.context = type("Ctx", (), {"get_using_provider": lambda self: provider})()
    plugin._calls = []
    plugin._get_calls = 0

    async def fake_tool(cmd, payload):
        if tool_ok:  # 只记录成功送达的调用（真实语义：失败时气泡没显示）
            plugin._calls.append((cmd, payload))
        return {"ok": tool_ok, "driver": "stub"}

    async def fake_get(path):
        plugin._get_calls += 1
        return status

    plugin._tool = fake_tool
    plugin._get = fake_get
    return plugin


def bubbles(plugin) -> list[str]:
    return [p["text"] for c, p in plugin._calls if c == "show-bubble"]


EXPR_STATUS = {
    "ok": True, "driver": "stub", "capabilities": ["ping", "set-expression",
                                                   "show-bubble"],
    "cat": {"ok": True, "data": {"modelInfo": {"expressions": [
        {"index": 0, "name": "默认"}, {"index": 2, "name": "星星眼"},
    ]}}},
}

NO_EXPR_STATUS = {
    "ok": True, "driver": "stub", "capabilities": ["ping", "show-bubble"],
    "cat": {"ok": True, "data": {"modelInfo": {"expressions": []}}},
}


# ---------------- 测试用例 ----------------

async def t_self_message_ignored():
    p = make_plugin()
    await p._route(make_event(self_id="1", sender_id="1", segs=[], private=True))
    assert not p._calls


async def t_private_direct_relay():
    p = make_plugin()
    await p._route(make_event(segs=[main.Comp.Plain("hello")], private=True))
    assert bubbles(p) == ["[私] Alice: hello"]


async def t_private_blacklist():
    p = make_plugin(overrides={"lists.private_blacklist": ["20000"]})
    await p._route(make_event(segs=[main.Comp.Plain("hi")], private=True))
    assert not p._calls


async def t_private_whitelist():
    p = make_plugin(overrides={"lists.private_whitelist": ["99999"]})
    await p._route(make_event(segs=[main.Comp.Plain("hi")], private=True))
    assert not p._calls


async def t_other_message_ignored():
    p = make_plugin()
    await p._route(make_event(segs=[main.Comp.Plain("x")], private=False))
    assert not p._calls


async def t_group_normal_goes_buffer():
    p = make_plugin()
    await p._route(make_event(segs=[main.Comp.Plain("普通消息")],
                              group_id="111", group_name="闲聊群"))
    assert not p._calls
    assert len(p._buffers["111"]) == 1
    assert p._gname["111"] == "闲聊群"


async def t_digest_fires_on_count():
    p = make_plugin()
    for i in range(3):
        await p._route(make_event(sender_id=f"2{i}000", nick=f"人{i}",
                                  segs=[main.Comp.Plain(f"msg{i}")],
                                  group_id="111", group_name="闲聊群"))
    await asyncio.sleep(0.05)  # 让 create_task 的摘要任务跑完
    bs = bubbles(p)
    assert len(bs) == 1 and "群摘要·3条" in bs[0] and "闲聊群" in bs[0]
    assert "人0×1" in bs[0] and "最新「msg2" in bs[0]
    assert not p._buffers.get("111")          # 成功后消费缓冲


async def t_at_bot_direct():
    p = make_plugin()
    await p._route(make_event(segs=[main.Comp.At(qq=BOT, name="猫"),
                                    main.Comp.Plain("在吗")],
                              group_id="111", group_name="闲聊群"))
    assert bubbles(p) == ["[群·闲聊群] Alice: @猫 在吗"]


async def t_reply_to_bot_direct():
    p = make_plugin()
    await p._route(make_event(segs=[main.Comp.Reply(sender_id=BOT),
                                    main.Comp.Plain("收到")],
                              group_id="111", group_name="闲聊群"))
    assert len(bubbles(p)) == 1 and "[引用] 收到" in bubbles(p)[0]


async def t_keyword_direct():
    p = make_plugin(overrides={"routing.direct_keywords": ["老板"]})
    await p._route(make_event(segs=[main.Comp.Plain("老板喊你")],
                              group_id="111", group_name="闲聊群"))
    assert len(bubbles(p)) == 1


async def t_sender_whitelist_direct():
    p = make_plugin(overrides={"routing.sender_whitelist": ["20000"]})
    await p._route(make_event(segs=[main.Comp.Plain("我是白名单")],
                              group_id="111", group_name="闲聊群"))
    assert len(bubbles(p)) == 1


async def t_group_blacklist_and_whitelist():
    p = make_plugin(overrides={"lists.group_blacklist": ["111"]})
    await p._route(make_event(segs=[main.Comp.Plain("x")],
                              group_id="111", group_name="黑名单群"))
    assert not p._calls and not p._buffers

    p2 = make_plugin(overrides={"lists.group_whitelist": ["222"]})
    await p2._route(make_event(segs=[main.Comp.Plain("x")],
                               group_id="111", group_name="其他群"))
    assert not p2._calls and not p2._buffers


async def t_sender_blacklist():
    p = make_plugin(overrides={"lists.sender_blacklist": ["20000"]})
    await p._route(make_event(segs=[main.Comp.Plain("x")],
                              group_id="111", group_name="闲聊群"))
    assert not p._buffers.get("111")


async def t_ignore_commands():
    p = make_plugin()
    await p._route(make_event(segs=[main.Comp.Plain("/help")],
                              group_id="111", group_name="闲聊群"))
    await p._route(make_event(segs=[main.Comp.Plain("/help")], private=True))
    assert not p._calls and not p._buffers


async def t_min_interval_throttle():
    p = make_plugin(overrides={"min_interval": 999.0})
    await p._route(make_event(segs=[main.Comp.Plain("第一条")], private=True))
    await p._route(make_event(segs=[main.Comp.Plain("第二条")], private=True))
    assert len(bubbles(p)) == 1 and bubbles(p)[0].endswith("第一条")


async def t_cooldown_then_overflow_force():
    p = make_plugin(overrides={"digest.max_buffer": 4})
    for i in range(3):  # 第一次摘要
        await p._route(make_event(segs=[main.Comp.Plain(f"m{i}")],
                                  group_id="111", group_name="闲聊群"))
    await asyncio.sleep(0.05)
    assert len(bubbles(p)) == 1

    for i in range(3):  # 冷却期内再攒 3 条（未溢出）：不触发
        await p._route(make_event(segs=[main.Comp.Plain(f"n{i}")],
                                  group_id="111", group_name="闲聊群"))
    await asyncio.sleep(0.05)
    assert len(bubbles(p)) == 1
    assert len(p._buffers["111"]) == 3        # 缓冲保留

    # 再到 1 条使缓冲达到 max_buffer：无视冷却强制摘要
    await p._route(make_event(segs=[main.Comp.Plain("overflow")],
                              group_id="111", group_name="闲聊群"))
    await asyncio.sleep(0.05)
    assert len(bubbles(p)) == 2 and "群摘要·4条" in bubbles(p)[1]
    assert not p._buffers.get("111")


async def t_digest_failure_keeps_buffer():
    p = make_plugin(tool_ok=False)
    for i in range(3):
        await p._route(make_event(segs=[main.Comp.Plain(f"m{i}")],
                                  group_id="111", group_name="闲聊群"))
    await asyncio.sleep(0.05)
    assert not bubbles(p)                     # 气泡没发出去
    assert len(p._buffers["111"]) == 3        # 缓冲保留待重试


async def t_expression_match_and_cache():
    p = make_plugin(status=EXPR_STATUS)
    await p._route(make_event(segs=[main.Comp.Plain("哈哈哈哈")], private=True))
    sets = [p_ for c, p_ in p._calls if c == "set-expression"]
    assert sets == [{"index": 2, "duration": 15}]
    await p._route(make_event(segs=[main.Comp.Plain("嘻嘻哈哈")], private=True))
    assert p._get_calls == 1                  # 30s TTL 内不再拉状态


async def t_no_expression_capability_degrades():
    p = make_plugin(status=NO_EXPR_STATUS)
    await p._route(make_event(segs=[main.Comp.Plain("哈哈哈哈")], private=True))
    assert len(bubbles(p)) == 1               # 气泡照常
    assert not [c for c, _ in p._calls if c == "set-expression"]
    assert p._expr_disabled_until > 0         # 退避生效


async def t_dashboard_down_degrades():
    # 场景一：仪表盘整个没开（/api/tool 也失败）——什么都不发生，不抛错
    p = make_plugin(tool_ok=False, status=None)
    await p._route(make_event(segs=[main.Comp.Plain("哈哈哈")], private=True))
    assert not p._calls

    # 场景二：气泡发得出去但状态读不到（表情能力未知）——气泡照发，表情退避
    p2 = make_plugin(status=None)
    await p2._route(make_event(segs=[main.Comp.Plain("哈哈哈")], private=True))
    assert len(bubbles(p2)) == 1
    assert not [c for c, _ in p2._calls if c == "set-expression"]
    assert p2._expr_disabled_until > 0


async def t_flatten_placeholders():
    p = make_plugin()
    await p._route(make_event(segs=[
        main.Comp.Plain("看这个"), main.Comp.Image(),
        main.Comp.Record(), main.Comp.Face(),
        main.Comp.At(qq="30000", name="Bob"),
    ], group_id="111", group_name="闲聊群"))
    assert "看这个 [图片] [语音] [表情] @Bob" in p._buffers["111"][0][1]


async def t_llm_digest_and_fallback():
    class GoodProvider:
        async def text_chat(self, prompt, system_prompt=None, **kwargs):
            assert kwargs.get("thinking") == {"type": "disabled"}  # 关思考参数已透传
            return SimpleNamespace(completion_text="大家在约周末聚餐 🍚")

    p = make_plugin(overrides={"digest.mode": "llm"}, provider=GoodProvider())
    for i in range(3):
        await p._route(make_event(segs=[main.Comp.Plain(f"m{i}")],
                                  group_id="111", group_name="闲聊群"))
    await asyncio.sleep(0.05)
    assert "周末聚餐" in bubbles(p)[0] and "闲聊群" in bubbles(p)[0]

    class BadProvider:
        async def text_chat(self, prompt, system_prompt=None, **kwargs):
            raise RuntimeError("provider down")

    p2 = make_plugin(overrides={"digest.mode": "llm"}, provider=BadProvider())
    for i in range(3):
        await p2._route(make_event(segs=[main.Comp.Plain(f"m{i}")],
                                   group_id="111", group_name="闲聊群"))
    await asyncio.sleep(0.05)
    assert "群摘要·3条" in bubbles(p2)[0]     # 回退 plain

    class EmptyProvider:  # 返回空文本：同样回退 plain（曾为静默路径）
        async def text_chat(self, prompt, system_prompt=None, **kwargs):
            return SimpleNamespace(completion_text="")

    p3 = make_plugin(overrides={"digest.mode": "llm"}, provider=EmptyProvider())
    for i in range(3):
        await p3._route(make_event(segs=[main.Comp.Plain(f"m{i}")],
                                   group_id="111", group_name="闲聊群"))
    await asyncio.sleep(0.05)
    assert "群摘要·3条" in bubbles(p3)[0]

    p4 = make_plugin(overrides={"digest.mode": "llm"}, provider=None)  # 无可用 provider
    for i in range(3):
        await p4._route(make_event(segs=[main.Comp.Plain(f"m{i}")],
                                   group_id="111", group_name="闲聊群"))
    await asyncio.sleep(0.05)
    assert "群摘要·3条" in bubbles(p4)[0]


async def t_manual_digest_command():
    p = make_plugin()
    for i in range(2):
        await p._route(make_event(segs=[main.Comp.Plain(f"m{i}")],
                                  group_id="111", group_name="闲聊群"))
    results = []
    async for r in p.bongocat_digest(make_event(private=True), "all"):
        results.append(r)
    assert "闲聊群" in results[0][1]
    assert len(bubbles(p)) == 1 and "群摘要·2条" in bubbles(p)[0]


async def t_status_command():
    p = make_plugin(status=EXPR_STATUS)
    results = []
    async for r in p.bongocat_status(make_event(private=True)):
        results.append(r)
    text = results[0][1]
    assert "driver=stub" in text and "表情=有" in text and "v0.2.0" in text


async def t_on_message_swallows_errors():
    p = make_plugin()

    async def boom(_event):
        raise RuntimeError("boom")

    p._route = boom
    await p.on_message(make_event(private=True))  # 不得抛出
    assert not [l for lv, m in main.logger.records if "boom" in m and lv == "info"]


async def t_group_name_fallback_to_gid():
    p = make_plugin()
    await p._route(make_event(segs=[main.Comp.Plain("x")],
                              group_id="777", group_name="N/A"))
    assert p._gname["777"] == "777"


async def t_request_real_path_no_nameerror():
    """回归：_request 曾引用 aiohttp.ClientTimeout 而未在本函数导入（NameError）。

    注入假 aiohttp 模块 + 假 session，走真实 _request 代码路径。
    """
    import types

    class _ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    fake_aiohttp = types.ModuleType("aiohttp")
    fake_aiohttp.ClientTimeout = _ClientTimeout
    fake_aiohttp.ClientSession = object
    sys.modules["aiohttp"] = fake_aiohttp

    class _Resp:
        async def json(self, content_type=None):
            return {"ok": True, "driver": "stub"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Session:
        closed = False

        def request(self, method, url, json=None, timeout=None):
            assert isinstance(timeout, _ClientTimeout) and timeout.total == 3.0
            return _Resp()

    async def fake_client():
        return _Session()

    p = make_plugin()
    p._client = fake_client
    result = await p._request("POST", "/api/tool", {"cmd": "ping", "payload": {}})
    assert result == {"ok": True, "driver": "stub"}


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("t_")]


def run() -> int:
    failures = []
    for test in TESTS:
        try:
            asyncio.run(test())
            print(f"  PASS {test.__name__}")
        except AssertionError:
            import traceback
            failures.append(test.__name__)
            print(f"  FAIL {test.__name__}")
            traceback.print_exc()
        except Exception as exc:  # noqa: BLE001
            import traceback
            failures.append(test.__name__)
            print(f"  ERROR {test.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - len(failures)}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
