---
description: 测试 BongoCat MCP 链路：ping、读状态、按名字切表情、气泡播报
---

按顺序执行以下步骤，逐步验证 bongocat-mcp 链路（全部通过 `mcp__bongo-cat__*` 工具调用）：

1. 调用 `ping` 确认链路在线；失败则停止并向用户报告错误内容。
2. 调用 `get_cat_status`，读取 driver、capabilities、模型信息与窗口可见性。
3. 调用 `list_expressions`，向用户展示全部表情的 index 和 name。
4. 依次切换表情做肉眼验证：先 `set_expression` 到「疑问」，隔 2 秒再切到「星星眼」
   （用 Bash `sleep 2` 控制间隔；具体 index 以第 3 步读到的列表为准）。
5. 调用 `show_bubble` 显示「Claude Code 插件链路测试成功喵！」。
6. 汇总每一步的返回 ok / 错误，向用户报告整体结论。

若 MCP 工具不可用（没有 `mcp__bongo-cat__` 前缀的工具），提示用户运行 `/mcp`
查看 bongo-cat 服务器状态（应为 connected）。
