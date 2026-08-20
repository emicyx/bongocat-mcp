import logging


class _Logger:
    """收集 warning/info 便于断言，同时落到标准 logging。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []
        self._log = logging.getLogger("astrbot.stub")

    def _record(self, level: str, msg: str) -> None:
        self.records.append((level, msg))
        getattr(self._log, level)(msg)

    def info(self, msg: str) -> None:
        self._record("info", msg)

    def warning(self, msg: str) -> None:
        self._record("warning", msg)

    def error(self, msg: str) -> None:
        self._record("error", msg)

    def debug(self, msg: str) -> None:
        pass


logger = _Logger()


class AstrBotConfig(dict):
    """dict 子类桩：真实 AstrBotConfig 同为 dict 子类。"""

    def save_config(self) -> None:
        pass
