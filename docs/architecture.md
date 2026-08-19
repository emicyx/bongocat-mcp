# bongocat-mcp 架构实现书

| | |
|---|---|
| 版本 | v1.1 |
| 日期 | 2026-08-19 |
| 状态 | 与代码同步（含 2026-08-19 仪表盘缺陷修复） |
| 关联文档 | [需求书](requirements.md)、[README](../README.md) |

---

## 1. 总体架构

三个入口进程共享同一套核心包，各持独立 driver 实例：

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ astrbot 等    │   │ 浏览器        │   │ 终端用户      │
│ MCP client    │   │ 仪表盘 UI     │   │ (无 AI 场景)  │
└──────┬───────┘   └──────┬───────┘   └──────┬───────┘
  stdio(MCP)        HTTP(FastAPI)      直接构造
       │                 │                   │
┌──────▼───────┐  ┌──────▼───────────┐  ┌────▼─────────┐
│ server.py    │  │ dashboard.py     │  │ mver-mirror  │
│ FastMCP      │  │ /api/status      │  │ (纯镜像)      │
│ 14 个工具     │  │ /api/tool ...    │  └────┬─────────┘
└──────┬───────┘  └──────┬───────────┘       │
       │                 │                   │
       ▼                 ▼                   │
   dispatch(cmd,payload) ── 能力门控+事件日志+失效重试
       │                 │                   │
       ▼                 ▼                   │
   resolve_driver(detect) ── 强制配置>自动探测，互斥重建
       │                 │                   │
┌──────▼──────┐  ┌───────▼──────┐  ┌────────▼─────┐
│ embedded    │  │ cdp          │  │ mver         │
│ HTTP+Bearer │  │ WebView2 CDP │  │ UDP 60fps    │
└──────┬──────┘  └───────┬──────┘  └────────┬─────┘
       │                 │                  │
       ▼                 ▼                  ▼
  自编译 BongoCat   Tauri 系成品猫      BongoCatMver 皮肤版
  (内置控制通道)    (重启接管+JS注入)   (镜像层+覆盖层)
                                          │
                                    bubble/overlay
                                    (cdp/mver 共用气泡)
```

关键拓扑事实：

- **三个入口 = 三个独立进程**，各自 `resolve_driver` 得到自己的 driver 实例。embedded/cdp 天然无冲突；mver 的两路镜像发送相同状态帧，属良性叠加；气泡 overlay 按进程各一份（已知限制，见 §9）。
- **命令面唯一**：MCP 工具、仪表盘试玩台、`test_client.py` 走的都是同一个 `dispatch()`，能力门控 / 事件日志 / 失效重试只写一处。

## 2. 模块清单与职责

```
bongocat-mcp\
  bongocat_mcp\
    config.py             统一配置（env > config.json > 默认；RLock 保护的文件缓存）
    detect.py             driver 解析与缓存（互斥重建；探测顺序 embedded→cdp→mver）
    dispatch.py           命令调度（能力门控 + 参数校验 + 失效重探测 + 事件环形日志）
    onboard.py            Mver 新猫接入（发现/文本级补丁/提权重启/自动切换）
    drivers\
      base.py             CatDriver 抽象 + 12 个命令常量 + DriverError
      embedded_http.py    自编译版：mcp-server.json 发现 + HTTP/Bearer
      cdp_webview2.py     Tauri 成品：CDP 会话 + 接管重启 + JS 注入 + pinia 读取
      mver_udp.py         Mver：协议帧合成 + 60fps 镜像线程 + 覆盖层 + 皮肤解析
      win32_utils.py      ctypes 级 Win32 小工具（进程/窗口/显隐）
    bubble\overlay.py     tkinter 自绘气泡（独立线程 + 队列 + 点击穿透）
  server.py               MCP stdio 入口（FastMCP，14 个工具 → dispatch）
  dashboard.py            FastAPI 仪表盘（状态/配置/驱动切换/试玩台/事件/onboard）
  web\index.html          仪表盘前端（原生单页，无构建）
  mver-mirror.py          独立镜像进程入口
  test_client.py          MCP client 侧全工具回归
