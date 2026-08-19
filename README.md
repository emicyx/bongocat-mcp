# bongocat-mcp

把各种「BongoCat 猫」统一封装成 MCP tools 的独立控制器——**与 BongoCat 仓库完全解耦**，
让 astrbot 等 MCP client / LLM 能主动控制猫咪的按键动画 / 打字 / 表情 / 聊天气泡，
无需猫重新编译。附带本地 Web 仪表盘，方便查看状态与编辑配置。

> 完整设计文档：[需求书 docs/requirements.md](docs/requirements.md) ·
> [架构实现书 docs/architecture.md](docs/architecture.md)。

## 支持三种猫（自动探测，也可配置强制指定）

| driver | 目标猫 | 原理 | 前置条件 |
|---|---|---|---|
| `embedded` | 自编译版 BongoCat（内置控制通道） | 本地 HTTP 控制通道（127.0.0.1 随机端口 + Bearer token） | 启动自编译版即可，自动发现 `mcp-server.json` |
| `cdp` | **Tauri 系成品**：官方 release、皮肤重打包版（前端不变只换模型资源） | WebView2 CDP 注入：以调试端口启动成品 → `Runtime.evaluate` 调 `__TAURI_INTERNALS__.invoke('plugin:event|emit')` 合成原生事件 | 无需配置；猫在跑但未开调试端口时**自动重启接管**（闪断一次）；exe 路径可在配置里指定 |
| `mver` | **BongoCatMver 系成品**：C++/SFML 皮肤版（手改 `img/` + `config.json`） | 实证逆向的 UDP 协议：**透明镜像层**（60fps 转发真实键鼠 + AI 覆盖叠加） | Mver 开启**网络同步**并设为**接收模式**；配置 `mver_dir` 可解析皮肤绑定 |

> **mver 接收模式的代价与镜像层**：Mver 开网络接收后会忽略本机键鼠，只渲染网络包。
> mver driver 的发送线程以 60fps 读取真实键鼠（`GetAsyncKeyState`/`GetCursorPos`）转发，
> 猫的行为与本机模式一致（延迟约一帧）；AI 指令作为覆盖层叠加。**MCP/镜像进程停止后
> 猫会失去键鼠响应**（重新运行即恢复）；同一时刻只能有一只 Mver 实例。

## 新猫自动接入（mver）

- **自动识别**：仪表盘状态轮询每 5 秒探测一次运行中的 Mver 进程；配置的猫没在跑（或未配置）
  而另一只在跑时，自动把 `mver_dir` 切到运行中的猫并重建 driver（事件日志可见切换记录）
- **一键接入**：仪表盘「🚀 一键接入 Mver 新猫」按钮，自动完成——定位运行中的猫 →
  文本级改写其 `config.json` 开启网络同步（接收模式，保留作者注释；**与它自家设置界面
  写的是同一份文件，不改程序本体**）→ 提权重启猫进程 → 重建 driver
- 新装的皮肤版 Mver 默认 `network:false`（不监听 UDP），一键接入即可修复；也可手工在
  猫的设置里开启网络同步并设为接收模式
- 注意：同一时刻只能有一只 Mver 实例占用接收端口

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows；macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 方式一：仪表盘（推荐日常使用，自动打开浏览器）
python dashboard.py               # 默认隐藏窗口后台运行
python dashboard.py --stop        # 停止后台仪表盘
python dashboard.py --visible     # 前台调试模式（终端可见）

# 方式二：MCP stdio server（供 astrbot 拉起）
python server.py

# 方式三：只让接收模式的 Mver 恢复键鼠跟随（不开 AI）
python mver-mirror.py               # 默认隐藏窗口后台运行
python mver-mirror.py --stop        # 停止隐藏运行的镜像
python mver-mirror.py --visible     # 前台调试模式（Ctrl+C 退出）

