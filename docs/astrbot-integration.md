# AstrBot 接入可行性方案（QQ 消息 → 猫咪气泡/表情转述）

| | |
|---|---|
| 版本 | v1.0（v0.2 三道路由/群摘要，v0.3 实证原生触发/限流，v1.0 插件实现落地） |
| 日期 | 2026-08-20 |
| 状态 | **已实现**：插件位于 `astrbot-plugin/astrbot_plugin_bongocat/`（经 junction 部署到本机 AstrBot，离线测试 26/26 通过，AstrBot v4.25.1 venv 冒烟导入通过）；待真机 QQ 验收（§7） |
| 关联 | [需求书 R1](requirements.md)、[架构书 §10](architecture.md)、[ZCode 插件参考实现](../zcode-plugin/bongocat-notify/hooks/notify.py) |

---

## 0. 结论

**高度可行，且大部分困难已被本项目解决。** 推荐做一条**新的接入路径**：
写一个 AstrBot 插件（`astrbot_plugin_bongocat`），监听 AstrBot 收到的 QQ 消息，
经**仪表盘 HTTP API**（`POST /api/tool`）驱动猫的气泡与表情。

它与既有的 R1 路径（AstrBot 内置 MCP client 拉起 `server.py`，让 LLM 主动控猫）
**互补而非互斥**：

| 需求 | 路径 | 驱动方 |
|---|---|---|
| "QQ 消息按可配置路由转述：私聊/命中规则逐条气泡、群聊出摘要 + 配表情"（本方案） | 插件 → 仪表盘 HTTP API | **规则代码**（确定性、零延迟、零 token；摘要可选 LLM 润色） |
| 用户对机器人说"让猫打个招呼"（R1，UC-1） | AstrBot MCP client → `server.py` stdio | **LLM**（自然语言 → 工具调用） |

消息转述是"每条消息都要做"的高频确定性动作，交给 LLM 会引入延迟、成本与
不确定性；而交互式控猫本来就是 LLM 的主场。两条链路各自独立，可同时开启
（embedded/cdp 无冲突，mver 双镜像良性叠加，见 README 已知限制）。

---

## 1. 代码 review 摘要（与接入相关的发现）

### 1.1 现成的接入面

- **命令面唯一**：`dispatch(cmd, payload)` 是唯一入口，能力门控 / 事件日志 /
  失效重探测只写一处。仪表盘的 `POST /api/tool` 就是把它原样暴露成 HTTP——
  **外部程序不需要 MCP client 也能驱动全部 12 个命令**。
- **表情是"资产感知 + 动态解析"的**：
  - `list_expressions` / `status.modelInfo.expressions` 实时返回当前猫的表情列表
    （每项含 `index` 与人类可读 `name`），mver 皮肤的名字来自作者注释；
  - mver 皮肤没有表情素材时 capabilities 诚实不含 `set-expression`，
    `dispatch` 返回 `{"ok":false,"supported":false}` 而不是抛错；
  - `set-expression` 带 `duration>0` 时由服务端调度**自动回落到动态解析的默认表情**
    （`dispatch.py::_schedule_expression_revert`，回落目标按名字关键词
    「默认/正常/default」解析，换皮肤自动适配）。
  
  这正好覆盖了"每个版本的猫表情不一样、且猫不一定有表情功能"的全部诉求——
  **调用方只需要按名字关键词匹配，永远不写死 index**。
- **`zcode-plugin/bongocat-notify/hooks/notify.py` 是同型集成已被验证的先例**：
  外部进程（hook）→ 仪表盘 HTTP → 先气泡后表情 → 关键词匹配表情 →
  仪表盘不可达时静默降级。AstrBot 插件本质上是它的"常驻化 + 异步化"版本。

### 1.2 需要在插件侧补齐的点（server 侧没有现成机制）

| 缺口 | 说明 | 对策（插件侧） |
|---|---|---|
| 无节流/合流 | `show-bubble` 是单实例气泡，新调用直接覆盖旧的；QQ 群消息风暴会让气泡疯狂闪跳 | 插件内做 minInterval 节流 + 突发合流（见 §4.3） |
| 非文本消息 | QQ 消息可能是图片/语音/表情；气泡只能显示文字 | 消息段遍历，非文本段转 `[图片]` `[语音]` 占位（见 §4.2） |
| 回环风险 | 机器人自己发出的回复在 OneBot 侧可能以 message_sent 形式再次进入事件流 | 过滤 `sender.id == self_id`（见 §4.1） |
| 仪表盘必须在线 | HTTP 路径的前提是 `python dashboard.py` 在跑 | 静默降级 + 定期重试；v2 可自动拉起（见 §6 R-4） |
| 仪表盘无鉴权 | dashboard 只绑 127.0.0.1 且无 token，设计上就是本机控制面 | 插件与 AstrBot 同机部署；跨机只走 SSH 隧道（见 §5） |

### 1.3 review 顺带确认的设计约束

- **不要在 AstrBot 进程内 import `bongocat_mcp` 直接调 dispatch**：
  - cdp/mver 的驱动重度依赖 Win32（`win32_utils`、60fps 发送线程），
    mver 会在 AstrBot 进程里多出一条镜像线程；
  - 气泡 overlay 是 tkinter 独立线程，塞进 AstrBot 的 asyncio 进程里有风险；
  - 已知问题 K2（双进程各渲染一个气泡）会加重。
  → 一切经仪表盘 HTTP，driver 实例只存在于仪表盘进程，这是 ZCode 插件
  已经验证过的正确姿势。

