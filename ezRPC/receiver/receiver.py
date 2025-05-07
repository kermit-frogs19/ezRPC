from typing import Literal, Callable
import asyncio
import msgspec

from ezh3.server import Server, ServerRequest, Response, RouteHandler
from ezh3.common.config import DEFAULT_PORT, DEFAULT_HOST

from ezRPC.receiver.function_handler import FunctionHandler
from ezRPC.receiver.receiver_response import ReceiverResponse, ReceiverResponseData
from ezRPC.receiver.receiver_call import ReceiverCall
from ezRPC.common.config import DEFAULT_PATH, DEFAULT_METHOD
from ezRPC.receiver.receiver_connection import ReceiverConnection


class Receiver(Server):
    def __init__(
            self,
            title: str = "",
            host: str = DEFAULT_HOST,
            port: int = DEFAULT_PORT,
            enable_tls: bool = False,
            custom_cert_file_loc: str = None,
            custom_cert_key_file_loc: str = None,
            cert_type: Literal["SELF_SIGNED", "CUSTOM", None] = "SELF_SIGNED",
            enable_ipv6: bool = False,
    ) -> None:
        super().__init__(
            title=title,
            host=host,
            port=port,
            enable_tls=enable_tls,
            custom_cert_file_loc=custom_cert_file_loc,
            custom_cert_key_file_loc=custom_cert_key_file_loc,
            cert_type=cert_type,
            connection_class=ReceiverConnection,
            enable_ipv6=enable_ipv6
        )
        self.functions: dict[str, FunctionHandler] = {}

    def function(self, name: str = None, await_result: bool = True, description: str = None) -> Callable:
        def decorator(func: Callable):
            function_name = func.__name__ if name is None else name
            self.functions[function_name] = FunctionHandler(
                function=func,
                await_result=await_result,
                description=description
            )
            return func

        return decorator

    async def discover(self) -> ReceiverResponse:
        result = {func_name: handler.discover() for func_name, handler in self.functions.items()}
        return ReceiverResponse(data=ReceiverResponseData(e=None, d=result, f="__discover__"))

    async def handle_call(self, call: ReceiverCall) -> ReceiverResponse:
        """Processes request and returns response"""

        func_name = call.get_function_name()
        if func_name == "__discover__":
            return await self.discover()

        handler = self.functions.get(func_name)
        if not handler:
            return ReceiverResponse(data=ReceiverResponseData(e=f"Not found function '{func_name}'", f=func_name))
        try:
            handler.verify(call)
        except (ValueError, TypeError, AssertionError, msgspec.ValidationError) as e:
            return ReceiverResponse(data=ReceiverResponseData(
                e=f"Call doesn't match the function format: {str(e)}",
                d=None,
                f=func_name
            ))

        try:
            return await handler.call(call)
        except BaseException as e:
            return ReceiverResponse(data=ReceiverResponseData(
                e=f"Error when running function '{func_name}': {str(e)}",
                d=None,
                f=call.data.f
            ))

    async def handle_request(self, request: ReceiverCall) -> Response:
        if request.path != DEFAULT_PATH:
            return ReceiverResponse(400, data=ReceiverResponseData(e=f"Requests should only be made to {DEFAULT_PATH} endpoint, not '{request.path}'"))

        if request.method != DEFAULT_METHOD:
            return ReceiverResponse(400, data=ReceiverResponseData(e=f"Requests should only be of method {DEFAULT_METHOD}, not '{request.method}'"))

        return await self.handle_call(request)





