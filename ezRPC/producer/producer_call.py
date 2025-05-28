import msgpack
import msgspec
from dataclasses import dataclass, field
from typing import Literal


from ezh3.client import ClientRequest
from ezRPC.common.config import CallType



@dataclass
class ProducerCallData:
    function_name: str | None = field(default=None)
    args: tuple | None = field(default=None)
    call_type: int = field(default=CallType.STANDARD_CALL)

    def to_dict(self) -> dict:
        return {
            "f": self.function_name,
            "a": self.args
        }

    def to_list(self) -> list:
        return [
            self.function_name,
            int(self.call_type),
            self.args
        ]

    def to_msgpack(self) -> bytes:
        try:
            return msgpack.packb(self.to_list(), use_bin_type=True)
        except BaseException as e:
            raise ValueError(f"Unsupported types passed when making the call: {self}") from e


@dataclass
class ProducerCall(ClientRequest):
    data: ProducerCallData = field(default=None)

    method: Literal["GET", "POST", "PATCH", "PUT", "DELETE", "CONNECT"] = field(default="POST", init=False)
    charset: str = field(default=None, init=False)
    json: dict = field(default=None, init=False)
    content: bytes = field(default=None, init=False)

    def render_body(self) -> bytes:
        return self.data.to_msgpack()

    def render_headers(self) -> list[tuple]:
        headers = [
            (b":method", self.method.encode(self._header_encoding)),
            (b":scheme", self.url.scheme.encode(self._header_encoding)),
            (b":path", self.url.full_path.encode(self._header_encoding)),
            (b":authority", self.url.authority.encode(self._header_encoding)),
        ]

        headers.extend([(k.lower().encode(self._header_encoding), v.encode()) for (k, v) in self.headers.items() if
                        k.lower() not in {"content-type", "content-length"}])

        return headers