```

## 3. 关键设计决策

### D1 与上游解耦的"黑盒"控制

**决策**：不 fork BongoCat 仓库、不重编译、不改程序本体；三种猫分别用其自身暴露的通道（HTTP 控制口 / WebView2 调试端口 / 网络 UDP）控制。

**理由**：上游迭代频繁、皮肤版由第三方维护，任何源码级耦合都会把维护成本乘以谱系数。黑盒方式的代价是协议逆向（D4/D5），但失效面收敛在 driver 内。

**被否方案**：向官方提 PR 增加通用控制 API——周期不可控且覆盖不了 Mver 谱系。

### D2 `CatDriver` 抽象 + 能力门控

**决策**：driver 只需实现 `capabilities() -> set[str]` 与 `call(cmd, payload) -> dict`；命令名与 embedded HTTP 接口一致（`base.py` 的 12 个常量）。`dispatch` 在调用前做能力检查，不支持则返回 `{"ok":false,"supported":false}` 并记 `skip` 事件。

**理由**：命令面对齐 embedded 使三个谱系共享一套语义；能力是**运行时属性**（mver 取决于皮肤资产），不能靠 driver 名静态推断。

**被否方案**：让 driver 在 `call` 里抛"不支持"异常——调用方（LLM）难以区分"参数错"与"能力缺失"，且无法在 UI 上预置灰化。

### D3 embedded：发现文件 + Bearer

`%APPDATA%/com.ayangweb.BongoCat/mcp-server.json` 由猫启动时写入（随机端口 + 随机 token）；driver 优先读配置覆盖（`embedded_port/token/config`），否则解析该文件。连接失败时清空配置缓存以便下次重新发现。`probe()` 要求 ping 通才算可用（文件存在 ≠ 猫活着）。

### D4 cdp：调试端口接管 + `Runtime.evaluate` 注入

**决策**链路：以 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9223` 启动（或杀掉无调试端口的运行实例重启接管）→ `/json/list` 选主窗口 target（排除 preference/devtools，优先有 canvas 的页面，12s 内重试等待加载）→ WebSocket `Runtime.evaluate` 调 `__TAURI_INTERNALS__.invoke('plugin:event|emit', ...)` 合成原生事件（`set-expression` / `start-motion` / `device-changed`）。状态读取则注入 JS 直接读 pinia 的 `model` store（`_STATUS_JS`）。

**理由**：Tauri 应用的偏好窗口本来就是用 JS `emit()` 触发这些事件的，前端监听管线现成——注入事件等价于"官方内部路径"，对前端版本不敏感。

**细节**：

- 启动猫进程必须 `DETACHED_PROCESS | DEVNULL`——猫不能继承 MCP server 的 stdio，否则污染协议通道。
- `_CDPSession` 为"后台 asyncio loop 线程 + 持久 WS"：同步调用面经 `run_coroutine_threadsafe`，等待响应时按 msgid 过滤；WS 中途断开则重置重试一次。
- 键名直接发 rdev 风格（`KeyA`/`Num1`/`Space`），复用前端 useDevice 的归一化。

**被否方案**：SendInput 真实按键——会干扰用户输入且无法做 `set_expression`。

### D5 mver：实证逆向的 UDP 协议 + 镜像/覆盖双层

**协议**（312 字节全量帧，60fps 连发，无握手）：

| 区域 | 含义 |
|---|---|
| `bytes[0..255]` | 按 Windows VK 索引：`0x81`=按下（**按住期间持续发**）、`0x80`=松开边缘帧（1-2 帧）、`0x00`=空闲；VK 0x01/0x02=鼠标左右键 |
| `bytes[256..311]` | 14 个 float；`fl[8]=0.8×光标x/屏宽`、`fl[9]=0.8×光标y/屏高`（物理像素） |
| 恒定槽位 | `0x90/0xF0/0xF3/0xF6/0xFB = 0x01`（真实 sender 常亮，照抄） |

**发送循环**（`mver_udp.py::_send_loop`）每帧合成：

```
状态(vk) = 覆盖层[vk]（AI 按下） ?? 真实键鼠[vk]（GetAsyncKeyState）
  按下        -> 0x81，清松开计数
  由按转松    -> 0x80，记 2 帧余晖
  余晖计数>0  -> 0x81->0x80 递减
  否则        -> 0x00
```

**理由**：接收模式会忽略本机键鼠，必须有人代为转发——driver 内置 60fps 镜像层使"开 AI"与"不开 AI"对用户无感；AI 指令作为覆盖层叠加在同一条帧流上，天然与真实输入合成而互不干扰。`mver-mirror.py` 只是"只开镜像不开 MCP"的裁剪入口。