---

## 2. 接入路径比较

| 路径 | 做法 | 评价 |
|---|---|---|
| **A. 插件 → 仪表盘 HTTP**（推荐） | AstrBot 插件监听消息事件，`POST /api/tool` | 零额外 driver 实例、零新依赖（aiohttp 为 AstrBot 自带）、复用全部能力门控与表情回落；notify.py 已验证同型链路 |
| B. AstrBot 内置 MCP → `server.py` | WebUI 配置 stdio MCP 服务器（v3.5.0+ 原生支持） | 适合 LLM 主动控猫（R1 原始目标），不适合确定性消息转述；与 A 并存无碍 |
| C. 插件进程内 import 包 | 直接调 `dispatch()` | 否决，理由见 §1.3 |

---

## 3. 推荐架构

```
QQ 好友/群 ──▶ NapCat / Lagrange (OneBot v11)
                  │ WebSocket
                  ▼
             AstrBot（aiocqhttp 平台适配器）
                  │ 消息事件
                  ▼
   astrbot_plugin_bongocat（本方案，Star 插件）
     ①路由：私聊/直述规则(@bot、关键词、白名单成员)→逐条气泡；
       其余群消息→按群进摘要缓冲（三车道模型，见 §4.2）
     ②过滤：黑白名单（群 / 私聊发送者 / 群内成员），黑名单优先
     ③文本化：Plain 拼接 + [图片]/[语音] 占位 + 截断；
       源标注 [私] 昵称 / [群·群名] 昵称
     ④摘要触发（可配置）：攒够 N 条 / 群静默 T 秒 / 热度突发 / 手动指令
     ⑤摘要合成：plain 统计拼接（默认）或 LLM 一句话（失败回退）
     ⑥表情映射（规则表 → 关键词匹配实时表情列表，TTL 缓存）
     ⑦ aiohttp POST /api/tool
                  │ HTTP 127.0.0.1:8766
                  ▼
        bongocat-mcp 仪表盘（常驻，持唯一 driver）
          dispatch() ── 能力门控 ── auto-revert ── 事件日志
                  │
        ┌─────────┼─────────┐
     embedded    cdp       mver   （自动探测/切换，插件完全不感知）

（并行可选）AstrBot MCP client ──stdio──▶ server.py ──▶ LLM 主动控猫（R1）
```

要点：

- 插件是**纯 HTTP 客户端**，对猫的谱系（embedded/cdp/mver）、皮肤差异、
  在线状态全部无感知——这些复杂度被仪表盘进程吸收。
- 仪表盘的事件日志（`/api/events`）天然记录插件的所有调用，排障有据（K6 范围内）。

---

## 4. 插件设计

### 4.1 目录结构

```
astrbot_plugin_bongocat/
├── metadata.yaml        # name/desc/version/repo/display_name
├── main.py              # Star 插件主类（不用 @register——v3.5.19 起已废弃，
│                        #   AstrBot 自动识别 Star 子类；被动监听器用普通协程）
├── _conf_schema.json    # WebUI 可视化配置（routing / lists / digest 三组）
├── requirements.txt     # 留空：只用 AstrBot 自带 aiohttp（懒加载导入）
└── README.md
../tests/                # 离线单元测试（stub astrbot，验证路由/摘要/降级，
                         #   python astrbot-plugin/tests/test_bongocat_plugin.py）
```

`metadata.yaml` 关键项：`name: astrbot_plugin_bongocat`；实测基线 v4.25.1
（`call_handler` 同时支持协程与异步生成器，源码 `context_utils.py:12`）。

### 4.2 消息路由与处理管线（main.py 骨架）

**三车道模型**（对应"配置转述哪些消息 / 群消息出摘要 / 黑白名单 / 摘要触发条件"）：

```
消息到达
 ├─ 回环与命令过滤 → 丢弃（机器人自身消息；/ 开头且 ignore_commands=true）
 ├─ 名单过滤（黑名单优先于白名单；白名单留空 = 放行全部）
 │    · 群整体：group_blacklist / group_whitelist
 │    · 私聊发送者：private_blacklist / private_whitelist
 │    · 群内成员：sender_blacklist（全局拉黑）/ sender_whitelist（永远直述）
 ├─ 私聊 ────────────▶【直述车道】逐条气泡（min_interval 节流）
 └─ 群聊 → 命中直述规则？（@bot / 回复 bot / direct_keywords / sender_whitelist）
          ├─ 是 ────▶【直述车道】[群·群名] 昵称: 内容
          └─ 否 ────▶【摘要车道】按群环形缓冲，触发条件（OR，全部可配置）：
                a) 攒够 trigger_count 条未播报消息
                b) 群静默 trigger_idle_sec 秒且仍有未播报消息（话题告一段落）
                c) 热度突发：hot_window_sec 秒内 ≥ hot_count 条
                d) 手动：QQ 指令 /bongocat_digest <群号|all>
             → 合成摘要（plain 统计拼接 / LLM 一句话，失败回退 plain）
             → 气泡 + 表情映射，清空缓冲，进入 cooldown_sec 冷却
```

