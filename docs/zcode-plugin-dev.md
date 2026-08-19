# 接入开发指南：为智能体客户端开发 BongoCat 插件

面向想在 ZCode / AstrBot / 其他 MCP client 上开发"猫播报"插件的开发者。
参考实现：[ZCode 插件 bongocat-notify](../zcode-plugin/bongocat-notify/)（已验证可用），
AstrBot 侧的方案论证见 [astrbot-integration.md](astrbot-integration.md)。

## 1. 三条接入通道，按场景选

| 通道 | 适用 | 说明 |
|---|---|---|
| **仪表盘 HTTP**（推荐给 hook/事件驱动） | `POST http://127.0.0.1:8766/api/tool`，body `{"cmd": "...", "payload": {...}}` | 最轻量：任意语言、零依赖。仪表盘常驻持有 mver 镜像线程，调用方不需要建 driver，也就不会多出一条 60fps 发送端。**要求仪表盘在运行**（`python dashboard.py`） |
| **MCP stdio**（推荐给 LLM 主动控制） | 拉起 `server.py`，工具名 `mcp__bongo-cat__*` 共 14 个 | 自然语言 → 工具调用的场景。每次会话一个独立 driver 实例（mver 下与仪表盘双镜像，良性叠加） |
| **Python 直调** | `from bongocat_mcp.dispatch import dispatch` | 与仪表盘/stdio 共用同一调度层（能力门控 + 失效重探测），适合写测试脚本 |

命令清单与各 driver 能力矩阵见 [README](../README.md#mcp-tools)。事件日志
`GET /api/events` 返回最近 200 条调用记录（含自动回落），是排查"到底发没发出去"的第一入口。

## 2. 有时效语义的两个命令

通知类插件的核心命令都带 `duration`（秒）：

- `show-bubble` `{"text": ..., "duration": 8}` —— 打字动画播完后停留 `duration` 秒自动消失；`duration: 0` 常驻直到 `hide-bubble`
- `set-expression` `{"index": ..., "duration": 15}` —— 到期自动回落到**默认表情**（动态解析：优先名字含「默认/正常/default」的表情，否则取列表第一个）；`duration: 0` 一直保持

回落逻辑在共享 dispatch 层实现，三种 driver 行为一致；新的 `set-expression` 会自动作废上一次未到期的回落。

## 3. 表情的可扩展性约定（重要）

**各版本猫的表情集并不相同，有些 mver 皮肤甚至没做表情素材。** 插件侧必须：

1. **实时读列表，别写死索引**：每次事件先 `status`（或 MCP `get_cat_status`）拿
   `modelInfo.expressions`（含 `index` / `name`），按事件的**关键词优先级**对名字做包含匹配。
   例如任务完成 → `["星星眼", "点赞", "开心", "happy"]`，等待审批 → `["疑问", "疑惑", "?"]`。
2. **逐层降级**：皮肤没有表情素材时 mver driver 会如实返回 `supported: false`；
   关键词匹配不到时跳过表情只出气泡。任何一层失败都不应报错阻塞调用方。
3. **关键词可配置**：参考实现的 `bongocat-notify.json` 允许按事件覆盖关键词列表，
   换英文命名的皮肤时只改配置不改代码。
4. mver 的表情绑定来自皮肤 `config.json` 的 `l2d_expression` 键位（Alt+字母），
   driver 通过「轻触和弦」间接触发，猫爪几乎不做可视动作。

## 4. ZCode 插件骨架（最小可用）

```
my-marketplace/                  # 本地市场目录（ZCode → 发现 → + 添加）
  marketplace.json               # {"name": ..., "plugins": [{"name": "my-plugin", "source": "my-plugin"}]}
  my-plugin/
    .zcode-plugin/plugin.json    # 清单：name + commands/skills/hooks/mcpServers
    .mcp.json                    # MCP 服务器注册（stdio 拉起 server.py）
    hooks/hooks.json             # 事件 → 脚本
    hooks/notify.py              # 播报脚本（见下）
```

**hooks.json 要点**（ZCode 只有 7 个事件：`SessionStart` / `UserPromptSubmit` /
`PreToolUse` / `PermissionRequest` / `PostToolUse` / `PostToolUseFailure` / `Stop`）：

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "process",
        "command": "<python 绝对路径>",
        "args": ["${ZCODE_PLUGIN_ROOT}\\hooks\\notify.py", "stop"],
        "timeoutMs": 15000
      }]
    }]
  }
}
```

**notify.py 模式**（完整实现见参考插件；只用标准库）：

1. 读 stdin JSON（防御式，字段可能缺失）取 `tool_name` 等；
2. `POST /api/tool` `show-bubble` → 气泡文案（支持 `{tool}` 占位符）；
3. `status` 读表情列表 → 关键词匹配 → `set-expression`（带 `duration`）；
4. **任何失败记日志后 exit 0 且不向 stdout 输出**——hook 的 stdout 会被客户端
   按 JSON schema 严格解析，空输出才是安全默认；通知链路绝不能阻塞会话。

## 5. 验证方法论（血泪教训）

- **猫的视觉状态以人眼为准。** 截图 + 视觉模型判读在本次开发中多次把已生效的
  星星眼判成普通眼，把常驻发饰判成表情，误导排查方向整整一轮。
- 气泡的显示/消失可以用**窗口几何**客观验证：枚举 `TkTopLevel` 窗口，
  在屏坐标 = 显示中，钉在 `-32000`（DPI 缩放后报告为 -25600）= 已隐藏。
- 命令是否发出/回落是否触发看 `GET /api/events` 的事件流水。
- 截图验证脚本务必先 `SetProcessDPIAware()`，否则窗口坐标被虚拟化、裁剪区域错位。

## 6. 已知坑（mver 环境）

| 坑 | 现象 | 规避 |
|---|---|---|
| 多个 UDP 发送端 | 每个持有 driver 的进程都是一条 60fps 镜像；对「跟随键鼠」是良性叠加，但会互相冲掉和弦时序，表情触发变得不稳定 | 保持**单一常驻发送端**（通常是仪表盘）；`mver-mirror.py` 与仪表盘二选一 |
| 从 Git Bash 拉起隐藏进程 | MSYS 句柄继承会让子进程里的 win32 枚举/tasklist 挂死，表现为 status 超时、气泡渲染停滞 | 后台拉起用 `powershell Start-Process`，或直接在正常终端运行 |
| tk `after` 定时器自我复制 | `try` 体内提前 `return` 的分支若也 `root.after()` 续期，`finally` 再续一次 → 每 30ms 定时器数量翻倍，几秒内队列淹没 | 续期只允许写在 `finally` 里（已修复，改 overlay 时勿回退） |
| driver 重启闪断 | 仪表盘停止/重启期间猫失去键鼠跟随（接收模式只认网络帧） | 重启后镜像自动恢复，属预期行为；避免频繁重启 |
| 和弦时序 | 组合键绑定要求修饰键先按住 ≥0.3s 再按触发键，同时按下不触发 | 用 driver 自带的 `_tap_chord`，不要自己拼帧 |

## 7. mver 接收模式的代价（部署前必读）

Mver 开网络接收后会**忽略本机键鼠**，只渲染网络包，因此镜像线程成为猫的
唯一输入来源：控制进程全部停止时猫会"冻住"不再跟随。部署任何常驻插件时，
确保有一个（且只有一个）常驻发送端在跑。
