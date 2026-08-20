# bongocat-mcp 需求书

| | |
|---|---|
| 版本 | v1.1 |
| 日期 | 2026-08-19 |
| 状态 | 已实现（本文同时作为后续演进的基线） |
| 关联文档 | [架构实现书](architecture.md)、[README](../README.md) |

---

## 1. 背景与问题

BongoCat 是流行的"键盘猫"桌宠：猫趴在屏幕底部的虚拟键盘上，实时跟随用户的键鼠输入做按键动画。围绕它形成了**三个互不兼容的版本谱系（lineage）**：

| 谱系 | 形态 | 控制能力 |
|---|---|---|
| 官方 Tauri 版（release） | 前端 Vue + Live2D，rdev 全局键鼠监听 | 仅被动跟随，无对外控制 API |
| 自编译版 | 官方仓库自行编译，内置 MCP/HTTP 控制通道 | 本地 HTTP API（端口 + Bearer token 动态发现） |
| BongoCatMver 系（皮肤版） | C++/SFML 重写，靠手改 `img/` + `config.json` 换皮肤 | 网络同步（UDP 收发键鼠状态帧），无语义 API |

由此产生三个问题：

1. **AI agent 无法主动操作桌宠。** 现有猫只有"被动跟随键鼠"一种行为；聊天机器人（astrbot 等 MCP client）说不出"让猫打个字 / 变个表情 / 冒个气泡"。
2. **版本碎片化。** 三种猫的控制方式完全不同，任何上层应用都要为每种猫写一套适配。
3. **调试困难。** 没有可视化工具查看"当前连的是哪只猫、支持什么能力、命令调用结果如何"。

## 2. 产品目标与成功指标

**一句话目标**：做一个与 BongoCat 上游仓库完全解耦的独立控制器，把三种猫统一封装成 MCP tools，供 LLM agent 调用；附带本地 Web 仪表盘供人查看与调试。

**设计原则**（约束所有需求）：

- **黑盒控制**：不 fork、不重编译、不修改任何猫的程序本体；只使用猫自身暴露的通道（HTTP / 调试端口 / UDP），或与其设置界面写同一份配置文件。
- **统一命令面**：上层（MCP 工具 / 仪表盘）只看到一套命令；三种猫的差异全部吸收在 driver 层。
- **诚实降级**：不支持的命令明确报不支持（能力门控），而不是静默失败或假装成功。

**成功指标**：

| 指标 | 目标 |
|---|---|
| agent 单指令生效 | 一句自然语言 → 猫完成打字/表情/气泡动作，端到端 < 2s（不含 LLM 推理） |
| 新 Mver 猫接入成本 | ≤ 1 次点击（一键接入）或 0 次点击（自动识别，≤ 5s） |
| 仪表盘调试闭环 | 状态、能力、配置、全部命令试玩、事件日志均在同一页面完成 |
| 回归测试 | `test_client.py` 全工具通过（不支持项按能力降级记 SKIP，不记失败） |

## 3. 术语

| 术语 | 含义 |
|---|---|
| 猫 / lineage | 一个可运行的 BongoCat 桌宠实例（三种谱系之一） |
| driver | 本项目中针对一种谱系的适配器，实现统一的 `CatDriver` 接口 |
| embedded | 自编译版 BongoCat 的 driver（内置 HTTP 控制通道） |
| cdp | Tauri 系成品（官方 release / 只换模型资源的重打包版）的 driver，经 WebView2 调试端口注入 |
| mver | BongoCatMver 系皮肤版的 driver，使用逆向得到的 UDP 协议 |
| 镜像层 | mver 专用：以 60fps 读取本机真实键鼠并转发给接收模式的猫，使其行为与本机模式一致 |
| 覆盖层 | mver 专用：AI 指令产生的虚拟按键状态，合成帧时优先于真实键鼠 |
| 能力门控 | 命令调度前检查当前 driver 是否支持该命令，不支持则返回结构化的 `supported:false` |
| 接收模式 | Mver 的网络同步配置 `network:true, is_sender:false`，忽略本机键鼠、只渲染 UDP 帧 |
| MCP | Model Context Protocol，LLM agent 的工具调用协议（本项目的 server 以 stdio 方式提供） |
| astrbot | 聊天机器人框架，典型的 MCP client，Roadmap 中的一等公民接入目标 |

## 4. 用户与使用场景

### 4.1 用户角色

