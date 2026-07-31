"""Client-side QUIC protocol: one bidirectional stream per call.

Each call opens a fresh stream, writes the request with FIN, and awaits the
response on a per-stream future. Critically, in-flight calls are failed
*immediately* when the connection drops or a stream is reset — the aioquic base
only resolves its own connected/ping waiters on termination, never ours, which
is exactly why v1 hung on connection loss until the full timeout elapsed.
"""

import asyncio

from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import (
    QuicEvent, StreamDataReceived, StreamReset, ConnectionTerminated,
)

from ezRPC.common.config import ERR_CLIENT_CANCEL, ERR_REQUEST_TOO_LARGE
from ezRPC.common.exceptions import TransportError, CallTimeoutError, CallError


class RPCClientProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._waiters: dict[int, asyncio.Future] = {}
        self._buffers: dict[int, bytearray] = {}
        self.method_ids: dict[str, int] = {}  # connection-scoped: method name -> server-assigned id
        self.sent_auth: str | None = None     # last auth token sent on this connection
        self.alive: bool = True

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, StreamDataReceived):
            buf = self._buffers.get(event.stream_id)
            if buf is None:
                buf = bytearray()
                self._buffers[event.stream_id] = buf
            buf.extend(event.data)
            if event.end_stream:
                data = bytes(self._buffers.pop(event.stream_id))
                waiter = self._waiters.pop(event.stream_id, None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(data)

        elif isinstance(event, StreamReset):
            self._buffers.pop(event.stream_id, None)
            waiter = self._waiters.pop(event.stream_id, None)
            if waiter is not None and not waiter.done():
                # translate known application error codes into typed exceptions
                if event.error_code == ERR_REQUEST_TOO_LARGE:
                    exc: Exception = CallError(
                        "request rejected by the server: body exceeds its max request size"
                    )
                else:
                    exc = TransportError(f"stream reset by peer (code {event.error_code})")
                waiter.set_exception(exc)

        elif isinstance(event, ConnectionTerminated):
            self.alive = False
            self._fail_all(TransportError(
                f"connection terminated: {event.reason_phrase or event.error_code}"
            ))

    def connection_lost(self, exc: Exception | None) -> None:
        self.alive = False
        self._fail_all(TransportError("connection lost" + (f": {exc}" if exc else "")))
        super().connection_lost(exc)

    def _fail_all(self, exc: BaseException) -> None:
        # pop-before-set so a late normal completion can never double-set a future
        while self._waiters:
            _, waiter = self._waiters.popitem()
            if not waiter.done():
                waiter.set_exception(exc)
        self._buffers.clear()

    async def request(self, payload: bytes, *, await_result: bool,
                      timeout: float | None) -> bytes | None:
        stream_id = self._quic.get_next_available_stream_id()
        if not await_result:
            # allocate + send with no await in between: the stream id cannot be reused
            self._quic.send_stream_data(stream_id, payload, end_stream=True)
            self.transmit()
            return None

        waiter: asyncio.Future = self._loop.create_future()
        self._waiters[stream_id] = waiter
        # allocate + send with no await in between: the stream id cannot be reused
        self._quic.send_stream_data(stream_id, payload, end_stream=True)
        self.transmit()

        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout)
        except asyncio.TimeoutError:
            self._abandon(stream_id)
            raise CallTimeoutError(f"call timed out after {timeout}s") from None
        except asyncio.CancelledError:
            # the caller cancelled the call: same cleanup as a timeout, or the
            # waiter leaks and the server keeps working for nobody
            self._abandon(stream_id)
            raise

    def _abandon(self, stream_id: int) -> None:
        """Drop local state for a call nobody awaits anymore, and tell the server
        to stop producing a response it will never read."""
        self._waiters.pop(stream_id, None)
        self._buffers.pop(stream_id, None)
        try:
            self._quic.stop_stream(stream_id, ERR_CLIENT_CANCEL)
            self.transmit()
        except Exception:
            pass
