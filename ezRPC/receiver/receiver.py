"""The RPC server.

Register plain functions with ``@app.function()`` (or ``add_class_instance``),
then ``await app.run(...)``. The server runs on raw QUIC via aioquic. Handler
exceptions are logged in full server-side but returned to the caller as a generic
message with a reference id (unless ``debug`` is on) so internal detail never
leaks over the wire.
"""

import asyncio
import logging
import uuid
from functools import partial
from typing import Callable, Any

import msgspec
from msgspec.msgpack import Ext
from aioquic.asyncio.server import serve
from aioquic.quic.configuration import QuicConfiguration

from ezRPC.common.config import (
    ALPN_PROTOCOL, DEFAULT_HOST, DEFAULT_PORT, FIRE_AND_FORGET_CALL, NOT_AWAITED_RUN_CALL,
    CallEnvelope, DISCOVER_SYSTEM_PROCEDURE_NAME, PING_SYSTEM_PROCEDURE_NAME, ERR_PREFIX_UNKNOWN_ID,
    HASH_EXT_CODE, method_hash,
)
from ezRPC.common.certificate import generate_self_signed_cert
from ezRPC.common.context import CallContext, _current as _current_context
from ezRPC.common.exceptions import AuthError
from ezRPC.receiver.function_handler import FunctionHandler
from ezRPC.receiver.protocol import RPCServerProtocol

logger = logging.getLogger("ezrpc")

_LOCALHOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"}


