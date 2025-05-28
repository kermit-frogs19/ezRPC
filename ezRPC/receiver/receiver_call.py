from ezh3.server import ServerRequest
import msgpack
import msgspec

from ezRPC.common.config import DEFAULT_PATH, StandardCallFormat


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

    def list(self) -> list:
        if self.data is None:
            self._process_body()

        return self.data.to_list()

    def get_function_name(self) -> str:
        unpacked = msgpack.unpackb(self.data.raw, raw=False)
        return unpacked.get("f")

    def get_function_name_new(self) -> str:
        unpacked = msgpack.unpackb(self.data.raw, raw=False)
        return unpacked[0]

    def _process_headers(self) -> None:
        if self.headers:
            return

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
        self.function_name: str | None = None
        self.call_type: int | None = None
        self.args: list | None = None

    def to_dict(self) -> dict:
        return {
            "f": self.function_name,
            "t": self.call_type,
            "a": self.args,
        }

    def to_list(self) -> list:
        return [
            self.function_name,
            self.call_type,
            self.args
        ]

    def to_msgpack(self) -> bytes:
        return msgpack.packb(self.to_list(), use_bin_type=True)

    def from_msgspec_struct(self, instance: StandardCallFormat) -> None:
        self.function_name = instance.function_name
        self.call_type = instance.call_type
        self.args = instance.args

    def from_dict(self, data: dict) -> None:
        self.function_name = data["f"]
        self.call_type = data["t"]
        self.args = data["a"]

    def from_list(self, data: list) -> None:
        self.function_name = data[0]
        self.call_type = data[1]
        self.args = data[2]