| 角色 | 描述 | 主要诉求 |
|---|---|---|
| U1 桌宠主人 | 同时使用聊天机器人和 BongoCat 的终端用户 | 聊天中用自然语言指挥猫做动作，猫给出可见反馈 |
| U2 直播主 | 用猫做 OBS 按键可视化的主播 | 手动摆拍（set_hand）、气泡读内容，稳定不抢焦点 |
| U3 开发者 | 本项目 / 猫皮肤的维护者 | 排障：看状态、改配置、试命令、看日志 |

### 4.2 核心用例

- **UC-1 聊天驱猫**：用户对 astrbot 说"让猫打个招呼" → agent 依次调 `type_text("hello")` + `show_bubble("喵～")` → 猫逐字打字并在头顶冒出气泡。
- **UC-2 表情切换**：用户说"让猫开心一点" → agent 先 `list_expressions` 拿到列表，再 `set_expression(index)`。
- **UC-3 直播摆拍**：主播在仪表盘试玩台点"左手按下"，或 agent 调 `set_hand(left=true)` 摆出按压姿势截图。
- **UC-4 开发排障**：开发者打开仪表盘，看到 driver=mver、能力矩阵绿色 8 项，点击 `ping` 返回 ok，事件日志实时滚动确认链路在线。
- **UC-5 新猫接入**：用户安装了一个新皮肤版 Mver 并启动 → 仪表盘 5 秒内自动识别并切换（或点"一键接入"自动开网络接收 + 重启猫）。
- **UC-6 无 AI 运行**：用户只想让接收模式的猫正常跟手 → 单独运行 `mver-mirror.py` 恢复键鼠跟随，不启动 MCP。
- **UC-7 猫被重启后自愈**：agent 调用命令时猫恰好被关闭/重启 → 调度层捕获连接失效，自动重探测一次后重发命令。

## 5. 功能需求

优先级：P0 = 必须且有回归验证；P1 = 必须；P2 = 应该有。

### FR-1 多驱动统一控制（P0）

系统支持三种 driver；解析顺序为**配置强制指定 > 自动探测**；自动探测顺序为 embedded → cdp → mver。driver 可在运行中切换（切换即持久化到 config.json 并重建实例）。

**验收**：三种猫各自在线时 `ping` 返回 ok；全部不在线时返回带三条指引的错误信息；仪表盘切换 driver 后 2 秒内状态栏反映新 driver。

### FR-2 统一命令面（P0）

对外暴露 **14 个 MCP 工具**，映射到 **12 个统一命令**（与自编译版 HTTP 接口同名），所有 driver 行为一致：

| MCP 工具 | 底层命令 | 说明 | embedded | cdp | mver |
|---|---|---|---|---|---|
| `ping` | ping | 链路健康检查 | ✅ | ✅ | ✅ |
| `get_cat_status` | status | driver / 能力 / 模型信息 / 窗口可见性 | ✅ | ✅ | ✅ |
| `list_expressions` | status | 表情列表（含 index 与名称） | ✅ | ✅ | ⚠️ |
| `list_motions` | status | 动作列表（分组/键位） | ✅ | ✅ | ⚠️ |
| `set_expression(index)` | set-expression | 切换表情 | ✅ | ✅ | ⚠️ |
| `play_motion(motion)` | play-motion | 播放动作 | ✅ | ✅ | ⚠️ |
| `press_key(key)` / `release_key(key)` | press-key / release-key | 按键/松键动画 | ✅ | ✅ | ✅ |
| `type_text(text)` | type-text | 逐字符打字动画 | ✅ | ✅ | ✅ |
| `set_hand(left, right)` | set-hand | 猫爪下压 | ✅ | ❌ | ❌ |
| `set_parameter(id, value)` | set-parameter | Live2D 参数 | ✅ | ❌ | ❌ |
| `show_bubble(text)` / `hide_bubble()` | show-bubble / hide-bubble | 聊天气泡 | ✅ | ✅ | ✅ |
| `set_window_visible(visible)` | set-window-visible | 猫窗口显隐 | ✅ | ✅ | ✅ |

⚠️ = 能力资产感知，见 FR-3。key 命名形如 `KeyA` / `Num1` / `Space`（rdev 风格）。

**验收**：`test_client.py` 对每种 driver 跑全工具矩阵，结果只有 OK / SKIP（能力降级）两种，无 FAIL。

### FR-3 能力门控与资产感知（P0）

- 调度前检查 `driver.capabilities()`；不支持时返回 `{"ok":false, "supported":false, ...}`，不产生副作用。
- mver 的表情/动作能力是**资产感知**的：仅当皮肤当前模式的模型目录确实存在 `*.exp3.json` / `*.motion3.json`（或 model3.json 引用）时才声明能力；把配置里的无效遗留绑定诚实报为不支持。

