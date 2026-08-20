# astrbot_plugin_bongocat

QQ 消息经桌宠猫（BongoCat）气泡/表情转述：私聊与命中规则的消息逐条播报，
群聊出可配置摘要，让桌面上的猫成为 QQ 消息的"播报员"。

由 [bongocat-mcp](../../README.md) 仪表盘驱动——插件只做 HTTP 调用，
不创建 driver，猫的谱系差异（embedded/cdp/mver）、表情资产识别、
表情自动回落全部由 bongocat-mcp 承担。设计文档：
[docs/astrbot-integration.md](../../docs/astrbot-integration.md)。

## 前置条件

1. bongocat-mcp 仪表盘在跑：`python dashboard.py`（默认 `127.0.0.1:8766`，
   猫也被它接管驱动）。仪表盘/猫不在线时插件静默跳过，不影响机器人收发消息。
2. AstrBot ≥ v4.0（开发基线 v4.25.1，aiocqhttp + NapCat 实测环境）。
3. 无额外 pip 依赖（HTTP 用 AstrBot 自带 aiohttp）。

## 消息路由（三车道）

```
私聊 ────────────────▶ 逐条气泡：[私] 昵称: 内容
群消息：
  命中直述规则（@bot / 回复 bot / direct_keywords / 白名单成员）
       ─────────────▶ 逐条气泡：[群·群名] 昵称: 内容
  其余 ─────────────▶ 按群缓冲，触发时出摘要：
                       [群名] 群摘要·N条｜张三×5、李四×4｜最新「…」
```

摘要触发条件（OR，全部可配）：攒够 `trigger_count` 条 / 群静默
`trigger_idle_sec` 秒 / `hot_window_sec` 内达到 `hot_count` 条（热度突发）/
手动指令 `/bongocat_digest <群号|all>`。触发后有 `cooldown_sec` 冷却，
缓冲溢出时无视冷却强制摘要。

## 关键配置（WebUI 插件面板）

| 组 | 作用 |
|---|---|
| `routing` | 私聊/群开关、忽略 `/` 命令、@bot 直述、回复直述、直述关键词、永远直述的成员 |
| `lists` | 黑白名单（群 / 私聊发送者 / 群内成员），黑名单优先，白名单留空=放行全部 |
| `digest` | 摘要模式（plain 零成本 / llm 一句话，失败回退）、四类触发参数、冷却、缓冲上限 |
| 顶层 | 仪表盘地址、直述节流间隔、气泡时长、表情时长 |

## 指令

| 指令 | 作用 |
|---|---|
| `/bongocat_status` | 猫控链路状态：driver、在线性、有无表情能力、摘要模式、各群待摘要缓冲 |
| `/bongocat_digest <群号\|all>` | 手动触发群摘要 |

## 与 AstrBot 原生配置的分工（重要）

- **原生 `platform_settings.rate_limit` / `id_whitelist` 是全 bot 级上游闸门**
  （同时约束所有插件与机器人回复），**不要**用它们来调猫的播报频率——
  会连带限制机器人本身。
- 猫播报频率的细粒度控制全部在本插件 `digest` 配置里。
- 原生唤醒配置（唤醒前缀、@设置）管不到本插件的触发（插件 filter 通过
  即放行管线），但也不会误触 LLM 回复（LLM 闸门是 `is_at_or_wake_command`）。

## 表情说明

表情**不写死索引**：每次实时读当前猫的表情列表按名字关键词匹配
（如消息含"哈哈"→ 找"星星眼/开心/笑"），换皮肤/换猫自动适配；
当前猫没有表情素材（部分 mver 皮肤）时自动降级为仅气泡，每 10 分钟重查。

## 开发

本目录是源码，通过目录连接（junction）部署到
`<AstrBot>/data/plugins/astrbot_plugin_bongocat`，改完在 WebUI 插件管理里
"重载"即可。离线单元测试（stub 掉 astrbot 模块，验证路由/摘要/降级逻辑）：

```bash
python ../../astrbot-plugin/tests/test_bongocat_plugin.py   # 在 bongocat-mcp 仓库内
```

真机验收清单见设计文档 §8（涉及真实 QQ 协议，需两个 QQ 账号实测）。
