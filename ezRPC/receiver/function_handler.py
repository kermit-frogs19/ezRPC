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
from typing import Callable, Any, TypedDict

import msgspec
import msgspec.inspect

from ezRPC.security import Security


# functional syntax because "return" is a keyword and can't be a class attribute
FunctionDescription = TypedDict(
    "FunctionDescription",
    {"parameters": dict[str, str], "description": str | None, "return": str | None},
)


def _type_name(ann) -> str:
    # 'int' for plain classes, 'list[int]' / 'int | None' for generics and unions
    return ann.__name__ if isinstance(ann, type) else str(ann)


def _check_wire_type(ann, where: str, func_name: str) -> None:
    """Reject annotations msgspec cannot decode from msgpack (arbitrary classes).

    msgspec accepts any annotation at Decoder-build time and only fails custom
    classes on the first call — walk the type tree at registration so a bad
    annotation is a loud dev-time error instead."""
    seen: set[int] = set()

    def walk(node) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, msgspec.inspect.CustomType):
            raise TypeError(
                f"unsupported {where} type '{_type_name(ann)}' in function '{func_name}': "
                f"'{node.cls.__name__}' is not msgpack-decodable (use builtins, containers, "
                f"or a msgspec.Struct)"
            )
        if isinstance(node, msgspec.Struct):        # all inspect nodes are Structs
            for f in node.__struct_fields__:
                walk(getattr(node, f))
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    try:
        walk(msgspec.inspect.type_info(ann))
    except TypeError:
        raise
    except Exception as e:
        raise TypeError(
            f"unsupported {where} type '{ann}' in function '{func_name}': {e}"
        ) from e


@dataclass
class FunctionHandler:
    name: str
    function: Callable
    await_result: bool = True
    description: str | None = None
    discovery: bool = True

    is_coroutine: bool = field(default=False, init=False, repr=False)
    # set in __post_init__ / _build_args_type, so no defaults needed
    signature: Signature = field(init=False, repr=False)
    args_type: Any = field(default=None, init=False, repr=False)
    method_id: int = field(default=-1, init=False, repr=False)
    # one entry per parameter, in order: None = wire arg, else = a Security scheme
    _param_plan: list = field(init=False, repr=False)
    _decoder: msgspec.msgpack.Decoder = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.signature = signature(self.function)
        self.is_coroutine = asyncio.iscoroutinefunction(self.function)
        self._build_args_type()

    def _build_args_type(self) -> None:
        ret = self.signature.return_annotation
        if ret is not Signature.empty and ret is not None:
            _check_wire_type(ret, "return", self.function.__name__)
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
            _check_wire_type(param.annotation, f"parameter '{pname}'", self.function.__name__)
            wire_annotations.append(param.annotation)
            self._param_plan.append(None)
        self.args_type = tuple[*wire_annotations] if wire_annotations else tuple[()]  # type: ignore[valid-type,misc]
        # anything msgspec supports is a wire type (generics, Optionals, Structs,
        # dataclasses, datetimes, ...); the prebuilt decoder also makes the
        # per-call decode cheaper than rebuilding one each time
        self._decoder = msgspec.msgpack.Decoder(self.args_type)

    def decode_args(self, raw) -> tuple:
        """Decode + type-validate the wire arguments against this function's signature."""
        return self._decoder.decode(raw)

    async def build_args(self, ctx, wire_args: tuple) -> list:
        """Interleave decoded wire args with resolved Security dependencies, in
        declaration order. A rejecting scheme raises AuthError, which the caller maps
        to an auth failure."""
        full, wire_index = [], 0
        for scheme in self._param_plan:
            if scheme is None:
                full.append(wire_args[wire_index])
                wire_index += 1
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

    def describe(self) -> FunctionDescription:
        params = {}
        for pname, param in self.signature.parameters.items():
            if isinstance(param.default, Security):     # not part of the wire contract
                continue
            params[pname] = _type_name(param.annotation)
        ret = self.signature.return_annotation
        if ret is Signature.empty or ret is None:
            ret_name = None
        else:
            ret_name = _type_name(ret)
        return {"parameters": params, "description": self.description, "return": ret_name}