**验收**：模型无表情素材的 mver 皮肤上，`list_expressions` 返回空、`set_expression` 返回 `supported:false`，且解释原因。

### FR-4 连接失效自愈（P1）

命令执行遇 `DriverError` 时自动重新探测 driver 一次并重试该命令一次；仍失败才把错误返回调用方。

**验收**：猫进程被结束的瞬间发起命令，最终要么重试成功、要么返回可读错误，不抛裸异常。

### FR-5 仪表盘（P0）

本地 Web UI（默认 `127.0.0.1:8766`，启动自动打开浏览器），含五个板块：

1. **状态总览**：当前 driver、能力矩阵（绿=支持 / 灰=不支持）、猫在线状态、模型/模式、窗口可见性、mver 镜像线程存活；2 秒轮询。
2. **驱动选择**：自动 / embedded / cdp / mver，切换即保存并重建 driver。
3. **配置编辑**：可视化编辑 config.json 全部键，保存即生效（端口类需重启仪表盘）。
4. **工具试玩台**：网页上直接调用全部命令（表情下拉、按键、打字、气泡、窗口显隐、set-hand）。
5. **事件日志**：最近 200 条调用/跳过/切换事件，3 秒轮询；**本地操作记录（保存配置、切换驱动、onboard 进度）与服务端事件共存于同一面板且互不覆盖**；环形缓冲填满后仍持续更新（按事件单调序号增量渲染）。

**验收**（回归清单，对应 2026-08-19 修复的缺陷）：

- 连续产生 > 200 条事件后，日志面板仍每 3 秒出现新行；
- 点击"保存配置"产生的本地日志行在后续事件到达后依然存在；
- 在"驱动选择"下拉中选择了未保存的值，2 秒轮询不会将其重置；
- 表情下拉的已选项不被轮询重建重置，展开状态下不被收起；
- `/api/tool` 收到缺字段的 payload 返回可读的 JSON 错误（非 500）。

### FR-6 Mver 一键接入 onboard（P1）

一键完成：定位运行中的猫（或用显式目录/配置目录）→ **文本级改写猫自己的 config.json** 开启网络同步接收模式（保留作者注释，不碰程序本体）→ 提权结束并重启猫进程 → 切换 `mver_dir` 并重建 driver。全过程步骤在事件日志可见。

**验收**：对 `network:false` 的新装皮肤执行 onboard 后，`ping` 无"network 未开启"告警，猫恢复键鼠跟随。

### FR-7 新猫自动识别 auto-redirect（P2）

仪表盘状态轮询每 ≥5 秒探测一次运行中的 Mver 进程；配置的猫没在跑（或未配置）而另一只在跑时，自动切换 `mver_dir` 并重建 driver，事件日志记录切换。

**验收**：启动一只未配置的 Mver 猫后 ≤5 秒（下一次轮询周期内）状态栏切换到新猫。

### FR-8 聊天气泡 bridge（P0）

为无应用内气泡 UI 的 driver（cdp / mver）提供自绘气泡：透明置顶、点击穿透、不抢焦点、跟随猫窗口位置、打字机动画、过宽自动折行；猫窗口不可见/消失时气泡隐藏。

**验收**：`show_bubble` 后气泡出现在猫窗口上方且鼠标可穿透点击下层；`hide_bubble` 后消失；隐藏猫窗口后气泡不残留。

### FR-9 Mver 透明镜像层（P0）

Mver 处于接收模式（忽略本机键鼠）时，driver 内置 60fps 发送线程以真实键鼠状态合成协议帧转发；AI 指令作为覆盖层叠加（覆盖优先）。可通过独立进程 `mver-mirror.py` 在不启动 MCP 的情况下单独提供该层。

**验收**：开启镜像后猫的按键/光标跟随与本机模式无肉眼差异（延迟约一帧）；`press_key` 覆盖期间真实按键不受影响。

### FR-10 配置体系（P0）

读取优先级：环境变量 `BONGOCAT_*` > config.json > 内置默认值；仪表盘编辑的是 config.json；未知键忽略；端口类空值归一为 null。

**验收**：设置 `BONGOCAT_MCP_DRIVER=cdp` 后无需改文件即强制 cdp；仪表盘保存后 `/api/config` 与文件内容一致。

## 6. 非功能需求

