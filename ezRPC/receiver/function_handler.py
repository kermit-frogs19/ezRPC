"""Wraps a registered function: validates its signature once, then decodes and
invokes it per call.

A parameter is a **wire argument** (its value comes from the client, typed and
validated by msgspec) unless its default is a ``Security(...)`` marker, in which
case it's a **dependency** — the framework runs the scheme against the call's auth
and injects the result, rejecting the call if the scheme rejects. So the msgspec
args type is built from the wire params only; dependencies are resolved at call
time and interleaved back into their declared positions.
"""

import asyncio
from dataclasses import dataclass, field
from inspect import signature, Signature, Parameter, isawaitable
from typing import Callable, Any

import msgspec

from ezRPC.common.config import SUPPORTED_TYPES
from ezRPC.security import Security


@dataclass
class FunctionHandler:
    name: str
    function: Callable
    await_result: bool = True
    description: str | None = None
    discovery: bool = True

    is_coroutine: bool = field(default=False, init=False, repr=False)
    signature: Signature = field(default=None, init=False, repr=False)
    args_type: Any = field(default=None, init=False, repr=False)
    method_id: int = field(default=-1, init=False, repr=False)
    # one entry per parameter, in order: None = wire arg, else = a Security scheme
    _param_plan: list = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.signature = signature(self.function)
        self.is_coroutine = asyncio.iscoroutinefunction(self.function)
        self._build_args_type()

    def _build_args_type(self) -> None:
        ret = self.signature.return_annotation
        if ret is not Signature.empty and ret not in SUPPORTED_TYPES:
            raise TypeError(
                f"unsupported return type '{ret}' in function '{self.function.__name__}'"
            )
        self._param_plan = []
        wire_annotations: list = []
        for pname, param in self.signature.parameters.items():
            if isinstance(param.default, Security):     # injected dependency, not a wire arg
                self._param_plan.append(param.default.scheme)
                continue
            if param.annotation is Parameter.empty:
                raise TypeError(
                    f"parameter '{pname}' in '{self.function.__name__}' has no type annotation"
                )
            if param.annotation not in SUPPORTED_TYPES:
                raise TypeError(
                    f"unsupported type '{param.annotation}' for parameter '{pname}' "
                    f"in '{self.function.__name__}'"
                )
            wire_annotations.append(param.annotation)
            self._param_plan.append(None)
        self.args_type = tuple[*wire_annotations] if wire_annotations else tuple[()]

    def decode_args(self, raw) -> tuple:
        """Decode + type-validate the wire arguments against this function's signature."""
        return msgspec.msgpack.decode(raw, type=self.args_type)

    async def build_args(self, ctx, wire_args: tuple) -> list:
        """Interleave decoded wire args with resolved Security dependencies, in
        declaration order. A rejecting scheme raises AuthError, which the caller maps
        to an auth failure."""
        full, wi = [], 0
        for scheme in self._param_plan:
            if scheme is None:
                full.append(wire_args[wi])
                wi += 1
            else:
                principal = scheme(ctx)
                if isawaitable(principal):
                    principal = await principal
                full.append(principal)
        return full

    async def call_with(self, full_args: list) -> Any:
        if self.is_coroutine:
            return await self.function(*full_args)
        return self.function(*full_args)

    def describe(self) -> dict:
        params = {}
        for pname, param in self.signature.parameters.items():
            if isinstance(param.default, Security):     # not part of the wire contract
                continue
            ann = param.annotation
            params[pname] = getattr(ann, "__name__", str(ann))
        ret = self.signature.return_annotation
        if ret is Signature.empty or ret is None:
            ret_name = None
        else:
            ret_name = getattr(ret, "__name__", str(ret))
        return {"parameters": params, "description": self.description, "return": ret_name}
