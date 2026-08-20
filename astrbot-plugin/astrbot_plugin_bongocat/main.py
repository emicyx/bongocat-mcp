"""astrbot_plugin_bongocat：QQ 消息 → 桌宠猫气泡/表情转述。

架构（设计文档：bongocat-mcp/docs/astrbot-integration.md）：
- 三车道路由：私聊与命中直述规则（@bot / 回复 bot / 关键词 / 白名单成员）的
  群消息逐条播报；其余群消息进入按群缓冲的摘要车道，触发条件（条数 / 静默 /
  热度 / 手动指令）满足时合成摘要播报（plain 统计拼接，或 LLM 一句话，失败
  回退 plain）。
- 对猫的一切控制经 bongocat-mcp 仪表盘 HTTP API（POST /api/tool）——插件进程
  不创建 driver，避免多出 mver 镜像线程与重复气泡（对齐 zcode-plugin 设计）。
- 表情不写死索引：实时读当前猫的表情列表按名字关键词匹配，换皮肤自动适配；
  猫无表情能力（mver 皮肤常见）时静默降级为仅气泡并退避重查。
- 全链路 try/except + 短超时：仪表盘没开 / 猫下线只记日志，绝不影响消息管线，
  也绝不 event.stop_event()（只旁路播报，不截断其他插件与 LLM）。

与 AstrBot 原生机制的关系（v4.25.1 源码实证，设计文档 §4.5）：
- 插件 filter 通过本身就是唤醒条件，原生唤醒配置管不到本插件的触发频率；
  LLM 的闸门是 is_at_or_wake_command，被动监听不会误触 LLM 回复。
- 原生 platform_settings.rate_limit / id_whitelist 是上游粗闸门（全 bot 共享），
  猫播报频率的细粒度控制由本插件 digest 配置承担；二者叠加取更严格者。
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp

# 消息情绪 → 表情关键词（按优先级，名字包含匹配当前猫的表情列表）。
# 表情集因皮肤而异，这里只写「期望语义」，匹配不到就只出气泡不切表情。
EXPR_RULES: list[dict[str, list[str]]] = [
    {"match": ["哈哈", "嘿嘿", "😄", "😂", "太好了", "好耶"],
     "expressions": ["星星眼", "开心", "高兴", "笑", "star", "happy"]},
    {"match": ["哭", "难过", "伤心", "😢", "呜"],
     "expressions": ["哭泣", "哭", "cry"]},
    {"match": ["？", "?", "吗", "怎么", "为什么"],
     "expressions": ["疑问", "疑惑", "问号", "?"]},
    {"match": ["谢谢", "感谢", "❤", "爱你"],
     "expressions": ["爱心", "喜欢", "开心", "happy"]},
]

_EXPR_TTL = 30.0          # 表情/能力缓存秒数（换皮肤后至多 30s 自适应）
_EXPR_BACKOFF = 600.0     # 猫无表情能力 / 仪表盘不可达时的退避重查秒数
_HTTP_TIMEOUT = 3.0       # 仪表盘短超时：绝不让播报链路拖慢消息管线
_DIGEST_SPACING = 1.2     # 摘要车道全局最小间隔（防多群同时触发互相覆盖气泡）


def _deep(cfg: dict, *path: str, default: Any = None) -> Any:
    """防御式读嵌套配置：Schema 升级或旧配置缺键时不崩，回落默认值。"""
    cur: Any = cfg
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


class BongoCatPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_direct = 0.0                  # 直述车道节流游标
        self._last_digest_any = 0.0              # 摘要车道全局间隔游标
        self._buffers: dict[str, list[tuple[str, str, float]]] = {}  # gid -> [(昵称, 文本, ts)]
        self._gname: dict[str, str] = {}         # gid -> 群名（消息事件里随行）
        self._last_msg: dict[str, float] = {}    # gid -> 最后一条消息时间（静默判定）
        self._last_digest: dict[str, float] = {} # gid -> 上次摘要成功时间（冷却）
        self._timers: dict[str, asyncio.Task] = {}
        self._expr_cache: tuple[float, list] = (0.0, [])  # (过期时间, 表情列表)
        self._expr_capable = True                # 猫是否具备 set-expression 能力
        self._expr_disabled_until = 0.0
        self._expr_warned = False                # 退避期内只告警一次
        self._session: Any = None                # aiohttp.ClientSession（懒创建）

    # ==================== 入口：消息路由 ====================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """被动监听所有消息事件（普通协程，无返回结果）。"""
        try:
            await self._route(event)
        except Exception as exc:  # noqa: BLE001 播报链路绝不影响消息管线
            logger.warning(f"[bongocat] 处理消息失败: {type(exc).__name__}: {exc}")

    async def _route(self, event: AstrMessageEvent) -> None:
        mo = event.message_obj
        sender = str(mo.sender.user_id)
        if sender == str(mo.self_id):        # 机器人自身消息（message_sent 回环）
            return

        group_id = str(mo.group_id or "").strip()
        if not group_id:
            if not event.is_private_chat():  # 既非群聊也非私聊（OTHER 类消息）
                return
            await self._route_private(event, sender)
            return
        await self._route_group(event, sender, group_id)

    # ---------- 私聊：直述车道 ----------

    async def _route_private(self, event: AstrMessageEvent, sender: str) -> None:
        if not _deep(self.config, "routing", "enable_private", default=True):
            return
        if sender in {str(x) for x in _deep(self.config, "lists", "private_blacklist", default=[]) or []}:
            return
        wl = {str(x) for x in _deep(self.config, "lists", "private_whitelist", default=[]) or []}
        if wl and sender not in wl:
            return
        text = self._flatten(event.message_obj.message)
        if not text:
            return
        if _deep(self.config, "routing", "ignore_commands", default=True) \
                and text.startswith("/"):
            return
        await self._relay_direct(f"[私] {event.get_sender_name()}: {text}")

    # ---------- 群聊：直述 or 摘要 ----------

    async def _route_group(self, event: AstrMessageEvent, sender: str, gid: str) -> None:
        routing = {
            "enable_group": _deep(self.config, "routing", "enable_group", default=True),
            "ignore_commands": _deep(self.config, "routing", "ignore_commands", default=True),
        }
        if not routing["enable_group"]:
            return
        lists = {
            "gb": {str(x) for x in _deep(self.config, "lists", "group_blacklist", default=[]) or []},
            "gw": {str(x) for x in _deep(self.config, "lists", "group_whitelist", default=[]) or []},
            "sb": {str(x) for x in _deep(self.config, "lists", "sender_blacklist", default=[]) or []},
            "sw": {str(x) for x in _deep(self.config, "routing", "sender_whitelist", default=[]) or []},
        }
        if gid in lists["gb"]:
            return
        if lists["gw"] and gid not in lists["gw"]:
            return
        if sender in lists["sb"]:
            return

        text = self._flatten(event.message_obj.message)
        if not text:
            return
        if routing["ignore_commands"] and text.startswith("/"):
            return

        gname = (getattr(event.message_obj.group, "group_name", "") or "").strip()
        if gname in ("", "N/A"):              # 适配器缺省值是 "N/A"（见 aiocqhttp 适配器）
            gname = gid
        self._gname[gid] = gname

        if self._hit_direct_rule(event, sender, text, lists["sw"]):
            await self._relay_direct(f"[群·{gname}] {event.get_sender_name()}: {text}")
        else:
            self._append_digest(gid, event.get_sender_name(), text)

    def _hit_direct_rule(self, event: AstrMessageEvent, sender: str,
                         text: str, sender_wl: set[str]) -> bool:
        if sender in sender_wl:
            return True
        self_id = str(event.message_obj.self_id)
        for seg in event.message_obj.message:
            if (_deep(self.config, "routing", "relay_at_bot", default=True)
                    and isinstance(seg, Comp.At)
                    and str(getattr(seg, "qq", "") or "") == self_id):
                return True
            if (_deep(self.config, "routing", "relay_reply", default=True)
                    and isinstance(seg, Comp.Reply)
                    and str(getattr(seg, "sender_id", "") or "") == self_id):
                return True
        keywords = _deep(self.config, "routing", "direct_keywords", default=[]) or []
        return any(k in text for k in keywords if str(k).strip())

    # ==================== 摘要车道 ====================

    def _flatten(self, chain: list) -> str:
        """消息链 → 纯文本：Plain 拼接，非文本段转占位（气泡只能显示文字）。"""
        parts: list[str] = []
        for seg in chain or []:
            if isinstance(seg, Comp.Plain):
                if (t := str(getattr(seg, "text", "") or "").strip()):
                    parts.append(t)
            elif isinstance(seg, Comp.Image):
                parts.append("[图片]")
            elif isinstance(seg, Comp.Record):
                parts.append("[语音]")
            elif isinstance(seg, Comp.Video):
                parts.append("[视频]")
            elif isinstance(seg, Comp.File):
                parts.append("[文件]")
            elif isinstance(seg, Comp.Face):
                parts.append("[表情]")
            elif isinstance(seg, Comp.At):
                qq = str(getattr(seg, "qq", "") or "")
                parts.append("@全体" if qq == "all"
                             else f"@{getattr(seg, 'name', None) or qq}")
            elif isinstance(seg, Comp.Reply):
                parts.append("[引用]")
            # 未知消息段忽略
        return " ".join(parts)

    def _digest_cfg(self) -> dict:
        return {
            "mode": _deep(self.config, "digest", "mode", default="plain"),
            "trigger_count": int(_deep(self.config, "digest", "trigger_count", default=15)),
            "trigger_idle_sec": float(_deep(self.config, "digest", "trigger_idle_sec", default=180)),
            "hot_window_sec": float(_deep(self.config, "digest", "hot_window_sec", default=60)),
            "hot_count": int(_deep(self.config, "digest", "hot_count", default=10)),
            "cooldown_sec": float(_deep(self.config, "digest", "cooldown_sec", default=60)),
            "max_buffer": int(_deep(self.config, "digest", "max_buffer", default=50)),
            "max_len": int(_deep(self.config, "digest", "max_len", default=100)),
            "llm_timeout_sec": float(_deep(self.config, "digest", "llm_timeout_sec", default=15)),
        }

    def _append_digest(self, gid: str, name: str, text: str) -> None:
        d = self._digest_cfg()
        now = time.time()
        buf = self._buffers.setdefault(gid, [])
        buf.append((name, text[:60], now))
        self._last_msg[gid] = now
        if len(buf) > d["max_buffer"]:        # 环形裁旧
            del buf[: len(buf) - d["max_buffer"]]

        if len(buf) >= d["trigger_count"] or self._hot(gid, d):
            overflow = len(buf) >= d["max_buffer"]
            asyncio.create_task(self._fire_digest(gid, force=overflow))
        elif not (t := self._timers.get(gid)) or t.done():
            self._timers[gid] = asyncio.create_task(
                self._idle_digest(gid, d["trigger_idle_sec"]))

    def _hot(self, gid: str, d: dict) -> bool:
        window_start = time.time() - d["hot_window_sec"]
        return sum(1 for _, _, ts in self._buffers.get(gid, [])
                   if ts >= window_start) >= d["hot_count"]

    async def _idle_digest(self, gid: str, idle: float) -> None:
        """等群真正静默 idle 秒——静默以最后一条消息起算，新消息会推迟唤醒点。"""
        task = asyncio.current_task()
        try:
            while (wait := self._last_msg.get(gid, time.time()) + idle - time.time()) > 0:
                await asyncio.sleep(min(wait, idle))
            if self._timers.get(gid) is task:
                del self._timers[gid]
            if self._buffers.get(gid):
                await self._fire_digest(gid)
        except asyncio.CancelledError:
            pass

    async def _fire_digest(self, gid: str, force: bool = False) -> None:
        buf = self._buffers.setdefault(gid, [])
        if not buf:
            return
        d = self._digest_cfg()
        if (not force and not self._hot(gid, d)
                and time.time() - self._last_digest.get(gid, 0.0) < d["cooldown_sec"]):
            return                              # 冷却中：缓冲保留，下次触发再试

        n = len(buf)                            # 只消费本次快照，期间新到的保留
        text = await self._compose_digest(gid, buf, d)
        if not await self._relay_direct(text, lane="digest", max_len=d["max_len"]):
            return                              # 气泡没发出去：缓冲保留重试
        self._last_digest[gid] = time.time()
        del buf[:n]

    async def _compose_digest(self, gid: str, buf: list, d: dict) -> str:
        gname = self._gname.get(gid, gid)
        cnt = Counter(name for name, _, _ in buf)
        top = "、".join(f"{name}×{c}" for name, c in cnt.most_common(3))
        if len(cnt) > 3:
            top += f" 等{len(cnt)}人"
        plain = f"[{gname}] 群摘要·{len(buf)}条｜{top}｜最新「{buf[-1][1][:20]}」"

        if d["mode"] != "llm":
            return plain
        try:                                     # LLM 一句话摘要，失败回退 plain
            provider = self.context.get_using_provider()
            if provider is None:
                logger.warning("[bongocat] 未获取到可用的 LLM 提供商（WebUI 未配置默认"
                               "模型？），摘要回退 plain")
                return plain
            transcript = "\n".join(f"{name}: {t}" for name, t, _ in buf[-30:])
            extra = {}
            if _deep(self.config, "digest", "llm_disable_thinking", default=True):
                # 思考型模型（kimi-k2.5 等）先推理后作答：摘要任务不需要推理，
                # 关掉可从 ~10s+ 降到 ~1.5s，且避免思考耗尽 max_tokens 导致空输出。
                # Anthropic 风格参数经 AstrBot 的 extra_body 透传；provider 不认时
                # 会报错回退 plain，届时在配置里关掉本开关即可。
                extra["thinking"] = {"type": "disabled"}
            resp = await asyncio.wait_for(provider.text_chat(
                prompt=("把下面的群聊记录浓缩成一两句口语摘要，点出主要谁在聊什么：\n"
                        f"{transcript}"),
                system_prompt="你是桌宠猫的群聊播报员，输出简短中文摘要，可带一个 emoji。",
                **extra,
            ), d["llm_timeout_sec"])
            if resp and str(getattr(resp, "completion_text", "") or "").strip():
                return f"[{gname}] {str(resp.completion_text).strip()}"
            logger.warning("[bongocat] LLM 返回了空内容，摘要回退 plain")
        except Exception as exc:  # noqa: BLE001 LLM 失败不影响播报
            logger.warning(f"[bongocat] LLM 摘要失败，回退 plain: {type(exc).__name__}: {exc}")
        return plain

    # ==================== 直述车道：节流 + 气泡 + 表情 ====================

    async def _relay_direct(self, bubble: str, lane: str = "direct",
                            max_len: int | None = None) -> bool:
        now = time.time()
        if lane == "direct":
            min_interval = float(_deep(self.config, "min_interval", default=3.0))
            if now - self._last_direct < min_interval:
                return False                    # 窗口内丢弃（最新消息最重要）
            self._last_direct = now
        else:                                   # digest 车道：独立预算，只防多群互踩
            spacing = float(_deep(self.config, "digest", "spacing",
                                  default=_DIGEST_SPACING))
            if now - self._last_digest_any < spacing:
                return False
            self._last_digest_any = now

        cap = max_len if max_len is not None else int(
            _deep(self.config, "max_text_len", default=60))
        bubble = bubble[: max(cap, 40)]

        ok = bool((r := await self._tool("show-bubble", {
            "text": bubble,
            "duration": _deep(self.config, "bubble_duration", default=8),
        })) and r.get("ok"))
        if not ok:
            return False
        await self._maybe_expression(bubble)     # 表情是增强，失败不影响气泡
        return True

    # ==================== 表情映射（能力感知 + 降级） ====================

    async def _maybe_expression(self, text: str) -> None:
        now = time.time()
        if now < self._expr_disabled_until:
            return
        exprs = await self._expressions()
        if exprs is None:                        # 猫/仪表盘离线，或无表情能力
            self._expr_disabled_until = now + _EXPR_BACKOFF
            if not self._expr_warned:
                logger.info("[bongocat] 当前猫无表情能力或仪表盘不可达，表情停用 "
                            f"{_EXPR_BACKOFF:.0f}s（气泡不受影响）")
                self._expr_warned = True
            return
        self._expr_warned = False
        for rule in EXPR_RULES:
            if not any(k in text for k in rule["match"]):
                continue
            for kw in rule["expressions"]:       # 关键词包含匹配（notify.py 同款）
                for e in exprs:
                    if kw.lower() in str(e.get("name", "")).lower():
                        await self._tool("set-expression", {
                            "index": e["index"],
                            "duration": _deep(self.config, "expression_duration", default=15),
                        })
                        return
            return                               # 命中情绪但皮肤没有对应表情：只出气泡

    async def _expressions(self) -> list | None:
        """当前猫的表情列表（30s TTL 缓存）；无表情能力/不可达返回 None。"""
        now = time.time()
        if now < self._expr_cache[0]:
            return self._expr_cache[1] if self._expr_capable else None
        status = await self._get("/api/status")
        if not isinstance(status, dict) or not status.get("ok"):
            return None
        caps = status.get("capabilities") or []
        if "set-expression" not in caps:
            self._expr_capable = False           # 该猫没有表情功能（如无素材 mver 皮肤）
            self._expr_cache = (now + _EXPR_TTL, [])
            return None
        info = ((status.get("cat") or {}).get("data") or {}).get("modelInfo") or {}
        exprs = [e for e in (info.get("expressions") or [])
                 if isinstance(e, dict) and isinstance(e.get("index"), int)]
        if not exprs:
            self._expr_capable = False
            self._expr_cache = (now + _EXPR_TTL, [])
            return None
        self._expr_capable = True
        self._expr_cache = (now + _EXPR_TTL, exprs)
        return exprs

    # ==================== 仪表盘 HTTP ====================

    async def _client(self):
        import aiohttp                          # 懒加载：AstrBot 自带，环境缺失时仅运行时报警
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict | None:
        try:
            import aiohttp                      # 懒加载：AstrBot 自带，环境缺失时仅运行时报警
            client = await self._client()
            url = str(_deep(self.config, "dashboard_url",
                            default="http://127.0.0.1:8766")).rstrip("/") + path
            async with client.request(method, url, json=body,
                                      timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)) as resp:
                data = await resp.json(content_type=None)
                return data if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001 仪表盘不可达：跳过本次播报
            logger.warning(f"[bongocat] 仪表盘不可达（跳过播报）: {type(exc).__name__}: {exc}")
            return None

    async def _tool(self, cmd: str, payload: dict) -> dict | None:
        return await self._request("POST", "/api/tool", {"cmd": cmd, "payload": payload})

    async def _get(self, path: str) -> dict | None:
        return await self._request("GET", path)

    # ==================== 运维指令 ====================

    @filter.command("bongocat_status")
    async def bongocat_status(self, event: AstrMessageEvent):
        '''查看猫控链路与本插件状态'''
        status = await self._get("/api/status")
        if not isinstance(status, dict) or not status.get("ok"):
            yield event.plain_result(
                f"[bongocat] 仪表盘不可达：{status!r}。"
                "请确认已运行 python dashboard.py")
            return
        cat = status.get("cat") or {}
        online = bool(cat.get("ok"))
        caps = status.get("capabilities") or []
        buffers = "，".join(
            f"{self._gname.get(g, g)}:{len(b)}条"
            for g, b in self._buffers.items() if b) or "空"
        mode = _deep(self.config, "digest", "mode", default="plain")
        yield event.plain_result(
            f"[bongocat] driver={status.get('driver')} 在线={online} "
            f"能力数={len(caps)}（表情={'有' if 'set-expression' in caps else '无'}） "
            f"摘要模式={mode} 待摘要缓冲：{buffers}")

    @filter.command("bongocat_digest")
    async def bongocat_digest(self, event: AstrMessageEvent, group: str = "all"):
        '''手动触发群摘要：/bongocat_digest <群号|all>'''
        targets = [g for g, b in self._buffers.items() if b and group in ("all", g)]
        for gid in targets:
            await self._fire_digest(gid, force=True)
        names = "、".join(self._gname.get(g, g) for g in targets) or "无未播报消息"
        yield event.plain_result(f"[bongocat] 已触发摘要：{names}")

    # ==================== 生命周期 ====================

    async def terminate(self):
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        if self._session is not None and not self._session.closed:
            await self._session.close()
