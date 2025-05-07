import msgpack
import msgspec
from dataclasses import dataclass, field
from typing import Literal


from ezh3.client import ClientRequest


@dataclass
class ProducerCallData:
    f: str | None = field(default=None)
    a: tuple | None = field(default=None)

    def to_dict(self) -> dict:
        return {
            "f": self.f,
            "a": self.a
        }

    def to_msgpack(self) -> bytes:
        return msgpack.packb(self.to_dict(), use_bin_type=True)


@dataclass
class ProducerCall(ClientRequest):
    data: ProducerCallData = field(default=None)
    content_type: str = field(default="application/octet-stream", init=False)

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
            (b"content-type", self.content_type.encode(self._header_encoding))
        ]

        headers.extend([(k.lower().encode(self._header_encoding), v.encode()) for (k, v) in self.headers.items() if
                        k.lower() not in {"content-type", "content-length"}])

        return headers



