# bongocat-notify（Codex 插件）

让桌面 BongoCat 猫成为 OpenAI Codex CLI 的任务播报员：**任务完成 / 等待审批 /
工具出错**等关键时刻，猫头顶弹出气泡告诉用户任务状态，并按表情列表切换对应表情；
同时把 bongocat-mcp 注册为 MCP 服务器，让智能体能直接操控猫咪（`bongo-test` 技能）。

与 ZCode 版（`zcode-plugin/bongocat-notify`）功能对等。Codex 的事件模型与 ZCode
几乎一一对应——`PermissionRequest` 是原生事件（这点比 Claude Code 版更贴切），
唯一需要适配的是工具出错：Codex 没有 `PostToolUseFailure`，由 `PostToolUse` 的
`tool_response` 保守启发式判定。

## 提供什么

| 组件 | 内容 |
|---|---|
| MCP 服务器 | `bongo-cat`（stdio 拉起 bongocat-mcp 的 `server.py`，工具名 `mcp__bongo-cat__*`） |
| `bongo-test` 技能 | 走 MCP 工具全链路自检：ping → 状态 → 表情切换 → 气泡 |
| hooks | 5 个事件的猫咪播报（见下表） |

## 事件 → 猫的行为

| Codex 事件 | 对应 ZCode 事件 | 触发时机 | 气泡文案（默认） | 表情关键词（按优先级） |
|---|---|---|---|---|
| `SessionStart` | `SessionStart` | 会话启动/恢复 | 🐱 Codex 已就位，随时可以开工喵！ | 默认 / 放松 |
| `UserPromptSubmit` | `UserPromptSubmit` | 用户提交新任务 | 🐾 收到新任务，本喵开始干活！ | 放松 / 默认 |
| `Stop` | `Stop` | **任务完成** | ✅ 任务完成啦！Codex 已交出结果… | 星星眼 / 点赞 |
| `PermissionRequest` | `PermissionRequest` | **等待审批**（原生事件，stdin 带 `tool_name`） | ❓ Codex 在等你的审批：「工具」需要确认… | 疑问 |
| `PostToolUse` | `PostToolUseFailure` | 工具响应判定为出错（启发式，见下） | 💦 有工具出错了（「工具」）… | 哭泣 / 生气 / 眩晕 |

**适配点说明**：

- Codex 不保证 `PostToolUse` 携带统一的错误标记，`notify.py` 对 `tool_response`
  做保守启发式判定（`is_error`/`isError` 为 true、文本以 `Error` 或 `Exit code`
  开头；Bash 非零退出也会触发 `PostToolUse`），判不出错就静默跳过——宁可漏报
  不误报。若实际使用中失败播报不触发，把判定条件加进 `response_is_error()` 即可。
- `Stop` 事件带 `stop_hook_active` 字段，沿用 ZCode 版的去重逻辑，避免 Stop
  hook 请求继续后的收尾再播一次。
- hooks.json 里全部 hook 配了 `async: true`（后台执行），播报绝不阻塞代理回合。

表情展示 `expressionDuration` 秒（默认 15）后自动回落到默认表情。

**表情自动回落**：事件表情默认保持 15 秒（`expressionDuration` 可调）后
自动回到默认表情——默认表情也是动态解析的（优先名字含「默认/正常/default」
的表情，否则取列表第一个），不同皮肤不用改配置。

**可扩展性 / 无表情皮肤**：各版本猫的表情集并不相同，有些 mver 皮肤甚至
没做表情素材。插件对此逐层降级：mver driver 检测模型目录无表情素材时
如实上报不支持（`set_expression` 返回 `supported: false`）；notify.py 拿到
空表情列表或关键词匹配不到时跳过表情、只播气泡。换一套表情名完全不同的
皮肤时，在 `bongocat-notify.json` 里覆盖对应事件的 `expressions` 关键词即可：

