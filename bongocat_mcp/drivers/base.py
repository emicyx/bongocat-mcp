"""CatDriver 抽象：MCP 工具面统一调度到不同 lineage 的猫。"""

from __future__ import annotations

from abc import ABC, abstractmethod

# 命令名与 embedded HTTP 接口保持一致
CMD_PING = "ping"
CMD_STATUS = "status"
CMD_SET_EXPRESSION = "set-expression"
CMD_PLAY_MOTION = "play-motion"
CMD_PRESS_KEY = "press-key"
CMD_RELEASE_KEY = "release-key"
CMD_TYPE_TEXT = "type-text"
CMD_SET_HAND = "set-hand"
CMD_SET_PARAMETER = "set-parameter"
CMD_SHOW_BUBBLE = "show-bubble"
CMD_HIDE_BUBBLE = "hide-bubble"
CMD_SET_WINDOW_VISIBLE = "set-window-visible"

ALL_COMMANDS = {
    CMD_PING, CMD_STATUS, CMD_SET_EXPRESSION, CMD_PLAY_MOTION,
    CMD_PRESS_KEY, CMD_RELEASE_KEY, CMD_TYPE_TEXT, CMD_SET_HAND,
    CMD_SET_PARAMETER, CMD_SHOW_BUBBLE, CMD_HIDE_BUBBLE,
    CMD_SET_WINDOW_VISIBLE,
}


class DriverError(Exception):
    """driver 层错误，message 面向 LLM 可读。"""


class CatDriver(ABC):
    """一只可控的猫。

    call(cmd, payload) 返回 dict，至少含 ok 字段；
    不支持的命令应体现在 capabilities() 而不是 call 里抛错。
    """

    name = "base"

    @abstractmethod
    def capabilities(self) -> set[str]:
        raise NotImplementedError

    @abstractmethod
    def call(self, cmd: str, payload: dict) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        pass

    # ---- 便捷封装 ----

    def ping(self) -> dict:
        return self.call(CMD_PING, {})

    def status(self) -> dict:
        return self.call(CMD_STATUS, {})
