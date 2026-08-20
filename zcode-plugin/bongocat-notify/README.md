# bongocat-notify（ZCode 插件）

让桌面 BongoCat 猫成为 ZCode 的任务播报员：**任务完成 / 等待审批 / 工具出错**
等关键时刻，猫头顶弹出气泡告诉用户任务状态，并按表情列表切换对应表情；
同时把 bongocat-mcp 注册为 MCP 服务器，让智能体能直接操控猫咪（`/bongo-test`）。

## 提供什么

| 组件 | 内容 |
|---|---|
| MCP 服务器 | `bongo-cat`（stdio 拉起 bongocat-mcp 的 `server.py`，工具名 `mcp__bongo-cat__*`） |
| `/bongo-test` 命令 | 走 MCP 工具全链路自检：ping → 状态 → 表情切换 → 气泡 |
| hooks | 5 个事件的猫咪播报（见下表） |

## 事件 → 猫的行为

| ZCode 事件 | 触发时机 | 气泡文案（默认） | 表情关键词（按优先级） |
|---|---|---|---|
| `SessionStart` | 会话启动/恢复 | 🐱 ZCode 已就位，随时可以开工喵！ | 默认 / 放松 |
| `UserPromptSubmit` | 用户提交新任务 | 🐾 收到新任务，本喵开始干活！ | 放松 / 默认 |
| `Stop` | **任务完成** | ✅ 任务完成啦！ZCode 已交出结果… | 星星眼 / 点赞 |
| `PermissionRequest` | **等待审批** | ❓ ZCode 在等你的审批：「工具」需要确认… | 疑问 |
| `PostToolUseFailure` | 工具执行出错 | 💦 有工具出错了（「工具」）… | 哭泣 / 生气 / 眩晕 |

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
ZCode hook ──process──> notify.py ──HTTP──> bongocat-mcp 仪表盘 /api/tool ──> 猫
ZCode MCP  ──stdio──> server.py（bongocat-mcp）──────────────────────────> 猫
```

- hook 走**仪表盘 HTTP API**（`http://127.0.0.1:8766`）：仪表盘常驻持有
  mver 镜像线程，hook 进程自身不建 driver，避免每次事件多出一条 60fps 镜像。
  **因此需要仪表盘保持运行**：`python dashboard.py`（可 `--stop` 停止）。
- 智能体直接控制猫走 **MCP stdio**（另一条独立 driver 实例，双镜像是良性叠加）。
- hook 任何失败（仪表盘没开 / 猫下线）都记入 `hooks/notify.log` 后静默退出，
  绝不阻塞 ZCode 会话。

## 安装（本机）

1. 确保猫和仪表盘在跑：`python dashboard.py`（免安装打包版：双击
   `bongocat-mcp.exe` 即为仪表盘）
2. Zcode → 设置 → 插件管理 → 发现 → `+` 添加市场，选择本地目录：
   `D:\pycharm\pycharmprojects\bongocat-mcp\zcode-plugin`
3. 在市场中安装 **bongocat-notify**（新插件默认启用，hooks 自动激活）
4. 新开会话验证：设置 → MCP 应显示 `bongo-cat` 已连接；跑 `/bongo-test`
   让猫表演；提交任意任务等 `Stop` 事件，看猫气泡 + 表情

## 配置

默认值内置在 `hooks/notify.py` 的 `DEFAULTS`，可在以下任一位置放
`bongocat-notify.json` 覆盖（深合并，后者优先级低）：

1. `hooks/bongocat-notify.json`（插件目录内，随插件分发）
2. `~/.zcode/bongocat-notify.json`

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

## 本机硬编码路径

`.mcp.json` 与 `hooks/hooks.json` 写死了本项目 venv 的 python 路径
（`D:\pycharm\pycharmprojects\bongocat-mcp\.venv\Scripts\python.exe`）。
换机器时改这两处即可；`notify.py` 本身只用标准库，任意 python 3.10+ 都能跑。
