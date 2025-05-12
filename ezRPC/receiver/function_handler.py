from dataclasses import dataclass, field
import asyncio
from typing import Callable
from inspect import signature, Signature, Parameter
from types import MappingProxyType
import msgspec
from typing import Any, Type

from ezRPC.receiver.receiver_call import ReceiverCall, ReceiverCallData
from ezRPC.receiver.receiver_response import ReceiverResponse, ReceiverResponseData


@dataclass
class FunctionHandler:
    name: str = field(default=None)
    function: Callable = field(default=lambda: None)
    await_result: bool = field(default=True)
    description: str = field(default=None)

    parameters: MappingProxyType = field(default=None, repr=False)
    signature: Signature = field(default=None, repr=False)
    cls: type[msgspec.Struct] = field(default=None, repr=False)

    def __post_init__(self):
        self.signature = signature(self.function)
        self.parameters = self.signature.parameters
        self.build_msgspec_class()

    def verify(self, call: ReceiverCall):
        func_name = call.get_function_name()
        if func_name != self.name:
            raise ValueError(f"Function mismatch: expected '{self.name}', got '{func_name}'")

        instance = msgspec.msgpack.decode(call.data.raw, type=self.cls)
        call.data.from_msgspec_struct(instance)

    def build_msgspec_class(self) -> None:
        args_annotations: list = []
        for name, param in self.parameters.items():
            if param.annotation is Parameter.empty:
                raise TypeError(f"Parameter '{name}' in {self.function.__name__} has no type annotation.")
            args_annotations.append(param.annotation)
        fields: list[tuple[str, Any]] = [
            ("f", str),
            ("a", tuple[*args_annotations])
        ]

        struct_cls = msgspec.defstruct(f"{self.function.__name__.capitalize()}Args", fields=fields)
        self.cls = struct_cls

    def discover(self) -> dict:
        return {
            "parameters": {k: v.annotation.__name__ for k, v in self.signature.parameters.items()},
            "description": self.description,
            "return": str(self.signature.return_annotation.__name__) if self.signature.return_annotation is not None else None
        }

    async def call(self, call: ReceiverCall) -> ReceiverResponse:
        if self.function is None:
            raise ValueError(f"Function handler is not defined for FunctionHandler object {self}")

        args = call.data.a if call.data.a is not None else []
        if not self.await_result:
            if asyncio.iscoroutinefunction(self.function):
                # noinspection PyAsyncCall
                asyncio.create_task(self.function(*args))

            return ReceiverResponse(data=ReceiverResponseData(e=None, d=None, f=call.data.f))

        if asyncio.iscoroutinefunction(self.function):
            result = await self.function(*args)
        else:
            result = self.function(*args)

        return ReceiverResponse(data=ReceiverResponseData(e=None, d=result, f=call.data.f))