**细节**：

- 表情/动作经"按下皮肤绑定键"间接触发；组合键有时序要求（修饰键先按 ≥0.3s 再按触发键），`_hold_chord` 按此实现。
- 接收端口**必须**从皮肤自己的 `config.json`（`network.receive_port`）解析——UDP 发完即忘，端口错了没有任何错误反馈，默认值不能拍脑袋。
- 皮肤 config 是带 `//` 注释的 JSON：先文本级剥注释再 `json.loads`；表情/动作的**人类可读名**来自作者写的行尾注释（`[18,219]//正常眼`），用括号配平截取数组原文后逐项提取。

### D6 气泡 overlay：tkinter 独立线程 + 点击穿透

**决策**：cdp/mver 无应用内气泡 UI，用 tkinter 自绘 bridge：`overrideredirect` + `transparentcolor` 分层窗口，一次性设置 `WS_EX_LAYERED|TRANSPARENT|TOOLWINDOW|NOACTIVATE` 扩展样式；隐藏 = 移到屏幕外（`+-32000`）而非 withdraw/deiconify（后者会重置扩展样式，与穿透/分层拉锯）。

**理由**：tkinter 是 stdlib，无额外依赖；MCP stdio 占主线程，tk 必须独立线程，命令经 `queue` 投递、`after` 轮询消化。30ms 跟随猫窗口重排位置，60ms 打字机步进，过宽按字符折行。

### D7 配置三层合并

`config.py` 以 `RLock` 保护的进程内缓存读写 config.json；`get(key)` 逐层 env > file > default，端口类键归一为 `int|None`。`save()` 只合并已知键（未知键忽略），写后刷新缓存。仪表盘表单的"底稿"用 `snapshot()`（env 覆盖后的生效视图）。

### D8 仪表盘轮询架构（2026-08-19 修复后）

| 轮询 | 周期 | 端点 | 说明 |
|---|---|---|---|
| 状态 | 2s | `/api/status` | driver/能力/猫状态/表单底稿；下拉与输入框仅在**未聚焦/未修改**时同步（touched 标记），表情下拉按内容指纹决定是否重建并保留选中项 |
| 事件 | 3s | `/api/events` | **按事件 seq 增量 prepend**；首拉渲染历史，之后只把 `seq > 已见最大值` 的新行插到顶部；本地操作行（保存配置/切换驱动/onboard）与服务端行共存 |
| 新猫探测 | ≥5s（节流+互斥） | 内嵌于 `/api/status` | `auto_redirect()`：配置猫未运行且另有 Mver 在跑 → 改 `mver_dir` 并 refresh driver |

**为什么 seq 而不是条数**：事件是 200 条环形缓冲，填满后 `recent_events()` 长度恒等于 200，"条数变化"永远为假——旧实现因此在调试中后段日志冻结；且旧实现整体重建 `innerHTML` 会顺带抹掉本地写入的行。

**为什么 driver 重建要互斥**（`detect.py::_driver_lock`）：FastAPI 的 sync handler 跑线程池，状态轮询（auto-redirect 触发 refresh）、配置保存、试玩台调用、dispatch 失效重试可能并发 `resolve_driver(refresh=True)`；无锁时会双 close/双创建，mver driver 会遗留**永不停止的孤儿发送线程**（两路相同 UDP 流叠加）。

### D9 事件日志的数据模型

`dispatch.py`：`itertools.count` 产生单调 `seq`（在 `_events_lock` 内取号），事件条目 `{seq, time, kind, detail}`，环形裁剪保 200 条。前端把 seq 当作游标做增量拉取（D8）。`dispatch` 对 `KeyError/ValueError/TypeError`（payload 缺字段/类型不符）返回结构化错误且**不触发**失效重探测——参数错误不是连接问题。

## 4. 技术选型

