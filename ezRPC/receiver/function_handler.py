from dataclasses import dataclass, field
import asyncio
from typing import Callable, cast
from inspect import signature, Signature, Parameter
from types import MappingProxyType
import msgspec
from typing import Any, Type

from ezRPC.receiver.receiver_call import ReceiverCall, ReceiverCallData
from ezRPC.receiver.receiver_response import ReceiverResponse, ReceiverResponseData
from ezRPC.common.config import StandardCallFormat, CallType, SUPPORTED_TYPES


@dataclass
class FunctionHandler:
    name: str = field(default=None)
    function: Callable = field(default=lambda: None)
    await_result: bool = field(default=True)
    description: str = field(default=None)
    discovery: bool = field(default=True)

    check_return_type: bool = field(default=True, repr=False, init=False)
    parameters: MappingProxyType = field(default=None, repr=False, init=False)
    signature: Signature = field(default=None, repr=False, init=False)
    cls: type[StandardCallFormat] = field(default=None, repr=False, init=False)

    def __post_init__(self):
        self.signature = signature(self.function)
        self.parameters = self.signature.parameters
        self.build_msgspec_class()

    def verify(self, call: ReceiverCall):
        instance = msgspec.msgpack.decode(call.data.raw, type=self.cls)
        call.data.from_msgspec_struct(instance)

    def build_msgspec_class(self) -> None:
        if self.check_return_type and self.signature.return_annotation not in SUPPORTED_TYPES:
            raise TypeError(f"Unsupported return value type - '{self.signature.return_annotation}' in function {self.function.__name__}")

        args_annotations: list = []
        for name, param in self.parameters.items():
            if param.annotation is Parameter.empty:
                raise TypeError(f"Parameter '{name}' in {self.function.__name__} has no type annotation.")
            elif param.annotation not in SUPPORTED_TYPES:
                raise TypeError(f"Unsupported type - '{param.annotation}' of parameter '{name}' -  in function {self.function.__name__}")

            args_annotations.append(param.annotation)

        class_name = f"{self.function.__name__.capitalize()}CallArgs"

        fields: list[tuple[str, Any]] = [
            ("function_name", str),
            ("call_type", int),
            ("args", tuple[*args_annotations])
        ]

        struct_cls = msgspec.defstruct(name=class_name, fields=fields, array_like=True, bases=(StandardCallFormat,))
        self.cls = cast(type[StandardCallFormat], struct_cls)

    def discover(self) -> dict:
        return {
            "parameters": {k: v.annotation.__name__ for k, v in self.signature.parameters.items()},
            "description": self.description,
            "return": str(self.signature.return_annotation.__name__) if self.signature.return_annotation is not None else None
        }

    async def call(self, call: ReceiverCall) -> ReceiverResponse:
        if self.function is None:
            raise ValueError(f"Function handler is not defined for FunctionHandler object {self}")

        args = call.data.args if call.data.args is not None else []
        if not self.await_result or call.data.call_type == CallType.NOT_AWAITED_RUN_CALL:
            if asyncio.iscoroutinefunction(self.function):
                # noinspection PyAsyncCall
                asyncio.create_task(self.function(*args))

            return ReceiverResponse(data=ReceiverResponseData(error=None, data=None))

        if asyncio.iscoroutinefunction(self.function):
            result = await self.function(*args)
        else:
            result = self.function(*args)

        return ReceiverResponse(data=ReceiverResponseData(error=None, data=result))


@dataclass
class SystemFunctionHandler(FunctionHandler):
    await_result: bool = field(default=True, init=False)
    description: str = field(default=None, init=False)
    check_return_type: bool = field(default=False, repr=False, init=False)

    async def call(self, call: ReceiverCall) -> ReceiverResponse:
        if self.function is None:
            raise ValueError(f"System function handler is not defined for FunctionHandler object {self}")
        return await self.function()









