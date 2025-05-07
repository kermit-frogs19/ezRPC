from ezh3.server import ServerRequest
import msgpack
import msgspec

from ezRPC.common.config import DEFAULT_PATH


class ReceiverCall(ServerRequest):
    def __init__(
            self,
            body: bytes = b"",
            path: str = DEFAULT_PATH,
            raw_headers: list = None,
            content_type: str = None,
            headers: dict = None
    ) -> None:
        super().__init__(
            method="POST",
            body=body,
            path=path,
            raw_headers=raw_headers,
            content_type=content_type,
            headers=headers
        )
        self.data: ReceiverCallData | None = None

    @property
    def text(self) -> str:
        return str(self.json())

    def json(self) -> dict:
        if self.data is None:
            self._process_body()

        return self.data.to_dict()

    def get_function_name(self) -> str:
        unpacked = msgpack.unpackb(self.data.raw, raw=False)
        return unpacked.get("f")

    def _process_headers(self) -> None:
        if self.raw_headers:
            headers = {str(k.decode()).replace(":", ""): v.decode() for k, v in self.raw_headers}

            if "path" in headers and not self.path:
                self.path = headers["path"]
            if "method" in headers:
                self.method = headers["method"]

        else:
            headers = {}

        self.headers = headers

    def _process_body(self) -> None:
        self.data = ReceiverCallData(self.body)


class ReceiverCallData:
    def __init__(
            self,
            raw: bytes = b""
    ) -> None:
        self.raw = raw
        self.f: str | None = None
        self.a: list | None = None

    def to_msgpack(self) -> bytes:
        return msgpack.packb(self.to_dict(), use_bin_type=True)

    def to_dict(self) -> dict:
        return {
            "f": self.f,
            "a": self.a
        }

    def from_msgspec_struct(self, instance: msgspec.Struct) -> None:
        self.a = instance.a
        self.f = instance.f

    def from_dict(self, data: dict) -> None:
        self.a = data["a"]
        self.f = data["f"]