# 本地回归测试（自动探测 driver；或传 embedded / cdp / mver）
python test_client.py
```

## ZCode 插件（bongocat-notify）

`zcode-plugin/` 目录是一个**本地插件市场 + 插件**，让 Zcode 接入本 MCP server：

- **MCP 接入**：`.mcp.json` 把 `server.py` 注册为 stdio MCP 服务器
  （工具名 `mcp__bongo-cat__*`），智能体可直接控制猫；`/bongo-test` 命令全链路自检
- **任务播报**：hooks 在 Zcode 关键事件驱动猫咪气泡 + 表情切换——
  `Stop`（任务完成→星星眼）、`PermissionRequest`（等待审批→疑问）、
  `PostToolUseFailure`（出错→哭泣）、`SessionStart` / `UserPromptSubmit`（开工）
- 表情不写死索引：每次实时读 `get_cat_status` 的表情列表按名字关键词匹配，
  换皮肤自动适配；hook 走仪表盘 HTTP API（`python dashboard.py` 需保持运行），
  仪表盘不在时静默跳过，绝不阻塞会话

安装：Zcode → 设置 → 插件管理 → 发现 → `+` 添加本地市场目录
`zcode-plugin/`，安装 **bongocat-notify** 即可（详见 `zcode-plugin/bongocat-notify/README.md`）。

> 想为 ZCode / AstrBot 或其他客户端**开发自己的猫播报插件**？
> 接入通道选型、插件骨架模板、表情可扩展性约定与验证方法论见
> [接入开发指南 docs/zcode-plugin-dev.md](docs/zcode-plugin-dev.md)。

## Claude Code 插件（bongocat-notify）

`claude-plugin/` 是同一套「本地市场 + 插件」的 Claude Code 版（与 ZCode 版功能对等）：

- **MCP 接入**：`.mcp.json` 把 `server.py` 注册为 stdio MCP 服务器
  （工具名同为 `mcp__bongo-cat__*`），`/bongo-test` 命令全链路自检
- **任务播报**：事件模型有差异——Claude Code 没有 `PermissionRequest` /
  `PostToolUseFailure` 事件，等待审批由 `Notification` 表达（按 message 关键词
  过滤空闲提示），工具出错由 `PostToolUse` 的 `tool_response` 保守判定

安装：`claude plugin marketplace add claude-plugin/目录` →
`claude plugin install bongocat-notify@bongocat-local`，重启会话后 `/mcp` 验证
（详见 `claude-plugin/bongocat-notify/README.md`）。

## Codex 插件（bongocat-notify）

`codex-plugin/` 是同一套插件的 OpenAI Codex CLI 版（与 ZCode 版功能对等）：

- **MCP 接入**：`.mcp.json`（Codex 原生直连服务器格式）把 `server.py` 注册为
  stdio MCP 服务器，`bongo-test` 技能（`skills/*/SKILL.md`，Codex 自定义
  prompts 已废弃、技能是官方替代）全链路自检
- **任务播报**：Codex hooks 与 ZCode 事件几乎一一对应——`PermissionRequest`
  是原生事件；工具出错没有 `PostToolUseFailure`，由 `PostToolUse` 的
  `tool_response` 保守判定；hooks 由插件清单（`.codex-plugin/plugin.json`）
  捆绑，全部 `async` 后台执行不阻塞回合

安装：`codex plugin marketplace add codex-plugin/目录` →
`codex plugin install bongocat-notify@bongocat-local` → **`/hooks` 里逐个
Trust 这 5 个 hook**（Codex 信任审查机制，不信任不执行）→ 新会话
`codex mcp list` 验证（详见 `codex-plugin/bongocat-notify/README.md`）。

## 配置（config.json，仪表盘可编辑）

读取优先级：环境变量 `BONGOCAT_*` > `config.json` > 默认值。首次使用可复制
`config.example.json` 为 `config.json`。

| 键 | 说明 |
|---|---|
| `driver` | 空=自动探测；`embedded` / `cdp` / `mver` 强制指定 |
| `app_path` | cdp：BongoCat.exe / bongo-cat.exe 路径 |
| `app_paths` | cdp：额外候选路径列表 |
| `cdp_port` | cdp：调试端口，默认 9223 |
| `mver_dir` | mver：皮肤目录（含 config.json），用于键位绑定与接收端口 |
| `mver_port` | mver：接收端口；空=从皮肤 config.json 的 `network.receive_port` 读 |
| `host` | 目标主机，默认 127.0.0.1 |
| `embedded_config` / `embedded_port` / `embedded_token` | embedded：覆盖自动发现 |
| `dashboard_host` / `dashboard_port` | 仪表盘监听地址，默认 127.0.0.1:8766 |

对应环境变量：`BONGOCAT_MCP_DRIVER`、`BONGOCAT_APP_PATH`、`BONGOCAT_CDP_PORT`、
`BONGOCAT_MVER_DIR`、`BONGOCAT_MVER_PORT`、`BONGOCAT_MCP_HOST`、
`BONGOCAT_MCP_CONFIG`、`BONGOCAT_MCP_PORT`、`BONGOCAT_MCP_TOKEN`（与旧版兼容）。

## 仪表盘

`python dashboard.py` 启动（自动打开浏览器），包含：

- **状态总览**：当前 driver、能力矩阵（绿=支持 / 灰=该猫不支持）、猫状态（模型/模式/窗口）、
  mver 镜像线程，2 秒轮询刷新
- **驱动选择**：自动 / embedded / cdp / mver，切换即保存并重建 driver
- **配置编辑**：可视化编辑 config.json 全部键
- **工具试玩台**：网页上直接调用全部命令（表情下拉、按键、打字、气泡、窗口显隐、
  set-hand），带最近 200 条事件日志

> 仪表盘与 astrbot 的 stdio server 各持独立 driver 实例，可并行使用；
> embedded / cdp 无冲突，mver 双镜像为良性叠加（两路相同状态帧），
> 聊天气泡可能在两个进程各渲染一个。

## MCP Tools（14 个工具，映射到 12 个统一命令，全部 driver 一致）

| 工具 | 说明 | embedded | cdp | mver |
|---|---|---|---|---|
| `ping` | 健康检查 | ✅ | ✅ | ✅ |
| `get_cat_status` | driver/capabilities/模型信息/窗口可见性 | ✅ | ✅ | ✅ |
| `list_expressions` / `list_motions` | 列出表情/动作 | ✅ | ✅ | ⚠️ 需模型素材 |
| `set_expression(index, duration)` | 切换表情（duration 秒后自动回默认表情，0=保持） | ✅ | ✅ | ⚠️ 需模型素材 |
| `play_motion(motion)` | 播放动作 | ✅ | ✅ | ⚠️ 需模型素材 |
| `press_key` / `release_key` | 按键/松键动画 | ✅ | ✅ | ✅ |
| `type_text(text)` | 逐字符打字动画 | ✅ | ✅ | ✅ |
| `set_hand(left, right)` | 猫爪下压 | ✅ | ❌ | ❌ |
| `set_parameter(id, value)` | Live2D 参数 | ✅ | ❌ | ❌ |
| `show_bubble` / `hide_bubble` | 聊天气泡（打字动画后 8 秒自动消失，duration=0 常驻） | ✅ | ✅ | ✅ |
| `set_window_visible(visible)` | 显示/隐藏猫窗口 | ✅ | ✅ | ✅ |

能力是**资产感知**的：mver 皮肤只有当模型目录确实存在表情/动作素材文件时才会
advertise 对应能力，否则诚实报不支持（避免把无效遗留配置当成能力）。

## 安全说明

- 所有通道仅绑定本机回环地址；embedded 通道每次启动随机 Bearer token
- cdp 的 WebView2 调试端口（默认 127.0.0.1:9223）是本机控制面，不用时勿长期开着带调试端口的猫
- cdp 接管会重启一次正在运行的猫；同时只支持一只猫

## 项目结构

```
bongocat-mcp\
  bongocat_mcp\           # 核心包
    config.py             # 统一配置（env > config.json > 默认）
    detect.py             # driver 探测/切换
    dispatch.py           # 命令调度（能力门控 + 事件日志）
    drivers\              # embedded_http / cdp_webview2 / mver_udp / win32_utils
    bubble\overlay.py     # bridge 自绘聊天气泡窗
  server.py               # MCP stdio 入口
  dashboard.py            # FastAPI 仪表盘
  web\index.html          # 仪表盘前端（原生单页，无构建）
  mver-mirror.py          # Mver 独立镜像进程
  zcode-plugin\           # ZCode 插件（本地市场 + bongocat-notify）
  claude-plugin\          # Claude Code 插件（本地市场 + bongocat-notify）
  codex-plugin\           # Codex CLI 插件（本地市场 + bongocat-notify）
  docs\                   # 需求/架构/接入文档；验证截图为本地存档不入库
```

## Mver UDP 协议（实证逆向笔记）

- 312 字节全量状态帧，60fps 连发，无握手
- `bytes[0..255]`：VK 索引按键状态；`0x81`=按下（**整个按住期间持续发送**）、
  `0x80`=松开边缘帧、`0x00`=空闲；VK `0x01`/`0x02` = 鼠标左右键
- `bytes[256..311]`：14 个 float，`fl[8]=0.8×光标x/屏宽`、`fl[9]=0.8×光标y/屏高`
- 恒定槽位 `0x90/0xF0/0xF3/0xF6/0xFB = 0x01`
- 组合键绑定需**时序按下**（修饰键先按住 ≥0.3s 再按触发键）
- `mode`：1=标准、2=键盘、3=手柄（来自 BongoCatMverUI 源码）
