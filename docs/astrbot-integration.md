# AstrBot 接入可行性方案（QQ 消息 → 猫咪气泡/表情转述）

| | |
|---|---|
| 版本 | v0.1（探索稿） |
| 日期 | 2026-08-19 |
| 状态 | 可行性已论证，待评审后进入实现 |
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
| "每来一条 QQ 消息，猫冒气泡转述 + 配表情"（本方案） | 插件 → 仪表盘 HTTP API | **规则代码**（确定性、零延迟、零 token） |
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
     ①过滤（self 回环 / 开关 / 群黑白名单 / 节流）
     ②文本化（Plain 拼接 + [图片]/[语音] 占位 + 截断）
     ③表情映射（规则表 → 关键词匹配实时表情列表，TTL 缓存）
     ④ aiohttp POST /api/tool
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
├── metadata.yaml        # name/desc/version/astrbot_version/support_platforms
├── main.py              # Star 插件主类
├── _conf_schema.json    # WebUI 可视化配置
├── requirements.txt     # 留空：只用 AstrBot 自带 aiohttp
└── README.md
```

`metadata.yaml` 关键项：`name: astrbot_plugin_bongocat`、
`support_platforms: [aiocqhttp, qq_official, telegram, ...]`（事件面是平台无关的，
列表声明"经测试的平台"）、`astrbot_version: ">=3.5,<5"`（事件 API 自 v3.5 稳定）。

### 4.2 消息处理管线（main.py 骨架）

```python
import time
import aiohttp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp

DEFAULT_EXPR_RULES = [  # 顺序即优先级，match 命中任一关键词即停
    {"match": ["哈哈", "嘿嘿", "😄", "😂", "太好了"], "expressions": ["星星眼", "开心", "高兴", "笑", "star", "happy"]},
    {"match": ["哭", "难过", "伤心", "😢", "呜"], "expressions": ["哭泣", "哭", "cry"]},
    {"match": ["？", "?", "吗", "怎么", "为什么"], "expressions": ["疑问", "疑惑", "问号", "?"]},
    {"match": ["谢谢", "感谢", "❤"], "expressions": ["爱心", "喜欢", "开心", "happy"]},
]

class BongoCatPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_relay = 0.0          # 节流游标
        self._expr_cache = (0.0, [])    # (过期时间戳, 表情列表)
        self._expr_disabled_until = 0.0 # 猫无表情能力时的退避重查时间

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            await self._relay(event)
        except Exception as e:  # 播报链路绝不影响消息管线
            logger.warning(f"bongocat 转述失败: {e}")

    async def _relay(self, event: AstrMessageEvent):
        mo = event.message_obj
        # ① 过滤：机器人自身消息（message_sent 回环）
        if str(mo.sender.user_id) == str(mo.self_id):
            return
        is_group = bool(mo.group_id)
        if is_group and not self.config["enable_group"]: return
        if not is_group and not self.config["enable_private"]: return
        if is_group and self.config["group_whitelist"] \
           and mo.group_id not in map(str, self.config["group_whitelist"]):
            return
        # 节流：窗口内直接丢弃（v1 策略：最新消息最重要）
        now = time.time()
        if now - self._last_relay < self.config["min_interval"]:
            return
        # ② 文本化
        text = self._flatten(mo.message)          # Plain 拼接 + [图片]/[语音] 占位
        if not text: return
        text = text[: self.config["max_text_len"]]
        where = "群" if is_group else "私聊"
        bubble = f"[{where}] {event.get_sender_name()}: {text}"
        # ③④ 气泡 + 表情
        self._last_relay = now
        ok = await self._tool("show-bubble",
                              {"text": bubble, "duration": self.config["bubble_duration"]})
        if ok:
            await self._maybe_expression(text)

    async def _maybe_expression(self, text: str):
        if time.time() < self._expr_disabled_until: return
        exprs = await self._expressions()          # TTL 缓存 status.modelInfo.expressions
        if exprs is None:                          # 猫/仪表盘不在线或无表情能力
            self._expr_disabled_until = time.time() + 600
            return
        for rule in DEFAULT_EXPR_RULES:
            if any(k in text for k in rule["match"]):
                for kw in rule["expressions"]:     # notify.py 同款关键词包含匹配
                    for e in exprs:
                        if kw.lower() in str(e.get("name", "")).lower():
                            await self._tool("set-expression",
                                             {"index": e["index"],
                                              "duration": self.config["expression_duration"]})
                            return
                break                              # 命中规则但皮肤没有对应表情：只出气泡

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

    async def terminate(self):
        pass
```

另注册一条运维指令（远程排障很有用）：

```python
    @filter.command("bongocat_status")
    async def status(self, event: AstrMessageEvent):
        '''查看猫控链路状态'''
        # GET /api/status 摘要 driver/capabilities/在线性，plain_result 回到 QQ
        yield event.plain_result(...)
