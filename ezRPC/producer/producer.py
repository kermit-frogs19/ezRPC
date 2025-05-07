import asyncio
from typing import Any
from collections import deque
from typing import BinaryIO, Callable, Deque, Dict, List, Optional, Union, cast, Literal
from aioquic.h3.events import DataReceived, H3Event, HeadersReceived, PushPromiseReceived

from ezh3.client import *
from ezh3.common.config import DEFAULT_TIMEOUT, _DEFAULT_TIMEOUT

from ezRPC.producer.producer_call import ProducerCall, ProducerCallData
from ezRPC.producer.producer_response import ProducerResponse, ProducerResponseData
from ezRPC.producer.stub_proxy import StubProxy
from ezRPC.common.config import DEFAULT_PATH, DISCOVER_SYSTEM_NAME


class Producer(Client):
    def __init__(
            self,
            url: str = "",
            headers: dict = None,
            use_tls: bool = True,
            timeout: int | float | None = DEFAULT_TIMEOUT
    ):
        super().__init__(
            base_url=url,
            headers=headers,
            use_tls=use_tls,
            timeout=timeout
        )
        self.function = StubProxy(self)

    async def call(
            self,
            name: str,
            *args,
            url: str = None,
            headers: dict = None,
            safe: bool = False,

    ) -> Any:
        response = await self._request(ProducerCall(
            url=url,
            headers=headers,
            data=ProducerCallData(f=name, a=args)
        ))
        if safe:
            return response

        if response.data.e is not None:
            raise Exception(response.data.e)
        return response.data.d

    async def call_safe(self, name: str,  *args, url: str = None, headers: dict = None) -> ProducerResponse:
        return await self.call(name=name, url=url, headers=headers, safe=True, *args)

    async def discover(self, url: str = None, headers: dict = None) -> dict:
        return await self.call(name=DISCOVER_SYSTEM_NAME, url=url, headers=headers)

    async def _request(self, request: ProducerCall) -> ProducerResponse:
        # Resolve the conflict between class base URL and request URL, ensure the request is made to /ezrpc endpoint
        request.url.raw_url += DEFAULT_PATH if DEFAULT_PATH not in request.url.raw_url else ""
        request.url = self.base_url.resolve(request.url)

        connection = await self.connect(request.url.port, request.url.host)
        self._is_running = True

        timeout = request.timeout if not isinstance(request.timeout, _DEFAULT_TIMEOUT) else self.timeout

        stream_id = connection._quic.get_next_available_stream_id()
        connection._http.send_headers(stream_id=stream_id, headers=request.render_headers(), end_stream=False)
        connection._http.send_data(stream_id=stream_id, data=request.body, end_stream=True)

        waiter = connection._loop.create_future()
        connection._request_events[stream_id] = deque()
        connection._request_waiter[stream_id] = waiter
        connection.transmit()
        try:
            events = await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
        except asyncio.TimeoutError:
            raise HTTPTimeoutError(f"Request timed out after {timeout} seconds")

        return self._process_response_events(request=request, events=events)

    def _process_response_events(self, request: ProducerCall, events: list[H3Event]) -> ProducerResponse:
        raw_headers = None
        body = bytearray()

        for event in events:
            if isinstance(event, HeadersReceived):
                raw_headers = event.headers
            elif isinstance(event, DataReceived):
                body.extend(event.data)

        return ProducerResponse(raw_headers=raw_headers, request=request, data=ProducerResponseData(raw=body))