```python
import asyncio
import time
from collections import Counter
import aiohttp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

DEFAULT_EXPR_RULES = [  # 顺序即优先级，match 命中任一关键词即停
    {"match": ["哈哈", "嘿嘿", "😄", "😂", "太好了"], "expressions": ["星星眼", "开心", "高兴", "笑", "star", "happy"]},
    {"match": ["哭", "难过", "伤心", "😢", "呜"], "expressions": ["哭泣", "哭", "cry"]},
    {"match": ["？", "?", "吗", "怎么", "为什么"], "expressions": ["疑问", "疑惑", "问号", "?"]},
    {"match": ["谢谢", "感谢", "❤"], "expressions": ["爱心", "喜欢", "开心", "happy"]},
]

@register(
    "astrbot_plugin_bongocat", "bongocat-mcp",
    "QQ 消息经桌宠猫气泡/表情转述（私聊/命中规则逐条，群聊出摘要）",
    "0.1.0", "https://github.com/EXAMPLE/astrbot_plugin_bongocat",
)
class BongoCatPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_relay = 0.0            # 直述车道节流游标
        self._buffers: dict[str, list] = {}   # group_id -> [(昵称, 文本, ts)]
        self._gname: dict[str, str] = {}      # group_id -> 群名（来自消息事件）
        self._last_msg: dict[str, float] = {} # group_id -> 最后一条消息时间（静默判定）
        self._last_digest: dict[str, float] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._expr_cache = (0.0, [])       # (过期时间戳, 表情列表)
        self._expr_disabled_until = 0.0    # 猫无表情能力时的退避重查时间

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            await self._route(event)
        except Exception as e:  # 播报链路绝不影响消息管线
            logger.warning(f"bongocat: {e}")

    # ---------- ① 路由 ----------
    async def _route(self, event: AstrMessageEvent):
        mo = event.message_obj
        sender = str(mo.sender.user_id)
        if sender == str(mo.self_id):                       # 机器人自身消息（回环）
            return
        text = self._flatten(mo.message)                    # Plain 拼接 + [图片]/[语音] 占位
        if not text:
            return
        lists, r = self.config["lists"], self.config["routing"]
        if not (group_id := mo.group_id):                   # 私聊
            if not r["enable_private"]:
                return
            if sender in set(map(str, lists["private_blacklist"])):
                return
            wl = set(map(str, lists["private_whitelist"]))
            if wl and sender not in wl:
                return
            await self._relay_direct(f"[私] {event.get_sender_name()}: {text}")
            return

        # 群聊
        gid = str(group_id)
        if gid in set(map(str, lists["group_blacklist"])):
            return
        gwl = set(map(str, lists["group_whitelist"]))
        if gwl and gid not in gwl:
            return
        if sender in set(map(str, lists["sender_blacklist"])):
            return
        if r["ignore_commands"] and text.startswith("/"):
            return
        gname = (getattr(mo.group, "group_name", "") or "").strip() or gid
        self._gname[gid] = gname
        label = f"[群·{gname}] {event.get_sender_name()}"
        if self._hit_direct_rule(event, sender, text, r):
            await self._relay_direct(f"{label}: {text}")
        else:
            self._append_digest(gid, event.get_sender_name(), text)

    def _hit_direct_rule(self, event, sender, text, r) -> bool:
        if sender in set(map(str, self.config["lists"]["sender_whitelist"])):
            return True
        if r.get("relay_at_bot", True):
            for seg in event.message_obj.message:           # @机器人 → 直述
                if isinstance(seg, Comp.At) and str(getattr(seg, "qq", "")) == \
                        str(event.message_obj.self_id):
                    return True
        return any(k in text for k in r.get("direct_keywords", []))

    # ---------- ② 摘要车道 ----------
    def _append_digest(self, gid: str, name: str, text: str):
        d, now = self.config["digest"], time.time()
        buf = self._buffers.setdefault(gid, [])
        buf.append((name, text[:60], now))
        self._last_msg[gid] = now
        if len(buf) > d["max_buffer"]:                      # 环形裁剪
            del buf[: len(buf) - d["max_buffer"]]
        if len(buf) >= d["trigger_count"] or self._hot(gid, d):
            asyncio.create_task(self._fire_digest(gid, force=len(buf) >= d["max_buffer"]))
        elif not (t := self._timers.get(gid)) or t.done():  # 静默触发定时器（每群一个）
            self._timers[gid] = asyncio.create_task(self._idle_digest(gid, d["trigger_idle_sec"]))

    def _hot(self, gid: str, d: dict) -> bool:
        window = time.time() - d["hot_window_sec"]
        return sum(1 for _, _, ts in self._buffers[gid] if ts >= window) >= d["hot_count"]

    async def _idle_digest(self, gid: str, idle: float):
        """等群真正静默 idle 秒（以最后一条消息起算，新消息会推迟唤醒）。"""
        try:
            while (wait := self._last_msg.get(gid, time.time()) + idle - time.time()) > 0:
                await asyncio.sleep(min(wait, idle))
            if self._buffers.get(gid):
                await self._fire_digest(gid)
        except asyncio.CancelledError:
            pass

    async def _fire_digest(self, gid: str, force: bool = False):
        buf = self._buffers.get(gid) or []
        if not buf:
            return
        d = self.config["digest"]
        if not force and time.time() - self._last_digest.get(gid, 0) < d["cooldown_sec"]:
            return                                           # 冷却中（缓冲溢出/手动则无视）
        self._buffers[gid] = []
        self._last_digest[gid] = time.time()
        text = await self._compose_digest(gid, buf, d)
        await self._relay_direct(text)

    async def _compose_digest(self, gid: str, buf: list, d: dict) -> str:
        gname = self._gname.get(gid, gid)
        cnt = Counter(n for n, _, _ in buf)
        top = "、".join(f"{n}×{c}" for n, c in cnt.most_common(3))
        if len(cnt) > 3:
            top += f" 等{len(cnt)}人"
        plain = f"[{gname}] 群摘要·{len(buf)}条｜{top}｜最新「{buf[-1][1][:20]}」"
        if d["mode"] != "llm":
            return plain
        try:                                                  # LLM 一句话摘要（v4.25.1 实测 API）
            provider = self.context.get_using_provider()
            transcript = "\n".join(f"{n}: {t}" for n, t, _ in buf[-30:])
            resp = await asyncio.wait_for(provider.text_chat(
                prompt=f"把群聊记录浓缩成一两句口语摘要，点出主要谁在聊什么：\n{transcript}",
                system_prompt="你是桌宠猫的群聊播报员，输出简短中文摘要，可带一个 emoji。"),
                d["llm_timeout_sec"])
            if resp and resp.completion_text:
                return f"[{gname}] {resp.completion_text.strip()}"
        except Exception as e:
            logger.warning(f"LLM 摘要失败，回退 plain: {e}")
        return plain

    # ---------- ③ 直述车道：节流 + 气泡 + 表情 ----------
    async def _relay_direct(self, bubble: str):
        now = time.time()
        if now - self._last_relay < self.config["min_interval"]:
            return                                            # 窗口内丢弃（最新消息最重要）
        self._last_relay = now
        bubble = bubble[: max(self.config["max_text_len"], 40)]
        ok = await self._tool("show-bubble",
                              {"text": bubble, "duration": self.config["bubble_duration"]})
        if ok:
            await self._maybe_expression(bubble)

    async def _maybe_expression(self, text: str):
        if time.time() < self._expr_disabled_until:
            return
        exprs = await self._expressions()                    # TTL 缓存 status.modelInfo.expressions
        if exprs is None:                                    # 猫/仪表盘不在线或无表情能力
            self._expr_disabled_until = time.time() + 600
            return
        for rule in DEFAULT_EXPR_RULES:
            if any(k in text for k in rule["match"]):
                for kw in rule["expressions"]:               # notify.py 同款关键词包含匹配
                    for e in exprs:
                        if kw.lower() in str(e.get("name", "")).lower():
                            await self._tool("set-expression",
                                             {"index": e["index"],
                                              "duration": self.config["expression_duration"]})
                            return
                break                                         # 皮肤没有对应表情：只出气泡

    async def _tool(self, cmd: str, payload: dict) -> bool:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{self.config['dashboard_url'].rstrip('/')}/api/tool",
                                  json={"cmd": cmd, "payload": payload},
                                  timeout=aiohttp.ClientTimeout(total=3)) as r:
                    data = await r.json()
                    return bool(data.get("ok"))
        except Exception as e:
            logger.warning(f"仪表盘不可达（bongocat 播报跳过）: {e}")
            return False

    # ---------- 运维指令 ----------
    @filter.command("bongocat_digest")
    async def digest_now(self, event: AstrMessageEvent, group: str = "all"):
        '''手动触发群摘要：/bongocat_digest <群号|all>'''
        targets = [gid for gid, buf in self._buffers.items() if buf and group in ("all", gid)]
        for gid in targets:
            await self._fire_digest(gid, force=True)
        yield event.plain_result(f"已触发摘要: {targets or '无未播报消息'}")

    @filter.command("bongocat_status")
    async def status(self, event: AstrMessageEvent):
        '''查看猫控链路状态（driver / 能力 / 在线性 / 各群缓冲条数）'''
        # GET /api/status 摘要 + dict(self._buffers) 计数，plain_result 回到 QQ
        yield event.plain_result(...)

    async def terminate(self):
        for t in self._timers.values():
            t.cancel()
```

