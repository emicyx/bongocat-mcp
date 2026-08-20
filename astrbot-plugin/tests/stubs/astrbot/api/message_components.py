"""消息组件桩：属性名与 astrbot.api.message_components（Nakuru 风格）一致。"""


class Base:
    pass


class Plain(Base):
    def __init__(self, text: str = "") -> None:
        self.text = text


class At(Base):
    def __init__(self, qq="", name="") -> None:
        self.qq = qq
        self.name = name


class AtAll(Base):
    pass


class Image(Base):
    def __init__(self, url: str = "") -> None:
        self.url = url


class Record(Base):
    def __init__(self, url: str = "") -> None:
        self.url = url


class Video(Base):
    pass


class File(Base):
    pass


class Face(Base):
    def __init__(self, face_id=-1) -> None:
        self.id = face_id


class Reply(Base):
    def __init__(self, sender_id="", chain_id="", chain=None) -> None:
        self.sender_id = sender_id
        self.chain_id = chain_id
        self.chain = chain
