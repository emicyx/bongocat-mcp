import enum


class EventMessageType(enum.Flag):
    GROUP_MESSAGE = enum.auto()
    PRIVATE_MESSAGE = enum.auto()
    OTHER_MESSAGE = enum.auto()
    ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE


class _Filter:
    EventMessageType = EventMessageType

    def event_message_type(self, event_type, priority: int = 0):
        def deco(fn):
            fn._stub_event_message_type = event_type
            return fn
        return deco

    def command(self, name: str, priority: int = 0):
        def deco(fn):
            fn._stub_command = name
            return fn
        return deco


filter = _Filter()


class AstrMessageEvent:  # 类型占位；测试用 tests 内的 FakeEvent
    pass