```json
{
  "events": {
    "stop": { "expressions": ["celebrate", "thumbs", "happy"] },
    "approval": { "expressions": ["confused", "question"] }
  }
}
```

## 工作原理

```
Codex hook ──process──> notify.py ──HTTP──> bongocat-mcp 仪表盘 /api/tool ──> 猫
Codex MCP  ──stdio──> server.py（bongocat-mcp）──────────────────────────> 猫
```

- hook 走**仪表盘 HTTP API**（`http://127.0.0.1:8766`）：仪表盘常驻持有
  mver 镜像线程，hook 进程自身不建 driver，避免每次事件多出一条 60fps 镜像。
  **因此需要仪表盘保持运行**：`python dashboard.py`（可 `--stop` 停止）。
- 智能体直接控制猫走 **MCP stdio**（另一条独立 driver 实例，双镜像是良性叠加）。
- hook 任何失败（仪表盘没开 / 猫下线）都记入 `hooks/notify.log` 后静默退出，
  绝不阻塞 Codex 会话（Codex 对 hook 的 stdout 按 JSON 严格解析，空输出 =
  放行，脚本保证零输出）。

## 安装（本机）

1. 确保猫和仪表盘在跑：`python dashboard.py`
2. 添加本地市场并安装插件：
   ```
   codex plugin marketplace add D:\pycharm\pycharmprojects\bongocat-mcp\codex-plugin
   codex plugin install bongocat-notify@bongocat-local
   ```
   或在 Codex CLI 里用 `/plugin` 命令从上面的市场安装 **bongocat-notify**。
3. **信任 hooks（关键步骤）**：Codex 对插件捆绑的 hooks 有信任审查——运行
   `/hooks`，逐条 review 后 **Trust** 这 5 个 hook，否则它们不会执行。
4. 新开会话验证：`codex mcp list` 应显示 `bongo-cat`；对猫说「跑一下
   bongo-test 技能」让猫表演；提交任意任务等 `Stop` 事件，看猫气泡 + 表情。

> 与 Claude Code 不同，hooks.json 由插件清单（`.codex-plugin/plugin.json` 的
> `hooks` 字段）直接捆绑，无需把脚本路径写进 `~/.codex/hooks.json`；
> hook 命令里的 `${PLUGIN_ROOT}` 由 Codex 展开为插件安装目录。

## 配置

默认值内置在 `hooks/notify.py` 的 `DEFAULTS`，可在以下任一位置放
`bongocat-notify.json` 覆盖（深合并，后者优先级低）：

1. `hooks/bongocat-notify.json`（插件目录内，随插件分发）
2. `~/.codex/bongocat-notify.json`

```json
{
  "dashboardUrl": "http://127.0.0.1:8766",
  "httpTimeout": 5,
  "expressionDuration": 15,
  "events": {
    "user-prompt": { "enabled": false },
    "stop": {
      "text": "🎉 搞定！{tool}",
      "expressions": ["点赞", "星星眼"]
    }
  }
}
```

`text` 支持 `{tool}` 占位符（审批/出错事件替换为工具名）；觉得每条
`UserPromptSubmit` 都播报太吵就把它 `enabled: false`。

## 不用插件 hook 的轻量替代（可选）

若只需要「任务完成」一个事件，也可不用本插件的 hooks，直接在
`~/.codex/config.toml` 配 `notify`（Codex 会以 JSON 作 argv[1] 调起程序，
目前仅支持 `agent-turn-complete` 事件；注意项目级 config 不能覆盖此键）。
插件 hooks 覆盖的事件更全，二者二选一即可。

## 本机硬编码路径

`.mcp.json` 与 `hooks/hooks.json` 写死了本项目 venv 的 python 路径
（`D:\pycharm\pycharmprojects\bongocat-mcp\.venv\Scripts\python.exe`）。
换机器时改这两处即可；`notify.py` 本身只用标准库，任意 python 3.10+ 都能跑。
