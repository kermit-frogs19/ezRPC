from aioquic.h3.events import H3Event, HeadersReceived, DataReceived
from aioquic.quic.events import ProtocolNegotiated, StreamReset, QuicEvent

from ezh3.server import ServerConnection

from ezRPC.receiver.receiver_call import ReceiverCall
from ezRPC.common.config import FIRE_AND_FORGET_CALL


class ReceiverConnection(ServerConnection):
    def __init__(self, server, *args, **kwargs):
        super().__init__(server, *args, **kwargs)

        self._requests: dict[int, ReceiverCall] = {}  # ✅ Stores request data (headers + body)

    async def _h3_event_received(self, event: H3Event) -> None:
        if isinstance(event, HeadersReceived):
            self._requests[event.stream_id] = ReceiverCall(raw_headers=event.headers)

        elif isinstance(event, DataReceived):
            if event.stream_id in self._requests:
                self._requests[event.stream_id].body += event.data

        elif isinstance(event, StreamReset):
            if event.stream_id in self._requests:
                del self._requests[event.stream_id]

        if event.stream_ended:
            await self._process_request(event.stream_id)

    async def _process_request(self, stream_id: int) -> None:
        """Processes a fully received HTTP request."""
        if stream_id not in self._requests:
            return

        request = self._requests.pop(stream_id)

        # Let ReceiverCall process body into ReceiverCallData
        request._process_body()

        response = await self.server.handle_request(request)

        if request.data.call_type == FIRE_AND_FORGET_CALL:
            self.close_stream(stream_id=stream_id)
        else:
            self.send_response(stream_id=stream_id, response=response)