| # | 类别 | 需求 |
|---|---|---|
| NFR-1 | 安全-网络面 | 所有监听/连接仅绑定本机回环（dashboard、embedded HTTP、CDP 端口、UDP 目标）；embedded token 由猫每次启动随机生成 |
| NFR-2 | 安全-侵入面 | 不修改猫程序本体；onboard 仅文本级改写猫自己的 config.json（与其设置界面写同一份文件）；提权操作仅限 taskkill/Start-Process 且有 UAC 确认 |
| NFR-3 | 可靠性 | 命令面任何输入（含非法 payload）都返回结构化 JSON 错误，不 500、不抛裸异常；参数错误不触发连接重探测 |
| NFR-4 | 可靠性-并发 | 状态轮询、工具调用、配置保存、driver 切换并发到达时行为确定（driver 重建互斥），不遗留重复的镜像发送线程 |
| NFR-5 | 性能 | mver 镜像 60fps 恒定节拍（落后自动对齐）；cdp 注入单次 < 100ms（本机 WS）；打字动画节奏约 200ms/字符（可看清） |
| NFR-6 | 兼容性 | Windows 10/11 x64；Python 3.10+；无编译型依赖（ctypes 直调 Win32） |
| NFR-7 | 可维护性 | 新增一个猫谱系只需实现 `CatDriver` 子类，调度层 / MCP 工具 / 仪表盘零改动 |
| NFR-8 | 可观测性 | 仪表盘进程内事件日志（200 条环形 + 单调 seq）；每个 driver 的 ping 附带配置告警（如 network 未开启） |

## 7. 约束与边界（当前已知限制）

1. **平台**：镜像层（GetAsyncKeyState/GetCursorPos）、窗口管理、CDP 注入均为 Windows 专属；embedded 理论上跨平台但未验证。
2. **mver 接收模式的代价**：MCP/镜像进程停止后猫失去键鼠响应（重新运行即恢复）；同一时刻只能有一只 Mver 实例占用接收端口。
3. **cdp 接管**：需要以调试端口重启正在运行的猫（闪断一次）；同时只支持一只。
4. **双进程并存**：仪表盘与 astrbot 的 stdio server 各持独立 driver 实例——embedded/cdp 无冲突，mver 双镜像为良性叠加，但聊天气泡可能各渲染一个。
5. **协议稳定性**：mver UDP 协议与 cdp 页面结构（pinia store 形状）为逆向/实证所得，上游更新可能失效，需要回归。
6. **边界-诚实性**：`set_hand` / `set_parameter` 仅 embedded 支持（其他谱系无对应事件面），能力矩阵如实标 ❌。

## 8. Roadmap（未实现，按优先级）

| # | 方向 | 说明 |
|---|---|---|
| R1 | **astrbot 深度接入** | 提供 stdio 配置样例与系统提示词模板；把工具反馈（气泡文本、打字内容）与聊天会话联动；探索"猫的人格化回复→气泡"专用工具（如 `cat_say` 组合封装） |
| R2 | 仪表盘实时推送 | 以 SSE/WebSocket 替代 2s/3s 轮询，事件日志准实时 |
| R3 | 事件持久化 | 事件日志落盘与回放，支持离线排障 |
| R4 | 多猫并行 | driver 多实例管理与路由（当前 cdp/mver 均单实例） |
| R5 | 单守护进程 | dashboard 与 MCP server 合一（MCP 走 Streamable HTTP transport），消除双进程气泡重复问题 |
| R6 | 跨平台 | macOS 镜像层（rdev 等价物）与窗口管理 |
| R7 | 分发 | uvx / pyinstaller 打包，降低安装门槛 |
| R8 | **多源命令缓存队列** | dispatch 层引入命令队列：多 agent / 多入口（MCP stdio、仪表盘 HTTP、hooks、AstrBot 插件）并发调用时排队串行执行而非互相覆盖——消除气泡 last-writer-wins 丢内容、表情回落跨进程互踩、mver 和弦时序互踩（同进程并发工具调用同样受益）。与 R5 互补：R5 把进程合一，R8 在进程内把命令排成一列；若 R5 先落地，R8 即其调度内核。背景：2026-08-20 多 agent 并发冲突分析 |

## 9. 验收与测试矩阵

| 层 | 手段 | 覆盖 |
|---|---|---|
| 协议回归 | `test_client.py`（MCP client 拉起 server.py，遍历全部工具） | FR-2/3/4；输出 OK/SKIP/FAIL 三态汇总，FAIL 退出码非 0 |
| 仪表盘手工清单 | `python dashboard.py` 后按 FR-5 验收清单逐项点检 | FR-5/6/7/10 |
| 视觉验证 | `docs/test-screenshots/` 截图存档（命名区分"已验证/已证伪"；本地存档不入库） | 气泡、按键动画、镜像跟随等需肉眼确认的效果 |
| 并发冒烟 | 同时开状态轮询 + 连续工具调用 + 配置保存 | NFR-3/4 |
