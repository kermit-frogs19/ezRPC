from ezh3.server.responses import Response
from dataclasses import dataclass, field
from typing import Any
import msgpack


@dataclass
class ReceiverResponseData:
    error: str | None = field(default=None)
    data: Any = field(default=None)

    def to_dict(self) -> dict:
        return {
            "e": self.error,
            "d": self.data,
        }

    def to_list(self) -> list:
        return [
            self.error,
            self.data
        ]

    def to_msgpack(self) -> bytes:
        return msgpack.packb(self.to_list(), use_bin_type=True)


@dataclass
class ReceiverResponse(Response):
    data: ReceiverResponseData = field(default=None)

    def render_headers(self) -> list:
        raw_headers = [
            (b":status", str(self.status_code).encode(self._header_encoding))
        ]

        for header, value in self.headers.items():
            raw_headers.append((header.lower().encode(self._header_encoding), value.lower().encode(self._header_encoding)))

        return raw_headers

    def render_body(self) -> bytes:
        return self.data.to_msgpack()