> 骨架省略 `_flatten`（消息链 → 纯文本 + 占位）与 `_expressions`（表情列表 TTL
> 缓存）的实现，语义见 §4.3。

### 4.3 关键策略说明

**路由与名单**

- **名单优先级：黑名单 > 白名单**；任何名单留空即"不启用该名单"（放行全部）。
  群名单按群号、私聊名单按发送者 QQ、成员名单跨所有群生效。
- **直述规则命中顺序**：sender_whitelist → @bot（`relay_at_bot`）→
  `direct_keywords`（可填自己的名字/外号，别人提你时立即播报）。
  回复（reply）机器人消息的检测作为 v1.1 增强，v1 先不做。
- **源标注格式**：私聊 `[私] 昵称: ...`；群直述 `[群·群名] 昵称: ...`；
  群摘要 `[群名] 群摘要·N条｜发言统计｜最新一条`。群名取
  `event.message_obj.group.group_name`（aiocqhttp 适配器从 NapCat 上报透传，
  v4.25.1 `aiocqhttp_platform_adapter.py:218` 实测存在），缺失时回退显示群号。

**摘要车道语义**

- **触发是 OR 组合**，四个条件任一满足即出摘要；触发后缓冲清空并进入
  `cooldown_sec` 冷却——但**缓冲达到 `max_buffer` 时无视冷却强制触发**，
  防止消息洪峰时摘要被无限推迟。
- **"静默"以最后一条消息起算**：定时器不是简单的 sleep(idle)——新消息会
  推迟唤醒点，真正静默 idle 秒才触发（骨架 `_idle_digest` 的 while 循环）。