| 领域 | 选型 | 理由 | 被否方案 |
|---|---|---|---|
| 语言 | Python 3.10+ | MCP/astrbot 生态同语言；`X \| None` 等语法 | Rust/TS（与生态割裂） |
| MCP SDK | 官方 `mcp` 包（FastMCP） | 装饰器即工具，stdio 传输内建 | 手写 JSON-RPC |
| Web 框架 | FastAPI + uvicorn | pydantic 请求体校验、sync handler 自动线程池 | Flask（校验手写） |
| CDP 客户端 | `websockets` + 自管 asyncio 线程 | 单页注入场景足够轻 | playwright（重依赖，为一条 evaluate 不值） |
| Win32 访问 | ctypes 直调 + `tasklist`/powershell 兜底 | 零编译依赖，可移植到任意 venv | pywin32/pywinauto（二进制依赖） |
| 气泡 UI | tkinter | stdlib；分层窗口样式可经 ctypes 补齐 | PyQt（体积） |
| 前端 | 原生单页 HTML/JS，无构建 | 一个文件、零工具链，2s/3s 轮询足够 | Vue/React（构建链不值得） |
| HTTP 客户端 | urllib（stdlib） | embedded 通道就几个 POST | requests（可省的依赖） |
| 测试 | `mcp` client 侧拉起 server 全链路回归 | 覆盖协议序列化与调度层 | 单元测试为主（driver 强依赖真实猫， mocks 价值低） |

依赖面（requirements.txt）：`mcp`、`websockets`、`fastapi`、`uvicorn`（+测试用 `pillow`）。

## 5. 关键数据流

### 5.1 agent → `set_expression` → cdp

```
astrbot ──MCP stdio──▶ server.py(set_expression)
  └▶ dispatch("set-expression", {index})
       ├─ resolve_driver()                    # 命中缓存 cdp 实例
       ├─ capabilities() 含 set-expression     # 门控通过
       └─ driver.call:
            _emit("set-expression", index)
              ├─ _ensure_attached()            # devtools 探活；必要时重启接管
              ├─ js = invoke('plugin:event|emit', {event, payload})
              └─ cdp.evaluate(js)              # 线程池 → asyncio loop 线程 → WS
                   └─ 猫前端收到原生事件 → Live2D 表情切换
       └─ log_event("call", ...) ──▶ /api/events（仪表盘 3s 内可见）
```

### 5.2 mver 帧合成（见 D5 表）

镜像层读真实键鼠 → 与覆盖层合成 VK 状态字节 → 填恒定槽位 → 打包 14 floats（含归一化光标）→ `sendto` → 按下一帧节拍 `sleep` 对齐。

## 6. 并发模型与线程清单

| 线程/池 | 属主 | 生命周期 | 同步 |
|---|---|---|---|
| uvicorn 线程池（sync handler） | dashboard | 进程级 | `_redirect_lock`（auto-redirect 节流） |
| asyncio loop 线程 | `_CDPSession` | driver 级 | `run_coroutine_threadsafe`；`cdp._lock` 串行化 attach |
| 60fps 发送线程 | `MverUdpDriver` | driver 级（daemon） | `mver._lock` 保护覆盖表；`_stop` event 终止 |
| tk 主循环线程 | `bubble.overlay` | 进程级单例 | `queue` 投递命令；`_instance_lock` 防重复创建 |
| MCP 请求处理 | FastMCP | 会话级 | — |

全局锁：`config._lock`（RLock，文件缓存）、`dispatch._events_lock`、`detect._driver_lock`（2026-08-19 新增）。无嵌套锁路径，无死锁环。

## 7. 错误处理与降级

| 情形 | 行为 | 用户可见 |
|---|---|---|
| driver 不支持命令 | 门控返回 `supported:false`，记 skip 事件 | MCP：结构化 JSON；UI：灰色"不支持" |
| payload 缺字段/类型错 | `dispatch` 捕 KeyError/ValueError/TypeError → `ok:false` + 参数提示（不触发重探测） | 可读错误，无 500 |
| 连接失效（猫关闭/重启） | DriverError → refresh 重探测一次 → 重试一次 | 多数自愈；仍失败返回错误 |
| 冷启动无任何猫 | `resolve_driver` 抛带三条指引的 DriverError | 仪表盘红点 + 引导文案 |
| mver 端口/配置可疑 | `ping` 附带 note（network 未开 / is_sender / 配置解析失败） | 状态页提示 |
| cdp attach 超时 | 25s 等待后 DriverError | 试玩台/状态报错 |
| tk 绘制异常 | `_track` 捕获后继续 after 循环 | 气泡不致死 |

## 8. 安全设计

