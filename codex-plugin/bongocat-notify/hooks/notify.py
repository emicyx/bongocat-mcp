"""bongocat-notify hook：Codex 关键事件 → 猫咪气泡播报 + 表情切换。

与 ZCode 版（zcode-plugin/bongocat-notify/hooks/notify.py）同源，Codex 的事件
模型与 ZCode 几乎一一对应（改代码前请先读完）：

- Codex 原生就有 PermissionRequest 事件（stdin JSON 带 tool_name），
  等待审批无需像 Claude Code 版那样从 Notification 的 message 里过滤。
- Codex 没有 PostToolUseFailure 事件：工具出错要靠 PostToolUse 的
  tool_response 启发式判定（is_error/isError 标记，或文本以 Error/Exit code
  开头），宁可漏报不可误报，判不出错就静默退出。
- Stop 事件同样有 stop_hook_active 字段，沿用 ZCode 版的去重逻辑。
- hooks.json 里全部 hook 配了 async: true（后台执行不阻塞回合），本脚本
  依旧保持同步逻辑 + 秒级超时，两种运行方式行为一致。

其余设计约束与 ZCode 版一致：
- 只用 Python 标准库。hook 由 Codex 以独立进程拉起，通过 bongocat-mcp
  仪表盘的 HTTP API（POST /api/tool）驱动猫——仪表盘常驻持有 mver 镜像线程，
  hook 进程自身不创建 driver，避免每次事件多出一条 60fps 镜像。
- hook 绝不能干扰会话：任何失败（仪表盘没开 / 猫下线 / 配置坏了）都
  记日志后静默 exit 0，不向 stdout 输出任何东西（Codex 对 hook 的 stdout
  按 JSON 严格解析，空输出 = 放行；UserPromptSubmit / SessionStart 的
  stdout 还会被注入上下文，空输出才安全）。
- 表情不写死索引：每次从 get_cat_status 实时读表情列表，按事件的关键词
  优先级匹配名字（换皮肤 / 换猫自动适配），匹配不到就只出气泡不切表情。
- 表情带 expressionDuration 秒的自动回落（服务端 dispatch 调度，回到
  动态解析的默认表情），事件结束后猫不会一直顶着上一个表情。

用法：notify.py <session-start|user-prompt|stop|approval|failure>
事件 JSON 由 Codex 从 stdin 喂入（字段不保证存在，一律防御性读取）。
配置覆盖：插件目录或 ~/.codex/ 下的 bongocat-notify.json（见 DEFAULTS）。
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent / "notify.log"
CONFIG_NAME = "bongocat-notify.json"

# 事件 → 默认文案（{tool} 会被替换为工具名）与表情关键词（按优先级）。
# 关键词做「包含」匹配，例如列表里的「星星眼」能命中皮肤注释名「星星眼」。
DEFAULTS = {
    "dashboardUrl": "http://127.0.0.1:8766",
    "httpTimeout": 5,
    # 表情保持秒数：事件表情展示这么久后自动回到默认表情（0=一直保持到下个事件）
    "expressionDuration": 15,
    "events": {
        "session-start": {
            "enabled": True,
            "text": "🐱 Codex 已就位，随时可以开工喵！",
            "expressions": ["默认", "正常", "放松", "default"],
        },
        "user-prompt": {
            "enabled": True,
            "text": "🐾 收到新任务，本喵开始干活！",
            "expressions": ["放松", "默认", "认真", "default"],
        },
        "stop": {
            "enabled": True,
            "text": "✅ 任务完成啦！Codex 已交出结果，快来看看喵～",
            "expressions": ["星星眼", "点赞", "开心", "高兴", "笑", "star", "happy"],
        },
        "approval": {
            "enabled": True,
            "text": "❓ Codex 在等你的审批：{tool}需要确认，喵？",
            "expressions": ["疑问", "疑惑", "问号", "?"],
        },
        "failure": {
            "enabled": True,
            "text": "💦 有工具出错了（{tool}），Codex 正在想办法喵…",
            "expressions": ["哭泣", "哭", "生气", "眩晕", "晕", "cry", "angry"],
        },
    },
}


def log(message: str) -> None:
    """追加一行日志；超 256KB 截断，避免长年运行无限膨胀。"""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 256 * 1024:
            LOG_FILE.write_text("", encoding="utf-8")
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    for candidate in (
        Path(__file__).resolve().parent / CONFIG_NAME,
        Path.home() / ".codex" / CONFIG_NAME,
    ):
        try:
            if candidate.exists():
                return _merge(DEFAULTS, json.loads(candidate.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return DEFAULTS


def read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def api_tool(cfg: dict, cmd: str, payload: dict):
    """POST /api/tool；返回解析后的 JSON，失败返回 None。"""
    url = cfg["dashboardUrl"].rstrip("/") + "/api/tool"
    body = json.dumps({"cmd": cmd, "payload": payload}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg["httpTimeout"]) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"api_tool({cmd}) 失败: {type(exc).__name__}: {exc}")
        return None


def fetch_expressions(cfg: dict) -> list[dict]:
    status = api_tool(cfg, "status", {})
    if not isinstance(status, dict) or not status.get("ok"):
        return []
    info = (status.get("data") or {}).get("modelInfo") or {}
    exprs = info.get("expressions") or []
    return [e for e in exprs if isinstance(e, dict) and isinstance(e.get("index"), int)]


def match_expression(exprs: list[dict], keywords: list[str]) -> int | None:
    """按关键词优先级做名字包含匹配，返回 index；找不到返回 None。"""
    for keyword in keywords:
        for expr in exprs:
            if keyword.lower() in str(expr.get("name", "")).lower():
                return expr["index"]
    return None


def response_is_error(resp) -> bool:
    """从 PostToolUse 的 tool_response 启发式判定是否出错（保守：宁可漏报）。

    Codex 各工具的错误形态不统一：MCP 工具发回 MCP call result（dict 带
    is_error/isError），Bash 失败是 "Exit code N" 开头的文本，其余多为
    "Error..." 开头。判不出错就静默跳过，不误报打扰。
    """
    if resp is None:
        return False
    if isinstance(resp, dict):
        if resp.get("is_error") is True or resp.get("isError") is True:
            return True
        text = json.dumps(resp, ensure_ascii=False)
    else:
        text = str(resp)
    compact = re.sub(r"\s+", "", text).lower()
    if '"is_error":true' in compact or '"iserror":true' in compact:
        return True
    head = text.lstrip()[:80].lower()
    return head.startswith("error") or head.startswith("exit code")


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    event = sys.argv[1]

    cfg = load_config()
    event_cfg = cfg["events"].get(event)
    if not isinstance(event_cfg, dict) or not event_cfg.get("enabled", True):
        return 0

    stdin = read_stdin_json()
    # stop_hook_active=true 表示这次 Stop 是上一轮 Stop hook 请求继续后的收尾，
    # 再播报一次会重复打扰
    if event == "stop" and stdin.get("stop_hook_active"):
        return 0

    tool_name = str(stdin.get("tool_name") or stdin.get("tool") or "").strip()

    if event == "failure":
        if not response_is_error(stdin.get("tool_response")):
            return 0

    tool_part = f"「{tool_name}」" if tool_name else "某项操作"

    try:
        text = str(event_cfg.get("text", "")).format(tool=tool_part)
    except (KeyError, IndexError, ValueError):
        text = DEFAULTS["events"][event]["text"].format(tool=tool_part)
    if not text:
        return 0

    # 先气泡（瞬时反馈），再切表情（mver 轻触热键约需 0.5s，由仪表盘承担）
    bubble = api_tool(cfg, "show-bubble", {"text": text})
    if bubble is None:
        # 仪表盘不可达：链路已断，切表情也必然失败，直接静默退出
        return 0

    keywords = event_cfg.get("expressions") or []
    if keywords:
        index = match_expression(fetch_expressions(cfg), keywords)
        if index is not None:
            api_tool(cfg, "set-expression", {
                "index": index,
                "duration": cfg.get("expressionDuration", 15),
            })
        else:
            log(f"事件 {event}: 表情列表中没有匹配 {keywords} 的条目，仅显示气泡")

    log(f"事件 {event}: 已播报「{text}」")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 通知链路绝不向会话抛错
        log(f"未捕获异常: {type(exc).__name__}: {exc}")
        sys.exit(0)
