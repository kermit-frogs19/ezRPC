from ezh3.client import ClientConnection


class ProducerConnection(ClientConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.headers: dict | None = None

