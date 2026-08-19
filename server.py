"""bongocat-mcp：MCP stdio server（供 astrbot 等 MCP client 拉起）。

12 个工具的接口与旧版一致，底层路由见 bongocat_mcp.dispatch。
配置优先级：环境变量 BONGOCAT_* > config.json > 默认值（见 bongocat_mcp.config）。
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from bongocat_mcp.detect import resolve_driver
from bongocat_mcp.dispatch import dispatch
from bongocat_mcp.drivers.base import DriverError

mcp = FastMCP("bongo-cat-mcp")


def _fail(exc: Exception) -> dict:
    return {"ok": False, "error": str(exc)}


@mcp.tool(description="检查当前猫的控制链路是否在线。")
def ping() -> dict:
    """健康检查。"""
    try:
        return dispatch("ping", {})
    except DriverError as exc:
        return _fail(exc)


@mcp.tool(description="获取猫的当前状态：driver、capabilities、模型信息（motions / expressions）与窗口可见性。")
def get_cat_status() -> dict:
    """获取猫咪当前状态。"""
    try:
        result = dispatch("status", {})
        result["capabilities"] = sorted(resolve_driver().capabilities())
        return result
    except DriverError as exc:
        return _fail(exc)


@mcp.tool(description="列出当前 Live2D 模型支持的表情列表，每项含 index。")
def list_expressions() -> list:
    """列出可用表情。"""
    try:
        data = dispatch("status", {})
        info = (data.get("data") or {}).get("modelInfo") or {}
        return info.get("expressions") or []
    except DriverError as exc:
        return [{"ok": False, "error": str(exc)}]


@mcp.tool(description="列出当前模型的可用动作，按组分组。")
def list_motions() -> list:
    """列出可用动作。"""
    try:
        data = dispatch("status", {})
        info = (data.get("data") or {}).get("modelInfo") or {}
        return info.get("motions") or []
    except DriverError as exc:
        return [{"ok": False, "error": str(exc)}]


@mcp.tool(description="设置猫咪的表情。index 为表情索引（从 0 开始）。"
                      "duration 为表情保持秒数，到期自动回到默认表情（动态解析，适配不同皮肤）；"
                      "传 0 表示一直保持直到下次切换。")
def set_expression(index: int, duration: float = 15) -> dict:
    """切换猫咪表情。"""
    try:
        return dispatch("set-expression", {"index": index, "duration": duration})
    except DriverError as exc:
        return _fail(exc)


@mcp.tool(description="播放一个动作（motion）。motion 为对象，可从 list_motions 获取后原样传入。")
def play_motion(motion: dict) -> dict:
    """播放猫咪动作。"""
    try:
        return dispatch("play-motion", {"motion": motion})
    except DriverError as exc:
        return _fail(exc)


@mcp.tool(description="让猫按下某个按键（模拟打字/按键动画）。key 形如 KeyA、Num1、Space。")
def press_key(key: str) -> dict:
    """按下按键。"""
    try:
        return dispatch("press-key", {"key": key})
    except DriverError as exc:
        return _fail(exc)


@mcp.tool(description="松开某个按键。key 形如 KeyA、Num1、Space。")
def release_key(key: str) -> dict:
    """松开按键。"""
    try:
        return dispatch("release-key", {"key": key})
    except DriverError as exc:
        return _fail(exc)


@mcp.tool(description="让猫在键盘上打字。text 为要打的内容，会逐字符显示按键动画。")
def type_text(text: str) -> dict:
    """猫咪打字动画。"""
    try:
        return dispatch("type-text", {"text": text})
    except DriverError as exc:
        return _fail(exc)


@mcp.tool(description="控制猫的左右手是否按下（拍打/打鼓）。left、right 为布尔值。")
def set_hand(left: bool, right: bool) -> dict:
    """控制猫爪下压状态。"""
    try:
        return dispatch("set-hand", {"left": left, "right": right})
    except DriverError as exc:
        return _fail(exc)


@mcp.tool(description="设置 Live2D 参数值。id 为参数名（如 ParamAngleX），value 为数值。")
def set_parameter(id: str, value: float) -> dict:  # noqa: A002 与既有工具签名保持一致
    """设置 Live2D 参数。"""
    try:
        return dispatch("set-parameter", {"id": id, "value": value})
    except DriverError as exc:
        return _fail(exc)


@mcp.tool(description="在猫咪上方显示一条聊天气泡，text 为要显示的内容（带打字机动画）。"
                      "duration 为气泡停留秒数，默认 8 秒后自动消失；传 0 表示一直显示直到调用 hide_bubble。")
def show_bubble(text: str, duration: float = 8) -> dict:
    """显示聊天气泡。"""
    try:
        return dispatch("show-bubble", {"text": text, "duration": duration})
    except DriverError as exc:
        return _fail(exc)


@mcp.tool(description="隐藏当前聊天气泡。")
def hide_bubble() -> dict:
    """隐藏聊天气泡。"""
    try:
        return dispatch("hide-bubble", {})
    except DriverError as exc:
        return _fail(exc)


@mcp.tool(description="显示或隐藏猫咪窗口。visible 为布尔值。")
def set_window_visible(visible: bool) -> dict:
    """显示/隐藏猫咪窗口。"""
    try:
        return dispatch("set-window-visible", {"visible": visible})
    except DriverError as exc:
        return _fail(exc)


if __name__ == "__main__":
    mcp.run()
