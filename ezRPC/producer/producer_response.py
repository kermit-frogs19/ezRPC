import msgpack
import msgspec
from typing import Any
from dataclasses import dataclass, field
from ezh3.client.client_response import ClientResponse
from ezRPC.common.config import StandardResponseFormat


class ProducerResponseData:
    def __init__(
            self,
            raw: bytes = b"",
            error: str | None = None,
            data: Any = None

    ) -> None:
        self.raw = raw
        self.error = error
        self.data = data

        self.decode()

    def decode(self) -> None:
        if not self.raw:
            return

        decoded = msgspec.msgpack.decode(self.raw, type=StandardResponseFormat)

        self.error = decoded.error
        self.data = decoded.data


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

        else:
            headers = {}

        return headers

    def _process_body(self) -> None:
        return self.data.decode()
