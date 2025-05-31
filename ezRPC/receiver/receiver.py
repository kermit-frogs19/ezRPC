from typing import Literal, Callable, Any
import asyncio
import msgspec

from ezh3.server import Server, ServerRequest, Response, RouteHandler
from ezh3.common.config import DEFAULT_PORT, DEFAULT_HOST

from ezRPC.receiver.function_handler import FunctionHandler, SystemFunctionHandler
from ezRPC.receiver.receiver_response import ReceiverResponse, ReceiverResponseData
from ezRPC.receiver.receiver_call import ReceiverCall
from ezRPC.receiver.receiver_connection import ReceiverConnection
from ezRPC.common.config import DEFAULT_PATH, DEFAULT_METHOD, DISCOVER_SYSTEM_PROCEDURE_NAME, PING_SYSTEM_PROCEDURE_NAME


class Receiver(Server):
    def __init__(
            self,
            title: str = "",
            host: str = DEFAULT_HOST,
            port: int = DEFAULT_PORT,
            enable_tls: bool = False,
            custom_cert_file_loc: str = None,
            custom_cert_key_file_loc: str = None,
            enable_ipv6: bool = False,
    ) -> None:
        super().__init__(
            title=title,
            host=host,
            port=port,
            enable_tls=enable_tls,
            custom_cert_file_loc=custom_cert_file_loc,
            custom_cert_key_file_loc=custom_cert_key_file_loc,
            connection_class=ReceiverConnection,
            enable_ipv6=enable_ipv6
        )
        self.functions: dict[str, FunctionHandler] = {}
        self.instances: dict[str, Any] = {}     # Keep the references to instances, so they're not Garbage-Collected
        self.system_functions: dict[str, SystemFunctionHandler] = {
            DISCOVER_SYSTEM_PROCEDURE_NAME: SystemFunctionHandler(
                name=DISCOVER_SYSTEM_PROCEDURE_NAME,
                function=self.discover
            ),
            PING_SYSTEM_PROCEDURE_NAME: SystemFunctionHandler(
                name=PING_SYSTEM_PROCEDURE_NAME,
                function=self.ping
            )
        }

    def add_function(
            self,
            func:  Callable,
            name: str = None,
            await_result: bool = True,
            description: str = None,
            discovery: bool = True
    ) -> None:
        function_name = func.__name__ if name is None else name
        self.functions[function_name] = FunctionHandler(
            name=function_name,
            function=func,
            await_result=await_result,
            description=description,
            discovery=discovery
        )

    def add_class_instance(
            self,
            instance: Any,
            name: str | None = None,
            await_result: bool = True,
            description: str | None = None,
            discovery: bool = True
    ):
        name = instance.__class__.__name__ if name is None else name

        self.instances[name] = instance

        methods = dir(instance)
        for method_name in methods:
            if method_name.startswith("_"):
                continue

            method = getattr(instance, method_name)
            if not callable(method):
                continue

            self.add_function(
                func=method,
                name=f"{name}.{method.__name__}",
                await_result=await_result,
                description=description,
                discovery=discovery
            )

    def function(
            self,
            name: str = None,
            await_result: bool = True,
            description: str = None,
            discovery: bool = True
    ) -> Callable:
        def decorator(func: Callable):
            self.add_function(func, name, await_result, description, discovery)
            return func

        return decorator

    async def discover(self) -> ReceiverResponse:
        result = {func_name: handler.discover() for func_name, handler in self.functions.items() if handler.discovery is True}
        return ReceiverResponse(data=ReceiverResponseData(error=None, data=result))

    async def ping(self) -> ReceiverResponse:
        return ReceiverResponse(data=ReceiverResponseData(error=None, data=None))

    async def handle_call(self, call: ReceiverCall) -> ReceiverResponse:
        """Processes request and returns response"""

        func_name = call.get_function_name_new()
        if func_name in self.system_functions:  # Check if the call was made to a system function
            handler = self.system_functions.get(func_name)
            try:
                return await handler.call(call)
            except BaseException as e:
                return ReceiverResponse(data=ReceiverResponseData(
                    error=f"r-Error when running system function '{func_name}': {str(e)}",
                    data=None,
                ))

        handler = self.functions.get(func_name)
        if not handler:
            return ReceiverResponse(data=ReceiverResponseData(error=f"n-Not found function '{func_name}'"))
        try:
            handler.verify(call)
        except (ValueError, TypeError, AssertionError, msgspec.ValidationError) as e:
            return ReceiverResponse(data=ReceiverResponseData(
                error=f"a-Call doesn't match the function format: {str(e)}",
                data=None,
            ))

        try:
            return await handler.call(call)
        except BaseException as e:
            return ReceiverResponse(data=ReceiverResponseData(
                error=f"r-Error when running function '{func_name}': {str(e)}",
                data=None,
            ))

    async def handle_request(self, request: ReceiverCall) -> Response:
        if request.path != DEFAULT_PATH:
            return ReceiverResponse(400, data=ReceiverResponseData(
                error=f"Requests should only be made to {DEFAULT_PATH} endpoint, not '{request.path}'"))

        if request.method != DEFAULT_METHOD:
            return ReceiverResponse(400, data=ReceiverResponseData(
                error=f"Requests should only be of method {DEFAULT_METHOD}, not '{request.method}'"))

        return await self.handle_call(request)





