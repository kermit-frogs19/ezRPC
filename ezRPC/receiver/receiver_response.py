from ezh3.server.responses import Response
from dataclasses import dataclass, field
from typing import Any
import msgpack


@dataclass
class ReceiverResponseData:
    e: str | None = field(default=None)
    d: Any = field(default=None)
    f: str | None = field(default=None)

    def to_dict(self) -> dict:
        return {
            "e": self.e,
            "d": self.d,
            "f": self.f
        }

    def to_msgpack(self) -> bytes:
        return msgpack.packb(self.to_dict(), use_bin_type=True)


@dataclass
class ReceiverResponse(Response):
    data: ReceiverResponseData = field(default=None)

    def render_headers(self) -> list:
        raw_headers = [
            (b":status", str(self.status_code).encode(self._header_encoding)),
            (b"content-type", self.content_type.encode(self._header_encoding)),
        ]

        for header, value in self.headers.items():
            raw_headers.append((header.lower().encode(self._header_encoding), value.lower().encode(self._header_encoding)))

        return raw_headers

    def render_body(self) -> bytes:
        return self.data.to_msgpack()