- **网络面全部回环**：dashboard `127.0.0.1:8766`、embedded HTTP `127.0.0.1:<随机>`（猫每次启动随机 token）、CDP `127.0.0.1:9223`、UDP 目标取自配置（默认本机）。
- **CDP 端口即控制面**：开着调试端口的猫可被本机任意进程注入，README 明示"不用时勿长期开着"。
- **提权面收敛**：仅 onboard 的 `_elevated_kill/_elevated_start`（taskkill/Start-Process + UAC），且仅改写猫自己的 config.json（与其设置界面同一份文件、同样的键）。
- **进程隔离**：cdp 拉起猫时 `DETACHED_PROCESS` + stdio 全 DEVNULL，防猫继承 MCP 协议管道。
- **前端**：日志渲染对插值做 HTML 转义（错误串可能含路径/引号）。

## 9. 已知问题与技术债

**2026-08-19 已修复**（详见需求书 FR-5 验收清单）：

1. 事件日志环形缓冲填满后长度恒定，前端按条数判断更新 → 永久冻结；且整体重建 `innerHTML` 会抹掉本地日志行。→ 改为 seq 游标增量 prepend。
2. 2s 轮询无条件重写 `driverSel.value` / `exprSel.innerHTML` → 未保存的驱动选择被重置、表情选中项被重置。→ touched 标记 + 内容指纹 + 保留选中。
3. `/api/tool` 对缺字段 payload 抛 KeyError → 500 且前端 `r.json()` 失败、按钮看似无反应。→ dispatch/dashboard 两级拦截。
4. `resolve_driver` 无锁 → 并发重建可遗留 mver 孤儿发送线程。→ `_driver_lock`。
5. （测试中发现）seq 增量渲染的首版实现以 `_lastSeq === undefined` 判定"首次渲染"：当页面加载时服务端事件缓冲为空（仪表盘刚重启），该标志一直不置位，稍后首批事件到达会再次触发 `innerHTML` 整体重建、抹掉已积累的本地日志行。→ 改为一次性 bootstrap 标志（`_boot`），历史渲染只发生一次。
6. （测试中观察到一次，未能复现）长寿命实例上 `/api/config`、`/api/driver` 持续返回 500（`config.save` 已完成、`resolve_driver(refresh=True)` 抛出非 DriverError，异常详情因 stderr 未捕获而丢失；新进程/并发压力/重复保存均无法复现）。→ 三个端点（config/driver/status）统一兜底捕获所有异常、返回结构化 JSON 并打印堆栈，符合 NFR-3"任何输入不 500"。
7. （全链路演示中发现）`detect.py` 的 `[detect]` 日志 print 到 stdout——而 stdio server 的 stdout 是 MCP JSONRPC 专线，客户端解析报错。→ 改走 stderr。
8. （全链路演示中发现）`set_window_visible(false)` 后无法再 `show`：`find_window` 只枚举可见窗口，隐藏后找不到目标；气泡定位同理失效。→ `enum_windows/find_window` 增加 `visible_only` 参数，mver/cdp 两 driver 的显隐命令改用全量枚举。
9. （用户报告）猫进程被关闭后仪表盘仍显示"在线"：mver 的 `ping` 只检查 driver 自己的 UDP 发送线程（活在调用方进程里），`status` 只读皮肤静态配置——都与猫无关；仅 `windowVisible` 如实变 false，形成"在线 + 窗口不可见"的自相矛盾。→ ping/status 增加**猫进程存活校验**（`_cat_process_alive`，tasklist 排除 UI/转换器伴随进程）：进程不在则 ping `ok:false`（附 note）、status 返回 `ok:false` 与恢复指引；仪表盘前端同时展示 `s.cat.error` 的具体离线原因。
10. （用户反馈）切换表情时猫爪按键动画干扰观感：旧实现 `_hold_chord(..., 1.2s)` 让触发键持续按住 1.2s，猫爪趴在按键上很显眼。→ 新增 `_tap_chord` 轻触式触发：修饰键按协议下限 0.32s，触发键仅保持 0.1s（约 6 帧，猫爪几乎来不及做动作），实测验证表情切换仍可靠生效（截图确认"叼花"）；`set_expression` 端到端耗时从 ~1.7s 降到 ~0.5s。`play_motion` 暂保持原时序（动作本身即动画，按键前摇无碍）。
11. （用户反馈）`mver-mirror.py` 直接启动会常驻一个终端窗口。→ 默认自转生为无控制台后台进程（`CREATE_NO_WINDOW`，pid 记录在 `.mver-mirror.pid`）；`--visible` 保留前台调试（Ctrl+C），`--stop` 按 pidfile 结束隐藏进程。已实测：隐藏进程主窗口句柄为 0，stop/重启生命周期正常。
12. （用户反馈）仪表盘同样常驻终端窗口。→ `dashboard.py` 采用与镜像一致的隐藏模式（pidfile `.dashboard.pid` + `--stop` / `--visible`），并增加端口代检：已有实例在监听时在原终端提示"已在运行"而非无声失败（隐藏进程的报错不可见）。已实测：隐藏运行 HTTP 200、主窗口句柄 0、重复启动有提示。
13. （三 driver 全量验证中发现）`server.py` 的 `list_expressions`/`list_motions` 在 embedded 返回 `modelInfo: null`（如猫未加载模型）时抛 `'NoneType' object has no attribute 'get'`——键存在但值为 null 时 `.get("modelInfo", {})` 的默认值不生效。→ 链式 `or {}` 防御，空模型时返回空列表（实测 isError=false）。

