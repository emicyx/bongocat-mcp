# 打包：bongocat-mcp 免安装版

把项目打包成**免 Python 环境的单文件夹应用**（PyInstaller onedir），
供用户下载解压后双击 `bongocat-mcp.exe` 直接使用。

## 构建

双击 `build.bat`（或在本目录执行它）。脚本会：

1. 复用项目根的 `.venv`（没有则自动创建并装依赖）
2. 安装 PyInstaller（仅首次）
3. 按 `bongocat-mcp.spec` 打包到 `packaging\dist\bongocat-mcp\`
4. 附上 `README.md`（面向最终用户，来自 `app-readme.md`）
5. 压缩为 `packaging\dist\bongocat-mcp-windows-x64.zip`（发布用）

构建产物（`build\`、`dist\`、zip）均不入库，见根目录 `.gitignore`。
发布时把 zip 上传到 GitHub Releases 即可。

## 打包版的入口设计（为什么不用改源码）

| 源码假设 | 打包后的解析 |
|---|---|
| `config.py` 的 `PROJECT_ROOT` | launcher 设置 `BONGOCAT_MCP_CONFIG_FILE` 指向 exe 旁的 `config.json`（源码原生支持该环境变量） |
| `dashboard.py` 的 `WEB_DIR = Path(__file__).parent / "web"` | `web/index.html` 按同样相对路径打进 `_internal`，PyInstaller 冻结模块的 `__file__` 就在那里 |
| `mver-mirror.py`（模块名带连字符） | 作为数据文件打包，launcher 用 `runpy.run_path` 执行 |
| dashboard / mirror 的「隐藏重启」以自身脚本路径为参数拉起 `sys.executable`（冻结后即 exe） | launcher 识别 `.py`/`.pyc` 路径参数并路由回对应入口 |
| PID 文件写在模块目录 | onedir 下落在 `_internal`，同一安装目录内自洽（`exe stop` 能找到） |

exe 子命令：无参=仪表盘、`server`=MCP stdio、`mirror`=Mver 镜像、`stop`=全部停止。
详见 `launcher.py` 顶部注释与 `app-readme.md`。

> **exe 名字里的 "mcp" 是功能性的**：源码的猫进程识别用 `name_contains="bongo"`
> 且排除词含 `"mcp"`（见 `cdp_webview2.py` / `detect.py`）。把 exe 改名为含
> "bongo" 而不含 "mcp" 的名字，会被 cdp 接管逻辑误当成猫进程 taskkill。

## 打包版与源码版的关系

- **不修改任何现有源码**：全部适配收敛在 `launcher.py` + spec + 数据文件布局
- 两版可并存（配置各管各的：源码版用项目根 `config.json`，打包版用 exe 旁的）
- 默认仪表盘端口相同（8766），同时运行会提示「已在运行」——用哪个版本跑都行