- **plain 摘要零成本**：统计 top 发言者 + 最新一条片段，不调 LLM。
- **llm 摘要只在触发时调用**（一次摘要一次调用，非逐条消费 token）：
  `self.context.get_using_provider().text_chat(prompt, system_prompt)`
  （v4.25.1 `provider.py:96` 实测签名），`asyncio.wait_for` 超时/异常一律
  回退 plain。摘要文本同样过表情规则——LLM 摘要自带的 emoji 会自然命中表情映射。

**播报与降级（沿用 v0.1 策略）**

- **直述车道节流**：`min_interval` 窗口外放行、窗口内丢弃（最新消息最重要）。
  v2 可升级为"窗口内计数，下条气泡尾缀（另有 N 条）"。
- **表情缓存**：`status.modelInfo.expressions` 以 30s TTL 缓存，按
  `(模型名, 列表长度)` 指纹失效——换皮肤/换猫后 30s 内自适应，与 ZCode 插件策略一致。
- **无表情猫退避**：表达式列表为空或 `set-expression` 返回 `supported:false` 时，
  10 分钟内不再尝试（记一次日志），气泡照常。`supported:false` 不视为错误。
- **绝不 `event.stop_event()`**：插件只旁路播报，不得截断其他插件与 LLM 的处理。
- **全链路 try/except + 短超时（3s）**：仪表盘没开、猫下线、配置坏了，
  都只 `logger.warning`，不影响 AstrBot 收发消息（对齐 notify.py 的静默降级原则）。
- **异步 HTTP**：遵守 AstrBot 开发规范"不要用 requests，用 aiohttp/httpx"，
  不得在 handler 里用 urllib 阻塞事件循环。

### 4.4 配置（`_conf_schema.json`）

```json
{
  "dashboard_url": {"type": "string", "default": "http://127.0.0.1:8766",
                    "description": "bongocat-mcp 仪表盘地址（python dashboard.py）"},
  "routing": {
    "type": "object", "description": "消息路由：哪些消息逐条直述",
    "items": {
      "enable_private":   {"type": "bool", "default": true, "description": "私聊逐条转述"},
      "enable_group":     {"type": "bool", "default": true,
                           "description": "启用群消息（命中直述规则的逐条，其余进摘要）"},
      "ignore_commands":  {"type": "bool", "default": true,
                           "description": "丢弃以唤醒前缀(/)开头的消息"},
      "relay_at_bot":     {"type": "bool", "default": true,
                           "description": "@机器人 的群消息逐条直述"},
      "direct_keywords":  {"type": "list", "default": [],
                           "description": "命中任一关键词的群消息直述（如自己的名字）"},
      "sender_whitelist": {"type": "list", "default": [],
                           "description": "群内永远直述的成员 QQ（跨所有群）"}
    }
  },
  "lists": {
    "type": "object", "description": "黑白名单（黑名单优先；白名单留空=放行全部）",
    "items": {
      "group_blacklist":   {"type": "list", "default": [], "description": "不转述的群号"},
      "group_whitelist":   {"type": "list", "default": [], "description": "只转述这些群号"},
      "private_blacklist": {"type": "list", "default": [], "description": "不转述的私聊 QQ"},
      "private_whitelist": {"type": "list", "default": [], "description": "只转述这些私聊 QQ"},
      "sender_blacklist":  {"type": "list", "default": [],
                            "description": "所有群都忽略的成员 QQ"}
    }
  },
  "digest": {
    "type": "object", "description": "群摘要：什么时候出摘要、摘要怎么写",
    "items": {
      "mode":             {"type": "string", "options": ["plain", "llm"], "default": "plain",
                           "description": "plain=规则拼接（零成本）；llm=LLM 一句话摘要（失败自动回退 plain）"},
      "trigger_count":    {"type": "int", "default": 15,
                           "description": "攒够 N 条未播报消息即出摘要"},
      "trigger_idle_sec": {"type": "int", "default": 180,
                           "description": "群静默 N 秒且仍有未播报消息即出摘要"},
      "hot_window_sec":   {"type": "int", "default": 60, "description": "热度统计窗口（秒）"},
      "hot_count":        {"type": "int", "default": 10,
                           "description": "窗口内达到 N 条立即出摘要"},
      "cooldown_sec":     {"type": "int", "default": 60,
                           "description": "两次摘要最小间隔（缓冲溢出时无视）"},
      "max_buffer":       {"type": "int", "default": 50,
                           "description": "每群缓冲上限（超出裁旧）"},
      "llm_timeout_sec":  {"type": "int", "default": 45,
                           "description": "LLM 摘要超时秒数（超时回退 plain；思考型模型如 kimi-k2.5 建议 ≥45）"}
    }
  },
  "min_interval":        {"type": "float", "default": 3.0,
                          "description": "直述车道两次气泡最小间隔秒数"},
  "max_text_len":        {"type": "int", "default": 60, "description": "单条气泡最大字符数"},
  "bubble_duration":     {"type": "int", "default": 8, "description": "气泡停留秒数"},
  "expression_duration": {"type": "int", "default": 15,
                          "description": "表情保持秒数（到期服务端自动回默认；0=保持）"}
}
```

AstrBot 会据此在 WebUI 生成 `data/config/astrbot_plugin_bongocat_config.json`
并注入 `__init__`，版本升级时自动补默认值，无需迁移逻辑。

### 4.5 与 AstrBot 原生消息机制的关系（v4.25.1 源码实证）