```

### 4.3 关键策略说明

- **节流（突发合流）**：v1 用"窗口外放行、窗口内丢弃"（最新消息最重要）。
  v2 可升级为合流：窗口内到达的消息计数，下一条气泡尾缀「（另有 N 条）」。
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
  "dashboard_url":     {"type": "string",  "default": "http://127.0.0.1:8766",
                        "description": "bongocat-mcp 仪表盘地址（python dashboard.py）"},
  "enable_private":    {"type": "bool",    "default": true,  "description": "转述私聊消息"},
  "enable_group":      {"type": "bool",    "default": true,  "description": "转述群消息"},
  "group_whitelist":   {"type": "list",    "default": [],
                        "description": "群号白名单（字符串），留空=全部群"},
  "min_interval":      {"type": "float",   "default": 3.0,
                        "description": "两次播报最小间隔秒数（防消息风暴刷屏）"},
  "max_text_len":      {"type": "int",     "default": 60,   "description": "气泡最大字符数"},
  "bubble_duration":   {"type": "int",     "default": 8,    "description": "气泡停留秒数"},
  "expression_duration": {"type": "int",   "default": 15,
                        "description": "表情保持秒数（到期服务端自动回默认表情；0=保持）"},
  "relay_commands":    {"type": "bool",    "default": true,
                        "description": "是否也转述指令消息（/开头）"}
}
```

AstrBot 会据此在 WebUI 生成 `data/config/astrbot_plugin_bongocat_config.json`
并注入 `__init__`，版本升级时自动补默认值，无需迁移逻辑。

---

## 5. 部署形态与边界

| 形态 | 可行性 | 说明 |
|---|---|---|
| **AstrBot 与猫同机（Windows）** | ✅ 推荐默认 | NapCat + AstrBot + `dashboard.py` + 猫同一桌面，全部走 127.0.0.1；`enablePrivate/Group` 按需关 |
| AstrBot 跑在本机 Docker | ⚠️ 需改绑定 | `dashboard_host` 改 `0.0.0.0` + 插件指 `host.docker.internal`；**仪表盘无鉴权**，仅在可信局域网，用完改回 |
| AstrBot 在远程 Linux 服务器 | ⚠️ 仅隧道 | SSH 端口转发到本机 8766；不要公网裸露 dashboard。cdp/mver 驱动本身仅支持 Windows，猫必须留在 Windows 桌面机 |

隐私提示：气泡会把 QQ 消息内容展示在桌面——这正是需求本身（本地转述），
但配置里保留群白名单/私聊开关，让用户可控。

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

---

## 7. 实施步骤（建议）

1. **脚手架**：按 §4.1 建插件仓库，先实现 `/bongocat_status` 指令打通
   AstrBot → 仪表盘 HTTP（最小闭环，验收：QQ 里发指令能收到猫状态摘要）。
2. **消息转述**：实现管线 ①②④（气泡），手工从两个 QQ 账号发私聊/群消息验收
   （涉及真实 QQ 协议，无法离线单测，走真机清单）。
3. **表情映射**：加 ③（规则表 + 关键词匹配 + TTL 缓存 + 退避），
   分别在"有表情的 mver 皮肤 / 无表情皮肤"上验收降级行为。
4. **压力与回环**：群内连发消息验证节流；与 LLM 对话验证无回环。
5. **（可选）R1 并行开启**：WebUI 配置 MCP 服务器指向本项目 venv 的
   `python server.py`，让 LLM 也能主动控猫；验证两链路并存无异常。
6. 发布插件市场前：README（含"需先启动 dashboard.py"前置说明）、
   logo、ruff 格式化（AstrBot 贡献规范）。

验收清单（对应需求）：

- [ ] 私聊消息 → 猫气泡显示 `[私聊] 昵称: 内容`，默认 8s 消失
- [ ] 群消息 → `[群] 昵称: 内容`；白名单生效
- [ ] 含图片/语音的消息 → 占位文本，不报错不空白
- [ ] 消息含"哈哈"等 → 猫切开心系表情，15s 后自动回默认表情
- [ ] 换无表情皮肤 → 只有气泡，日志一次"无表情能力"，10 分钟退避
- [ ] 仪表盘关闭 → AstrBot 收发消息完全不受影响
- [ ] 机器人自己回复 → 不触发气泡（无回环）
- [ ] 群内 1 秒连发 10 条 → 至多 1 次气泡

---

## 8. 参考资料

- [AstrBot 插件开发指南（事件监听）](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)
- [AstrBot 插件最小实例](https://docs.astrbot.app/dev/star/guides/simple.html)
- [AstrBot 插件配置 _conf_schema.json](https://docs.astrbot.app/dev/star/guides/plugin-config.html)
- [AstrBot MCP 支持（v3.5.0+）](https://docs.astrbot.app/use/mcp.html)
- 本仓库参考实现：`zcode-plugin/bongocat-notify/hooks/notify.py`