class Receiver:
    def __init__(self, title: str = "", host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *,
                 cert_file: str = None, key_file: str = None, debug: bool | None = None,
                 max_request_bytes: int = 8 * 1024 * 1024) -> None:
        self.title = title
        self.host = host
        self.port = port
        self.cert_file = cert_file
        self.key_file = key_file
        self.debug = debug
        self.max_request_bytes = max_request_bytes

        self.functions: dict[str, FunctionHandler] = {}
        self._id_to_handler: list[FunctionHandler] = []  # method id -> handler (list index)
        self._hash_to_handler: dict[bytes, FunctionHandler] = {}  # 64-bit name hash -> handler
        self.instances: dict[str, Any] = {}
        self.system_functions: dict[str, Callable] = {
            DISCOVER_SYSTEM_PROCEDURE_NAME: self.discover,
            PING_SYSTEM_PROCEDURE_NAME: self.ping,
        }
        self._before_call: Callable | None = None
        self._server = None
        self._background: set[asyncio.Task] = set()
        self._is_running = False
        self._debug_effective = bool(debug)

    # ---- registration ----
    def add_function(self, func: Callable, name: str = None, await_result: bool = True,
                     description: str = None, discovery: bool = True) -> None:
        function_name = func.__name__ if name is None else name
        handler = FunctionHandler(
            name=function_name, function=func, await_result=await_result,
            description=description, discovery=discovery,
        )
        existing = self.functions.get(function_name)
        if existing is not None:                 # re-registration keeps the id stable
            handler.method_id = existing.method_id
            self._id_to_handler[handler.method_id] = handler
        else:
            handler.method_id = len(self._id_to_handler)
            self._id_to_handler.append(handler)

        h = method_hash(function_name)           # 64-bit name hash + startup collision check
        clash = self._hash_to_handler.get(h)
        if clash is not None and clash.name != function_name:
            raise RuntimeError(
                f"method-name hash collision between '{function_name}' and '{clash.name}' "
                f"(identical 64-bit hash) — rename one of them."
            )
        self._hash_to_handler[h] = handler

        self.functions[function_name] = handler

    def function(self, name: str = None, await_result: bool = True,
                 description: str = None, discovery: bool = True) -> Callable:
        def decorator(func: Callable):
            self.add_function(func, name, await_result, description, discovery)
            return func
        return decorator

    def add_class_instance(self, instance: Any, name: str | None = None, await_result: bool = True,
                           description: str | None = None, discovery: bool = True) -> None:
        name = instance.__class__.__name__ if name is None else name
        self.instances[name] = instance
        cls = type(instance)
        for method_name in dir(instance):
            if method_name.startswith("_"):
                continue
            # don't evaluate property getters (registration must have no side effects)
            if isinstance(getattr(cls, method_name, None), property):
                continue
            method = getattr(instance, method_name)
            if not callable(method):
                continue
            try:
                self.add_function(func=method, name=f"{name}.{method_name}",
                                  await_result=await_result, description=description,
                                  discovery=discovery)
            except TypeError as e:  # skip a bad method, don't abort the whole instance
                logger.warning("skipping %s.%s: %s", name, method_name, e)

    def before_call(self, func: Callable) -> Callable:
        """Register a global hook ``async def hook(ctx) -> None | str`` run before
        every call (ctx has .method, .auth, .connection_state, .call_type). Return a
        string to reject the call with that error (prefix ``u-`` to raise AuthError on
        the client); return None to allow it. For per-handler auth, prefer Security()."""
        self._before_call = func
        return func

    # ---- built-in procedures ----
    async def discover(self) -> dict:
        return {n: h.describe() for n, h in self.functions.items() if h.discovery}

    async def ping(self) -> None:
        return None

    # ---- response helpers ----
    @staticmethod
    def ok_bytes(data: Any, method_id: int | None = None) -> bytes:
        if method_id is None:
            return msgspec.msgpack.encode([None, data])
        return msgspec.msgpack.encode([None, data, method_id])

    @staticmethod
    def error_bytes(error: str, method_id: int | None = None) -> bytes:
        if method_id is None:
            return msgspec.msgpack.encode([error, None])
        return msgspec.msgpack.encode([error, None, method_id])

    def _safe_error(self, exc: BaseException) -> str:
        if self._debug_effective:
            return f"{type(exc).__name__}: {exc}"
        ref = uuid.uuid4().hex[:8]
        logger.exception("call failed [ref %s]", ref, exc_info=exc)
        return f"internal error (ref {ref})"

    # ---- dispatch: bytes in, response bytes out (b"" = no response / fire-and-forget) ----
    async def dispatch(self, data: bytes, connection) -> bytes:
        try:
            env = msgspec.msgpack.decode(data, type=CallEnvelope)
        except (msgspec.DecodeError, msgspec.ValidationError):
            return self.error_bytes("a-malformed call payload")

        if env.auth is not None:               # client (re)sent its token; cache it on the connection
            connection.auth = env.auth

        fire = env.call_type == int(FIRE_AND_FORGET_CALL)
        method = env.method
        echo_id = None  # id to hand back to the client — only when it addressed us by name

        # resolve the target handler, by numeric id (int), name hash (Ext), or name (str)
        if isinstance(method, int):
            handler = self._id_to_handler[method] if 0 <= method < len(self._id_to_handler) else None
            if handler is None:
                # unknown id: ask the client to resend by name, and do NOT execute anything
                return b"" if fire else self.error_bytes(f"{ERR_PREFIX_UNKNOWN_ID}unknown method id {method}")
            name = handler.name
            system = None
        elif isinstance(method, Ext):
            handler = self._hash_to_handler.get(method.data) if method.code == HASH_EXT_CODE else None
            if handler is None:
                return b"" if fire else self.error_bytes("n-Not found function (unknown method hash)")
            name = handler.name
            system = None
            echo_id = handler.method_id          # client addressed us by hash -> teach it the id
        else:
            name = method
            system = self.system_functions.get(name)
            handler = None if system is not None else self.functions.get(name)
            if system is None and handler is None:
                return b"" if fire else self.error_bytes(f"n-Not found function '{name}'")
            if handler is not None:
                echo_id = handler.method_id  # teach the client this method's id

        ctx = CallContext(method=name, auth=connection.auth,
                          connection_state=connection.connection_state, call_type=env.call_type)
        _current_context.set(ctx)

        if self._before_call is not None:
            try:
                rejection = await self._before_call(ctx)
            except Exception as e:
                return b"" if fire else self.error_bytes(f"u-{self._safe_error(e)}", echo_id)
            if rejection is not None:
                return b"" if fire else self.error_bytes(str(rejection), echo_id)

        if system is not None:
            try:
                result = await system()
            except Exception as e:
                return b"" if fire else self.error_bytes(f"r-{self._safe_error(e)}")
            return b"" if fire else self.ok_bytes(result)

        try:
            wire_args = handler.decode_args(env.args)
        except (msgspec.DecodeError, msgspec.ValidationError, TypeError) as e:
            return b"" if fire else self.error_bytes(f"a-{e}", echo_id)

        # resolve auth/Security dependencies before we ack or spawn anything
        try:
            full_args = await handler.build_args(ctx, wire_args)
        except AuthError as e:
            return b"" if fire else self.error_bytes(f"u-{e}", echo_id)

        if fire or env.call_type == int(NOT_AWAITED_RUN_CALL) or not handler.await_result:
            self._spawn(handler, full_args)
            return b"" if fire else self.ok_bytes(None, echo_id)

        try:
            result = await handler.call_with(full_args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return self.error_bytes(f"r-{self._safe_error(e)}", echo_id)
        return self.ok_bytes(result, echo_id)

    def _spawn(self, handler: FunctionHandler, full_args: list) -> None:
        # keep a reference so the task can't be GC'd mid-flight, and log its failures
        task = asyncio.ensure_future(handler.call_with(full_args))
        self._background.add(task)

        def _done(t: asyncio.Task) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("background call to '%s' failed", handler.name, exc_info=t.exception())

        task.add_done_callback(_done)

    # ---- lifecycle ----
    def _resolve_debug(self) -> None:
        self._debug_effective = (self.host in _LOCALHOSTS) if self.debug is None else bool(self.debug)

    def _configure(self) -> QuicConfiguration:
        config = QuicConfiguration(is_client=False, alpn_protocols=[ALPN_PROTOCOL])
        if self.cert_file and self.key_file:
            cert, key = self.cert_file, self.key_file
        else:
            cert, key = "cert.pem", "key.pem"
            generate_self_signed_cert(cert, key)
        config.load_cert_chain(cert, key)
        return config

    async def start(self, host: str = None, port: int = None) -> "Receiver":
        """Bind and start serving, returning immediately (use for embedding/tests)."""
        if host is not None:
            self.host = host
        if port is not None:
            self.port = port
        self._resolve_debug()
        config = self._configure()
        self._server = await serve(
            self.host, self.port, configuration=config,
            create_protocol=partial(RPCServerProtocol, server=self),
        )
        sock = self._server._transport.get_extra_info("socket")
        if sock is not None:
            self.port = sock.getsockname()[1]
        self._is_running = True
        return self

    async def run(self, host: str = None, port: int = None) -> None:
        """Start serving and block until cancelled/interrupted."""
        await self.start(host, port)
        print(f"ezRPC server running on {self.host}:{self.port}")
        try:
            await asyncio.Future()
        except (asyncio.CancelledError, KeyboardInterrupt):
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        for task in list(self._background):
            if not task.done():
                task.cancel()
        self._background.clear()
        self._is_running = False

    @property
    def is_running(self) -> bool:
        return self._is_running
