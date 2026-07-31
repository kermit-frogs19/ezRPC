"""Server-side QUIC protocol: one bidirectional stream per call.

Bytes are accumulated per stream until FIN, then the call is dispatched as its
own task. Because this is raw QUIC (not HTTP/3), stream resets and STOP_SENDING
are actually delivered here — so a client cancel or disconnect cancels the
running handler and frees its state, closing v1's class of "reset silently
ignored / handler runs forever" bugs. A per-stream body-size cap protects against
unbounded memory growth from a hostile client.
"""

import asyncio
from functools import partial

from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import (
    QuicEvent, StreamDataReceived, StreamReset, StopSendingReceived, ConnectionTerminated,
)

from ezRPC.common.config import (
    ERR_PREFIX_RUN, ERR_REQUEST_TOO_LARGE, STREAM_TYPE_MASK, CLIENT_BIDI_STREAM,
)


class RPCServerProtocol(QuicConnectionProtocol):
    def __init__(self, *args, server=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._server = server
        self._buffers: dict[int, bytearray] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._rejected: set[int] = set()        # streams refused mid-transfer (e.g. oversized)
        self.auth: str | None = None            # connection's current auth token (cached)
        self.connection_state: dict = {}        # per-connection scratch for hooks/schemes
        if server is not None:                  # registry: lets the server drain on shutdown
            server._protocols.add(self)

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, StreamDataReceived):
            if event.stream_id & STREAM_TYPE_MASK != CLIENT_BIDI_STREAM:
                return  # only client-initiated bidirectional streams carry calls
            if event.stream_id in self._rejected:  # already refused: drop follow-up chunks
                if event.end_stream:
                    self._rejected.discard(event.stream_id)
                return
            buf = self._buffers.get(event.stream_id)
            if buf is None:
                buf = bytearray()
                self._buffers[event.stream_id] = buf

            limit = self._server.max_request_bytes
            if limit and len(buf) + len(event.data) > limit:
                self._buffers.pop(event.stream_id, None)
                if not event.end_stream:
                    self._rejected.add(event.stream_id)
                try:
                    # stop the inbound flood, and abandon our response side
                    self._quic.stop_stream(event.stream_id, ERR_REQUEST_TOO_LARGE)
                    self._quic.reset_stream(event.stream_id, ERR_REQUEST_TOO_LARGE)
                    self.transmit()
                except Exception:
                    pass
                return

            buf.extend(event.data)
            if event.end_stream:
                data = bytes(self._buffers.pop(event.stream_id, b""))
                task = self._loop.create_task(self._handle(event.stream_id, data))
                self._tasks[event.stream_id] = task
                task.add_done_callback(partial(self._forget_task, event.stream_id))

        elif isinstance(event, (StreamReset, StopSendingReceived)):
            self._buffers.pop(event.stream_id, None)
            self._rejected.discard(event.stream_id)
            pending = self._tasks.pop(event.stream_id, None)
            if pending is not None and not pending.done():
                pending.cancel()

        elif isinstance(event, ConnectionTerminated):
            self._cancel_all()

    def connection_lost(self, exc: Exception | None) -> None:
        self._cancel_all()
        if self._server is not None:
            self._server._protocols.discard(self)
        super().connection_lost(exc)

    def _forget_task(self, stream_id: int, _task: asyncio.Task) -> None:
        self._tasks.pop(stream_id, None)

    def _cancel_all(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._buffers.clear()
        self._rejected.clear()

    async def _handle(self, stream_id: int, data: bytes) -> None:
        try:
            response = await self._server.dispatch(data, self)
        except asyncio.CancelledError:
            raise  # cancelled by a client reset — send nothing
        except Exception as e:
            # backstop: log server-side with a reference id, reply generically
            response = self._server.error_bytes(ERR_PREFIX_RUN + self._server._safe_error(e))
        try:
            # empty response = fire-and-forget: still FIN the stream to close it cleanly
            self._quic.send_stream_data(stream_id, response, end_stream=True)
            self.transmit()
        except Exception:
            pass