"减少群聊消息触发频率"确实有原生配置，但它们的作用面与本插件不同。以下结论
全部来自本机 AstrBot v4.25.1 源码（`astrbot/core/pipeline/`）。

**管线顺序**（`stage_order.py`）——插件 handler 在第 6 站，前面有四道原生闸门：

```
WakingCheckStage → WhitelistCheckStage → SessionStatusCheckStage → RateLimitStage
      → ContentSafety → PreProcess → ProcessStage（插件 handler / LLM）→ ...
```

**逐条实证**：

| 原生机制 | 配置键（WebUI） | 对本插件的意义 |
|---|---|---|
| **唤醒检查** | `wake_prefix`、@、回复、私聊自动醒 | ⚠️ **管不到插件**：插件 filter 通过本身就是唤醒条件（`WakingCheckStage` 遍历 handler filter，通过即 `is_wake=True` 并放行）。`event_message_type(ALL)` 意味着每条消息都进管线——**无法用唤醒配置减少插件的触发频率**。但这不误触 LLM：LLM 的闸门是 `is_at_or_wake_command`（@/前缀/私聊才置位，插件 filter 唤醒不置位，`process_stage/stage.py:58`），被动监听安全。且本机已装的 `group_context_flow` 已在用 `event_message_type(GROUP_MESSAGE)` 监听全部群消息——"全量进管线"是现状，本插件不改变它 |
| **限流** | `platform_settings.rate_limit` = `{time:60, count:30, strategy:"stall"\|"discard"}`（本机当前值） | ✅ **这就是原生的频率闸门**：`RateLimitStage` 在插件之前，按 `session_id`（群=群号）固定窗口计数。`stall`=超额消息延迟到下窗口；`discard`=直接丢弃；`count:0`=关闭。**注意它是全 bot 级的**（同时约束该群所有插件与机器人回复），不是插件专属。对摘要车道的交互：`discard` 会让我们"攒够 N 条"的计数只基于限流后的消息（低估活跃度）；`stall` 只延迟不丢，内容完整但触发稍晚。**建议保持 stall 或调大 count，把"猫播报频率"的控制交给插件自己的 digest 配置** |
| **会话白名单** | `platform_settings.id_whitelist`（`enable_id_white_list`） | 在插件之前生效：不在名单的群/私聊直接终止，插件收不到——即**原生白名单自动对本插件生效**（当前本机名单为空=不检查）。但它是白名单语义且与"机器人是否回答"共享：想让机器人答群 A 但猫不播报群 A，原生做不到 → **插件自己的 `lists` 仍必要**（黑名单、成员级、独立开关），二者取交集 |
| **自消息忽略** | `platform_settings.ignore_bot_self_message`（本机当前 False） | 开启后机器人自身消息在唤醒阶段即被终止。本插件**无论开关都保留自己的 sender==self_id 过滤**（纵深防御）；建议保持现状即可 |
| **@全体忽略** | `platform_settings.ignore_at_all` | 只影响唤醒语义（@全体是否算唤醒），与插件触发无关 |

**结论**：原生的 `rate_limit` 与 `id_whitelist` 是插件的**上游粗闸门**（全 bot 共享），
猫播报频率的**细粒度控制由插件 digest 配置承担**（触发条数/静默/热度/冷却）；
两者叠加时以更严格者为准。这一分工写进插件 README，避免用户误调
`rate_limit` 来控制猫的播报频率（会连带限制机器人本身）。

---

## 5. 部署形态与边界

| 形态 | 可行性 | 说明 |
|---|---|---|
| **AstrBot 与猫同机（Windows）** | ✅ 推荐默认 | NapCat + AstrBot + `dashboard.py` + 猫同一桌面，全部走 127.0.0.1；`enablePrivate/Group` 按需关 |
| AstrBot 跑在本机 Docker | ⚠️ 需改绑定 | `dashboard_host` 改 `0.0.0.0` + 插件指 `host.docker.internal`；**仪表盘无鉴权**，仅在可信局域网，用完改回 |
| AstrBot 在远程 Linux 服务器 | ⚠️ 仅隧道 | SSH 端口转发到本机 8766；不要公网裸露 dashboard。cdp/mver 驱动本身仅支持 Windows，猫必须留在 Windows 桌面机 |

隐私提示：气泡会把 QQ 消息内容展示在桌面——这正是需求本身（本地转述），
但配置里保留群白名单/私聊开关，让用户可控。

### 5.1 部署基线（实测，2026-08-19）

开发者本机即"同机"形态，方案的所有前提直接满足。实测基线（下文
`<ASTRBOT_ROOT>` 指 AstrBot 源码根目录，按实际安装位置替换）：

| 项 | 实测值 |
|---|---|
| AstrBot | **v4.25.1**（源码部署，AstrBotLauncher 0.3.0 管理，`<ASTRBOT_ROOT>`） |
| Python | 3.12（`.python-version`），`aiohttp>=3.11.18` 在 requirements 内 ✅ |
| 平台适配器 | `aiocqhttp`，反向 WS 监听 `0.0.0.0:6199`（NapCat 主动连 AstrBot）；NapCat Shell 挂接同目录 QQ.exe |
| LLM / 唤醒前缀 | moonshot kimi-k2.5（思考型模型，摘要调用需关思考，见 §7）/ `/` |
| 插件目录 | `data/plugins/`（已有市场插件验证 `Star 子类` + `AstrBotConfig` + `_conf_schema.json` 模式与 §4 骨架一致） |
| AstrBot MCP | `data/mcp_server.json` 存在但 `mcpServers` 为空——MCP 路径（§2 路径 B）尚未开启，加一项配置即可 |
| 猫控面 | 本项目仪表盘 `127.0.0.1:8766`，与 AstrBot 同机互通 ✅ |

