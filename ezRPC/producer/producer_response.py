import msgpack
import msgspec
from typing import Any
from dataclasses import dataclass, field
from ezh3.client.client_response import ClientResponse


class ProducerResponseData:
    def __init__(
            self,
            raw: bytes = b"",
    ) -> None:
        self.raw = raw
        self.e: str | None = None
        self.d: str | None = None

        self.decode()

    def decode(self) -> None:
        data = msgpack.unpackb(self.raw, raw=False)
        self.e = data["a"]
        self.d = data["f"]


@dataclass
class ProducerResponse(ClientResponse):
    method: str = field(default="POST", init=False)
    content_type: str = field(default="application/octet-stream", init=False)

    data: ProducerResponseData = field(default=None)

    def _process_headers(self) -> dict:
        if self.raw_headers:
            headers = {str(k.decode()).replace(":", ""): v.decode() for k, v in self.raw_headers}

            if "path" in headers and not self.path:
                self.path = headers["path"]
            if "status" in headers:
                self.status_code = int(headers["status"])
            if "content-type" in headers and not self.content_type:
                self.content_type = headers["content-type"]
            else:
                self.content_length = len(self.body)

        else:
            headers = {}

        return headers

    def _process_body(self) -> None:
        return self.data.decode()
