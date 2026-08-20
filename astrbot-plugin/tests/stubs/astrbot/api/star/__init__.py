class Context:
    def __init__(self, provider=None) -> None:
        self._provider = provider

    def get_using_provider(self):
        return self._provider


class Star:
    def __init__(self, context: Context | None = None) -> None:
        self.context = context