插件安装（本地开发态）：

1. 把插件目录复制（或建目录连接）到
   `<ASTRBOT_ROOT>\data\plugins\astrbot_plugin_bongocat\`；
2. WebUI → 插件管理 → 重载（或重启 AstrBot），配置面板会出现 §4.4 的表单项
   （落盘为 `data/config/astrbot_plugin_bongocat_config.json`）；
3. 保持 `python dashboard.py` 在跑；QQ 里发 `/bongocat_status` 验证链路。
   NapCat 侧无需任何改动（插件只消费 AstrBot 事件，不碰 OneBot 连接）。

---

## 6. 风险登记与对策

| # | 风险 | 概率/影响 | 对策 |
|---|---|---|---|
| R-1 | 机器人自身回复被当作新消息（回环） | 中/低 | 过滤 `sender.user_id == self_id`；上线后用 `/bongocat_status` 验证 |
| R-2 | 群消息风暴刷气泡 | 高/中 | minInterval 节流；v2 合流计数 |
| R-3 | 当前猫/皮肤无表情能力 | 高（mver 常见）/低 | 能力门控已内建（`supported:false`），插件退避 + 气泡-only |
| R-4 | 仪表盘未启动 | 中/中 | 静默降级 + warning 日志；v2 增加 `auto_start_dashboard`（`CREATE_NO_WINDOW` 拉起 `dashboard.py`） |
| R-5 | 双进程气泡（K2）叠加 | 低/低 | 插件只走仪表盘不另起进程，已是受控形态；根治等 R5 单守护进程 |
| R-6 | AstrBot 大版本 API 变更 | 低/中 | `metadata.yaml` 锁 `astrbot_version` 区间；插件只用稳定的事件/配置面 |
| R-7 | NapCat 风控/协议变动 | 外部依赖 | 与本方案无关（AstrBot 生态自身课题），不纳入 |
| R-8 | LLM 摘要延迟/费用/失败 | 中/中 | 默认 plain；llm 仅在摘要触发时调用（非逐条）；超时/异常回退 plain |
| R-9 | 个别 OneBot 实现群名字段缺失 | 低/低 | `group.group_name or 群号` 回退（v4.25.1 实测 NapCat 上报带群名） |
| R-10 | 摘要缓冲内存 / 定时器泄漏 | 低/低 | 每群 `max_buffer` 裁旧；`terminate` 统一 cancel 定时器 |
| R-11 | 原生 `rate_limit=discard` 使摘要计数低估活跃度 | 低/低 | 建议 stall 或调大 count（见 §4.5）；插件 README 说明二者分工 |

---

## 7. 实现落地记录（v1.0）

插件实现于 `astrbot-plugin/astrbot_plugin_bongocat/`，与 v0.3 方案相比的
**实现期决策与修正**（全部经离线测试验证）：

1. **摘要车道独立预算**（修正方案骨架的设计缺陷）：骨架里 `_fire_digest` 复用
   `_relay_direct`，会被直述车道的 `min_interval` 吞掉。实现改为 digest 车道
   独立游标，仅保留 `digest.spacing`（默认 1.2s，防多群同时触发互相覆盖气泡）。
2. **摘要失败不消费缓冲**：气泡发送成功后才 `del buf[:n]`（只消费本次快照，
   期间新到的消息保留）；仪表盘掉线时缓冲保留，恢复后下次触发重试。
3. **`ignore_commands` 同时作用于私聊车道**（测试发现骨架只在群聊检查）。
4. **OTHER 类消息忽略**：既非群聊也非私聊的事件（`group_id` 空且
   `is_private_chat()` 假）直接丢弃。
5. **回复 bot 也直述**：`Comp.Reply.sender_id == self_id`（v4.25.1 唤醒阶段
   同款判定），配置 `routing.relay_reply` 可关。
6. **不用 `@register` 装饰器**：源码标注 DEPRECATED（v3.5.19+），Star 子类
   自动识别；被动监听器是普通 `async def`（`call_handler` 兼容两种形态）。
7. **aiohttp 懒加载**：`_client()` 内 import——依赖缺失时导入插件不炸，
   仅运行时报警（AstrBot 必带 aiohttp，防御性设计）。
8. **配置防御式读取**：`_deep()` 逐级取键带默认值，Schema 升级/旧配置
   缺键不崩。
9. **群名回退链**：`group.group_name` 缺失或为适配器缺省值 `"N/A"` 时回退群号
   （aiocqhttp 适配器 `event.get("group_name", "N/A")`，`aiocqhttp_platform_adapter.py:219`）。

**真机联调发现并修复（2026-08-20）**：

10. **aiohttp 懒加载作用域 bug**：`_request()` 引用 `aiohttp.ClientTimeout`
    但 import 写在 `_client()` 里（函数内 import 不跨作用域）→ 每次调用
    `NameError`。修复后补回归测试直接执行真实 `_request` 路径（此前测试桩掉了
    `_tool/_get`，该路径从未被执行——教训：HTTP 层必须有非桩用例）。
11. **思考型模型的 LLM 摘要超时/空输出**：kimi-k2.5 默认先推理后作答——实测
    开思考时小任务 9.3s 且 `finish_reason=length`（推理耗尽 max_tokens、
    可见输出为空），15s 超时必现；关思考后 1.4s 正常输出。修复：摘要调用
    默认透传 `thinking: {"type": "disabled"}`（经 AstrBot text_chat kwargs →
    `extra_body` 链，v4.25.1 源码 `_query` 实证），配置
    `digest.llm_disable_thinking` 可关；`llm_timeout_sec` 默认 15→45；
    三条回退路径（无 provider / 空输出 / 异常）全部记日志。

**验证状态**：

- 离线单元测试 27/27 通过（stub astrbot 模块；覆盖三车道路由、名单、
  摘要四类触发与冷却、溢出强制、失败保留缓冲、表情能力感知与退避、
  LLM 摘要及回退、真实 `_request` 路径、占位文本、指令、异常吞噬）。
- AstrBot v4.25.1 venv（Python 3.12.3）真实代码冒烟导入 + 对活仪表盘的
  端到端 HTTP（ping/status/表情列表）通过。
- **真机已验证**（2026-08-20）：私聊逐条转述、群摘要触发与输出、
  LLM 摘要（kimi 关思考后秒级出结果）、表情列表读取。
- 部署方式：`data/plugins/astrbot_plugin_bongocat` 指向仓库源码的
  目录连接（junction）——改完代码在 WebUI 插件管理"重载"即生效，无需复制。
- 待验（需真机两个 QQ 账号补完）：§8 清单其余项（@bot 直述、黑白名单、
  静默/热度触发、回环、仪表盘关闭不影响收发等）。

## 8. 真机验收步骤与清单

> 1-2 步（脚手架/直述/摘要/表情的核心逻辑）已由离线测试覆盖并落地；
> 真机部分从第 3 步开始。

1. **脚手架**：按 §4.1 建插件仓库，先实现 `/bongocat_status` 指令打通
   AstrBot → 仪表盘 HTTP（最小闭环，验收：QQ 里发指令能收到猫状态摘要）。
2. **直述车道**：私聊 + 群直述规则（@bot / 关键词 / 成员白名单）+ 名单过滤
   + 源标注（含群名），手工从两个 QQ 账号发私聊/群消息验收
   （涉及真实 QQ 协议，无法离线单测，走真机清单）。
3. **摘要车道**：按群缓冲 + 四类触发（条数/静默/热度/手动）+ plain 合成；
   通过 `/bongocat_digest` 手动验证，再在真实群观察三种自动触发。
4. **表情映射**：规则表 + 关键词匹配 + TTL 缓存 + 退避，
   分别在"有表情的 mver 皮肤 / 无表情皮肤"上验收降级行为；
   digest 开 llm 模式验收一句话摘要与回退。
5. **压力与回环**：群内连发消息验证节流与摘要冷却；与 LLM 对话验证无回环。
6. **（可选）R1 并行开启**：WebUI（或 `data/mcp_server.json`，部署基线为空配置）
   添加 stdio MCP 服务器，命令指向本项目 venv 的
   `<仓库根>\.venv\Scripts\python.exe`、参数 `<仓库根>\server.py`，
   让 LLM 也能主动控猫；验证两链路并存无异常。
7. 发布插件市场前：README（含"需先启动 dashboard.py"前置说明）、
   logo、ruff 格式化（AstrBot 贡献规范）。

验收清单（对应需求）：

**直述车道**
- [ ] 私聊消息 → `[私] 昵称: 内容`，默认 8s 消失
- [ ] 群内 @bot / 命中 direct_keywords → `[群·群名] 昵称: 内容` 直述
- [ ] 黑名单群/成员零气泡；白名单只放行配置对象（群/私聊分别验证）

**摘要车道**
- [ ] 普通群消息不逐条气泡；攒够 trigger_count 条出一条摘要（含 top 发言者统计与最新一条）
- [ ] 群静默 trigger_idle_sec 秒 → 出摘要（新消息会推迟静默判定）
- [ ] 热度突发（hot_window_sec 内 hot_count 条）→ 立即摘要
- [ ] 两次摘要间隔 ≥ cooldown_sec；缓冲溢出时无视冷却强制摘要
- [ ] `/bongocat_digest <群号|all>` 手动触发成功
- [ ] llm 模式输出一句话摘要；provider 失败/超时回退 plain 不报错

**通用**
- [ ] 含图片/语音的消息 → 占位文本，不报错不空白
- [ ] 消息含"哈哈"等 → 猫切开心系表情，15s 后自动回默认表情（摘要文本同理）
- [ ] 换无表情皮肤 → 只有气泡，日志一次"无表情能力"，10 分钟退避
- [ ] 仪表盘关闭 → AstrBot 收发消息完全不受影响
- [ ] 机器人自己回复 → 不触发气泡（无回环）
- [ ] 群内 1 秒连发 10 条 → 不刷屏（进摘要车道）

---

## 9. 参考资料

- [AstrBot 插件开发指南（事件监听）](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)
- [AstrBot 插件最小实例](https://docs.astrbot.app/dev/star/guides/simple.html)
- [AstrBot 插件配置 _conf_schema.json](https://docs.astrbot.app/dev/star/guides/plugin-config.html)
- [AstrBot MCP 支持（v3.5.0+）](https://docs.astrbot.app/use/mcp.html)
- 本仓库参考实现：`zcode-plugin/bongocat-notify/hooks/notify.py`