**未修（登记在案）**：

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| K1 | cdp `_ensure_attached` 持续失败（如 app_path 指向非猫 exe）时，每个 2s 状态轮询都会再次 Popen 拉起进程 | 配置错误时反复开新进程 | attach 失败后加冷却期再允许重试 |
| K2 | 双进程（dashboard + astrbot）各渲染一个气泡 | 气泡偶现双份 | R5 单守护进程，或 overlay 加跨进程互斥 |
| K3 | mver 注释剥离正则不覆盖"字母后行尾注释"（如 `"network": true //x`） | 个别皮肤 config 解析失败 → driver 报错 | 换成容忍任意前缀的行尾注释剥离 |
| K4 | `tasklist` 输出按 UTF-8 解码（中文系统实为 GBK） | 进程名含非 ASCII 时匹配失败 | 按 ANSI 代码页解码（进程名匹配为 ASCII 关键字，当前实际不受影响） |
| K5 | README 历史上写"12 个工具"，实为 14 个 MCP 工具（12 个底层命令） | 文档口径 | 已在需求书/本文按 14/12 澄清 |
| K6 | 事件日志仅存仪表盘进程内存，MCP stdio 进程的事件不可见 | 排障盲区 | R3 持久化 |
| K7 | **debug 构建的 Tauri 猫不能独立运行**：前端从 Vite dev server（localhost:1420）加载，dev server 不在跑时 WebView 显示"localhost 拒绝连接"错误页——cdp 会注入到错误页（ping 通但 status/emit 全失败，极具迷惑性），embedded 协议层照常工作但猫无画面 | 用户 `app_path` 指向 `target/debug/bongo-cat.exe` 时体验崩坏 | `cargo build --release` 出独立 exe 后把 app_path 指过去；或使用前先 `pnpm dev:vite`。已实测：dev server 运行时 embedded 15/15、cdp 12+3skip 全过 |

## 10. 演进方向（对应需求书 Roadmap）

- **R1 astrbot 接入拓扑**：astrbot 以 stdio 拉起 `python server.py`（env 传 `BONGOCAT_*` 覆盖）；建议的系统提示词让 agent 先 `get_cat_status` 再动作；"猫格化回复"可在 agent 侧封装 `type_text + show_bubble` 组合。
- **R5 单守护进程**：把 dashboard 与 MCP server 合并为一个进程（FastAPI 挂 `/mcp` Streamable HTTP 路由），driver 实例唯一，消除 K2；仪表盘与 MCP 的事件日志也随之合一（解决 K6）。
- **R2 实时推送**：`/api/events` 加 SSE（FastAPI 原生支持），前端 EventSource 替代 3s 轮询；seq 游标模型可直接复用。

## 11. 测试策略

1. **协议回归**（`test_client.py`）：MCP client 拉起 server.py，遍历 14 个工具；结果三态 OK/SKIP/FAIL，FAIL 退出码非 0。能力降级（supported:false）记 SKIP 不记失败。
2. **仪表盘手工清单**：需求书 FR-5 的五条验收（含本次修复的回归项）。
3. **视觉验证存档**：`docs/test-screenshots/`（本地存档，不入 git 仓库），文件名以"已验证-/已证伪-"前缀区分结论（如"已证伪-所谓哭泣眼(泪滴是常驻发饰).png"），防止后续把幻觉当结论。
4. **并发冒烟**：开状态轮询的同时连续调用工具 + 保存配置，验证无孤儿线程（任务管理器观察 UDP 发送停止）。
