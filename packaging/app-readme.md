# bongocat-mcp 打包版（免安装）

解压即用的 BongoCat 猫控制器：本地仪表盘 + MCP 控制器，**无需安装 Python**。

## 快速开始

1. 解压整个文件夹到任意可写目录（如 `D:\bongocat-mcp`），不要只拷出 exe
2. 双击 `bongocat-mcp.exe`——首次运行自动在 exe 旁生成 `config.json`，
   随后仪表盘转入后台运行并自动打开浏览器（`http://127.0.0.1:8766`）
3. 黑色窗口一闪而过是正常现象（它把自身转成了隐藏后台进程）

停止后台服务：命令行运行 `bongocat-mcp.exe stop`（或任务管理器结束进程）。
卸载 = 停止后删除整个文件夹，不写注册表、不留系统残留。

## 三种运行模式

| 命令 | 等价源码方式 | 用途 |
|---|---|---|
| `bongocat-mcp.exe` | `python dashboard.py` | 仪表盘（日常使用，推荐保持运行） |
| `bongocat-mcp.exe server` | `python server.py` | MCP stdio server，供 MCP client 拉起 |
| `bongocat-mcp.exe mirror` | `python mver-mirror.py` | 只让接收模式的 Mver 恢复键鼠跟随（不开 AI） |

## 接入 MCP client（astrbot / ZCode / Claude Code / Codex）

client 侧把 stdio server 配置为：

```json
{
  "command": "D:\\bongocat-mcp\\bongocat-mcp.exe",
  "args": ["server"]
}
```

（路径换成你的实际解压位置。）插件的气泡播报走仪表盘 HTTP API，
保持 `bongocat-mcp.exe`（仪表盘模式）在后台运行即可。

## 配置

配置就是 exe 旁的 `config.json`，推荐直接在仪表盘网页里改；
也可用环境变量 `BONGOCAT_*` 临时覆盖（优先级更高）。

## 注意

- 仅支持 Windows 10/11 x64；猫本体（BongoCat / BongoCatMver）需自行安装
- **不要把 exe 改名成含 "bongo" 且不含 "mcp" 的名字**（如 `bongocat.exe`）：
  控制器按进程名识别猫，改错名会被自己的 cdp 接管逻辑误当成猫结束掉
- exe 未做代码签名，个别杀软可能提示未知发布者：请自行判断是否放行
- 升级：停止后台进程后，用新版文件夹整个替换（可先把旧 `config.json` 拷回来）
