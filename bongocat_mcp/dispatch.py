"""命令调度：能力门控 + 连接失效重探测。server.py（MCP）与 dashboard.py 共用。"""

from __future__ import annotations

import itertools
import threading
import time

from bongocat_mcp.detect import resolve_driver
from bongocat_mcp.drivers.base import DriverError

# 事件环形缓冲（仪表盘日志）
_events: list[dict] = []
_events_lock = threading.Lock()
# 单调序号：缓冲填满后裁剪使长度恒定（== EVENTS_MAX），前端不能靠条数判断更新
_seq = itertools.count(1)
EVENTS_MAX = 200

# ---- 表情自动回落 ----
# 通知类调用（如 ZCode 插件的事件播报）set-expression 带 duration>0 时，
# 到期自动切回默认表情，避免事件结束后猫一直顶着上一个表情。
# 回落目标动态解析：不同皮肤表情集不同，优先名字含「默认/正常/default」的
# 表情，否则取 index 0（mver 皮肤约定第一组绑定即默认脸）。
# 皮肤完全没做表情时原始 set-expression 就已失败，不会调度回落。

DEFAULT_EXPR_KEYWORDS = ("默认", "正常", "default")
_revert_gen = 0
_revert_gen_lock = threading.Lock()


def _default_expression_index(driver) -> int | None:
    try:
        info = (driver.call("status", {}).get("data") or {}).get("modelInfo") or {}
    except DriverError:
        return None
    exprs = [e for e in (info.get("expressions") or []) if isinstance(e, dict)]
    for expr in exprs:
        name = str(expr.get("name", "")).lower()
        if any(k in name for k in DEFAULT_EXPR_KEYWORDS):
            return expr.get("index")
    return exprs[0].get("index") if exprs else None


def _schedule_expression_revert(delay: float) -> None:
    """delay 秒后回到默认表情；期间新的 set-expression 会作废本次回落。"""
    global _revert_gen
    with _revert_gen_lock:
        _revert_gen += 1
        gen = _revert_gen

    def _revert() -> None:
        with _revert_gen_lock:
            if gen != _revert_gen:
                return  # 已被更新的表情切换取代
        try:
            index = _default_expression_index(resolve_driver())
        except DriverError:
            return  # 猫下线：放弃本次回落
        if index is None:
            return  # 当前皮肤没有可用表情
        dispatch("set-expression", {"index": index})

    timer = threading.Timer(delay, _revert)
    timer.daemon = True
    timer.start()


def log_event(kind: str, detail: dict) -> None:
    with _events_lock:
        _events.append({
            "seq": next(_seq),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
            "detail": detail,
        })
        if len(_events) > EVENTS_MAX:
            del _events[: len(_events) - EVENTS_MAX]


def recent_events() -> list[dict]:
    with _events_lock:
        return list(_events[-EVENTS_MAX:])


def dispatch(cmd: str, payload: dict) -> dict:
    driver = resolve_driver()

    if cmd not in driver.capabilities():
        result = {
            "ok": False,
            "supported": False,
            "driver": driver.name,
            "error": f"当前猫（driver={driver.name}）不支持 {cmd}",
        }
        log_event("skip", {"cmd": cmd, "payload": payload, "result": result})
        return result

    try:
        result = driver.call(cmd, payload)
    except (KeyError, ValueError, TypeError) as exc:
        # payload 缺字段/类型不符：是调用方问题而非连接失效，不触发重探测
        result = {"ok": False, "error": f"参数不合法（{type(exc).__name__}: {exc}）"}
        log_event("call", {"cmd": cmd, "payload": payload, "result": result})
        return result
    except DriverError:
        # 连接可能失效（猫被关闭/重启），重新探测一次
        try:
            driver = resolve_driver(refresh=True)
            result = driver.call(cmd, payload)
        except DriverError:
            raise

    if isinstance(result, dict):
        result.setdefault("driver", driver.name)

    # 表情自动回落：成功切表情且带正 duration 时调度（<=0/缺省 = 一直保持）
    if cmd == "set-expression" and isinstance(result, dict) and result.get("ok"):
        try:
            duration = float(payload.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration > 0:
            _schedule_expression_revert(duration)

    log_event("call", {"cmd": cmd, "payload": payload, "result": {
        k: v for k, v in result.items() if k != "data"}} if isinstance(result, dict) else result)
    return result
