"""The RPC server.

Register plain functions with ``@app.function()`` (or ``add_class_instance``),
then ``await app.run(...)``. The server runs on raw QUIC via aioquic. Handler
exceptions are logged in full server-side but returned to the caller as a generic
message with a reference id (unless ``debug`` is on) so internal detail never
leaks over the wire.
"""

import asyncio
import logging
import time
import uuid
from collections import deque
from functools import partial
from pathlib import Path
from typing import Callable, Any

import msgspec
import xxhash
from msgspec.msgpack import Ext
from aioquic.asyncio.server import serve
from aioquic.quic.configuration import QuicConfiguration
from aioquic.tls import SessionTicket

from ezRPC.common.config import (
    ALPN_PROTOCOL, DEFAULT_HOST, DEFAULT_PORT, FIRE_AND_FORGET_CALL, NOT_AWAITED_RUN_CALL,
    CallEnvelope, DISCOVER_SYSTEM_PROCEDURE_NAME, PING_SYSTEM_PROCEDURE_NAME,
    ERR_PREFIX_ARGUMENT, ERR_PREFIX_NAME, ERR_PREFIX_RUN, ERR_PREFIX_AUTH, ERR_PREFIX_UNKNOWN_ID,
    HASH_EXT_CODE, method_hash,
    DEFAULT_IDLE_TIMEOUT, DEFAULT_SHUTDOWN_GRACE, DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_MAX_BACKGROUND_CALLS, DEFAULT_IDEMPOTENCY_TTL, DEFAULT_IDEMPOTENCY_MAX_ENTRIES,
    DEFAULT_IDEMPOTENCY_MAX_RESPONSE_BYTES, DEFAULT_IDEMPOTENCY_MAX_BYTES,
    SESSION_TICKET_STORE_SIZE,
)
from ezRPC.common.certificate import generate_self_signed_cert
from ezRPC.common.context import CallContext, _current as _current_context
from ezRPC.common.exceptions import AuthError
from ezRPC.receiver.function_handler import FunctionHandler
from ezRPC.receiver.protocol import RPCServerProtocol

logger = logging.getLogger("ezrpc")

_LOCALHOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"}
_ERROR_REF_HEX_CHARS = 8    # length of the reference id linking a client error to its log line
_AT_CAPACITY_ERROR = f"{ERR_PREFIX_RUN}server is at background-call capacity"


class _UnknownMethod(Exception):
    """Internal: carries the wire error string for a method that resolved to nothing."""

    def __init__(self, wire_error: str):
        self.wire_error = wire_error


class _SessionTicketStore:
    """Bounded in-memory store of TLS session tickets for resumption.

    Tickets are single-use: a fetched ticket is popped, so a captured handshake
    cannot be replayed against the store. Oldest tickets evict beyond capacity."""

    def __init__(self, max_entries: int = SESSION_TICKET_STORE_SIZE):
        self._max_entries = max_entries
        self._tickets: dict[bytes, SessionTicket] = {}

    def add(self, ticket: SessionTicket) -> None:
        self._tickets[ticket.ticket] = ticket
        while len(self._tickets) > self._max_entries:
            del self._tickets[next(iter(self._tickets))]     # FIFO: oldest first

    def pop(self, label: bytes) -> SessionTicket | None:
        return self._tickets.pop(label, None)


def _announce(message: str) -> None:
    """Emit a lifecycle message that is always visible.

    Logged at INFO; if the application never configured logging (no real handler
    anywhere in the logger hierarchy), fall back to ``print`` so server startup
    and shutdown are never silent. Once logging *is* configured, its routing and
    levels are respected — no double output, no forced lines."""
    logger.info(message)
    lg: logging.Logger | None = logger
    while lg is not None:
        if any(not isinstance(h, logging.NullHandler) for h in lg.handlers):
            return
        if not lg.propagate:
            break
        lg = lg.parent
    print(message)


class _IdemEntry:
    """One idempotency-window slot: the outcome of a keyed call.

    ``future`` is the in-flight execution task while running; ``response`` holds
    the recorded response bytes once complete (success *or* error — a failed
    attempt is an outcome, not a license to re-execute). ``fingerprint`` hashes
    method+args so a reused key with a different call is rejected instead of
    silently replaying the wrong outcome."""
    __slots__ = ("fingerprint", "future", "response", "done_at")

    def __init__(self, fingerprint: bytes, future: asyncio.Task | None = None):
        self.fingerprint = fingerprint
        self.future = future
        self.response: bytes | None = None
        self.done_at: float | None = None


class Receiver:
    def __init__(
            self,
            host: str = DEFAULT_HOST,
            port: int = DEFAULT_PORT, *,
            cert_file: str | None = None,
            key_file: str | None = None,
            debug: bool | None = None,
            max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
            idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
            shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE,
            max_background_calls: int = DEFAULT_MAX_BACKGROUND_CALLS,
            idempotency_ttl: float = DEFAULT_IDEMPOTENCY_TTL,
            idempotency_max_entries: int = DEFAULT_IDEMPOTENCY_MAX_ENTRIES,
            idempotency_max_response_bytes: int = DEFAULT_IDEMPOTENCY_MAX_RESPONSE_BYTES,
            idempotency_max_bytes: int = DEFAULT_IDEMPOTENCY_MAX_BYTES,
    ) -> None:
        self.host = host
        self.port = port
        self.cert_file = cert_file
        self.key_file = key_file
        self.debug = debug
        self.max_request_bytes = max_request_bytes
        self.idle_timeout = idle_timeout
        self.shutdown_grace = shutdown_grace     # default drain budget for shutdown()
        # cap on concurrently tracked background work (fire-and-forget, not-awaited,
        # and keyed executions, which outlive their streams by design)
        self.max_background_calls = max_background_calls
        self.idempotency_ttl = idempotency_ttl
        self.idempotency_max_entries = idempotency_max_entries
        # outcomes larger than this are executed+deduped but not retained for replay
        self.idempotency_max_response_bytes = idempotency_max_response_bytes
        # total bytes the window may hold; oldest completed entries evict beyond it
        self.idempotency_max_bytes = idempotency_max_bytes

        self.functions: dict[str, FunctionHandler] = {}
        self._id_to_handler: list[FunctionHandler] = []  # method id -> handler (list index)
        self._hash_to_handler: dict[bytes, FunctionHandler] = {}  # 64-bit name hash -> handler
        self.system_functions: dict[str, Callable] = {
            DISCOVER_SYSTEM_PROCEDURE_NAME: self.discover,
            PING_SYSTEM_PROCEDURE_NAME: self.ping,
        }
        self._before_call: Callable | None = None
        self._server: Any = None                 # aioquic QuicServer (we touch its privates)
        self._session_tickets = _SessionTicketStore()   # enables TLS session resumption
        self._protocols: set = set()             # live connections (for graceful shutdown)
        self._background: set[asyncio.Task] = set()
        self._is_running = False
        self._debug_effective = bool(debug)
        # idempotency window: key -> outcome; the deque tracks completion order
        # for TTL/size eviction (in-progress entries are never evicted)
        self._idem: dict[bytes, _IdemEntry] = {}
        self._idem_done: deque = deque()
        self._idem_bytes = 0                     # total recorded-response bytes in the window

    # ---- registration ----
    def add_function(
            self,
            func: Callable,
            name: str | None = None,
            await_result: bool = True,
            description: str | None = None,
            discovery: bool = True
    ) -> None:
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

        name_hash = method_hash(function_name)   # 64-bit name hash + startup collision check
        clash = self._hash_to_handler.get(name_hash)
        if clash is not None and clash.name != function_name:
            raise RuntimeError(
                f"method-name hash collision between '{function_name}' and '{clash.name}' "
                f"(identical 64-bit hash) — rename one of them."
            )
        self._hash_to_handler[name_hash] = handler

        self.functions[function_name] = handler

    def function(
            self,
            name: str | None = None,
            await_result: bool = True,
            description: str | None = None,
            discovery: bool = True
    ) -> Callable:
        def decorator(func: Callable):
            self.add_function(func, name, await_result, description, discovery)
            return func
        return decorator

    def add_class_instance(
            self,
            instance: Any,
            name: str | None = None,
            await_result: bool = True,
            description: str | None = None,
            discovery: bool = True
    ) -> None:
        name = instance.__class__.__name__ if name is None else name
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

    def before_call(
            self,
            func: Callable
    ) -> Callable:
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
    def ok_bytes(
            data: Any,
            method_id: int | None = None
    ) -> bytes:
        if method_id is None:
            return msgspec.msgpack.encode([None, data])
        return msgspec.msgpack.encode([None, data, method_id])

    @staticmethod
    def error_bytes(
            error: str,
            method_id: int | None = None
    ) -> bytes:
        if method_id is None:
            return msgspec.msgpack.encode([error, None])
        return msgspec.msgpack.encode([error, None, method_id])

    def _safe_error(self, exc: BaseException) -> str:
        if self._debug_effective:
            return f"{type(exc).__name__}: {exc}"
        ref = uuid.uuid4().hex[:_ERROR_REF_HEX_CHARS]
        logger.exception("call failed [ref %s]", ref, exc_info=exc)
        return f"internal error (ref {ref})"

    # ---- dispatch: bytes in, response bytes out (b"" = no response / fire-and-forget) ----
    async def dispatch(self, data: bytes, connection: RPCServerProtocol) -> bytes:
        try:
            env = msgspec.msgpack.decode(data, type=CallEnvelope)
        except (msgspec.DecodeError, msgspec.ValidationError):
            return self.error_bytes(f"{ERR_PREFIX_ARGUMENT}malformed call payload")

        if env.auth is not None:               # client (re)sent its token; cache it on the connection
            connection.auth = env.auth

        fire = env.call_type == int(FIRE_AND_FORGET_CALL)
        try:
            handler, system, name, echo_id = self._resolve_target(env.method)
        except _UnknownMethod as e:
            return b"" if fire else self.error_bytes(e.wire_error)

        ctx = CallContext(
            method=name,
            auth=connection.auth,
            connection_state=connection.connection_state,
            call_type=env.call_type
        )
        _current_context.set(ctx)

        if self._before_call is not None:
            try:
                rejection = await self._before_call(ctx)
            except Exception as e:
                return b"" if fire else self.error_bytes(f"{ERR_PREFIX_AUTH}{self._safe_error(e)}", echo_id)
            if rejection is not None:
                return b"" if fire else self.error_bytes(str(rejection), echo_id)

        if system is not None:
            try:
                result = await system()
            except Exception as e:
                return b"" if fire else self.error_bytes(f"{ERR_PREFIX_RUN}{self._safe_error(e)}")
            return b"" if fire else self.ok_bytes(result)

        assert handler is not None  # every non-system path above resolved a handler or returned

        try:
            wire_args = handler.decode_args(env.args)
        except (msgspec.DecodeError, msgspec.ValidationError, TypeError) as e:
            return b"" if fire else self.error_bytes(f"{ERR_PREFIX_ARGUMENT}{e}", echo_id)

        # resolve auth/Security dependencies before we ack or spawn anything
        try:
            full_args = await handler.build_args(ctx, wire_args)
        except AuthError as e:
            logger.debug("call '%s' rejected by auth", name)
            return b"" if fire else self.error_bytes(f"{ERR_PREFIX_AUTH}{e}", echo_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a buggy scheme is a server error, not an auth decision — log it
            return b"" if fire else self.error_bytes(f"{ERR_PREFIX_RUN}{self._safe_error(e)}", echo_id)

        if env.idempotency_key is not None:
            return await self._dispatch_keyed(handler, full_args, env, name, fire, echo_id)

        if fire or env.call_type == int(NOT_AWAITED_RUN_CALL) or not handler.await_result:
            if self._background_full():
                logger.warning("background call to '%s' rejected: at capacity (%d)",
                               name, self.max_background_calls)
                return b"" if fire else self.error_bytes(_AT_CAPACITY_ERROR, echo_id)
            self._spawn(handler, full_args)
            return b"" if fire else self.ok_bytes(None, echo_id)

        return await self._run_handler(handler, full_args, name, echo_id)

    def _resolve_target(
            self, method: int | str | Ext,
    ) -> tuple[FunctionHandler | None, Callable | None, str, int | None]:
        """Resolve the three wire addressing modes — server-assigned id (int),
        64-bit name hash (Ext), or name (str) — to a handler or system function.

        Returns ``(handler, system, name, echo_id)``; ``echo_id`` is the method
        id to teach the client, sent only when it did not address us by id.
        Raises ``_UnknownMethod`` when nothing matches — nothing is executed, so
        a stale id can never misroute a call."""
        if isinstance(method, int):
            in_range = 0 <= method < len(self._id_to_handler)
            handler = self._id_to_handler[method] if in_range else None
            if handler is None:
                # unknown id: the client falls back to resending by name
                raise _UnknownMethod(f"{ERR_PREFIX_UNKNOWN_ID}unknown method id {method}")
            return handler, None, handler.name, None

        if isinstance(method, Ext):
            handler = (self._hash_to_handler.get(bytes(method.data))
                       if method.code == HASH_EXT_CODE else None)
            if handler is None:
                raise _UnknownMethod(f"{ERR_PREFIX_NAME}unknown function (unresolved method hash)")
            return handler, None, handler.name, handler.method_id

        system = self.system_functions.get(method)
        if system is not None:
            return None, system, method, None
        handler = self.functions.get(method)
        if handler is None:
            raise _UnknownMethod(f"{ERR_PREFIX_NAME}unknown function '{method}'")
        return handler, None, method, handler.method_id

    async def _run_handler(self, handler: FunctionHandler, full_args: list,
                           name: str, echo_id: int | None) -> bytes:
        """Run the handler and encode its outcome (success or error) as response bytes."""
        started = time.perf_counter()
        try:
            result = await handler.call_with(full_args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("call '%s' failed in %.2f ms", name, (time.perf_counter() - started) * 1000)
            return self.error_bytes(f"{ERR_PREFIX_RUN}{self._safe_error(e)}", echo_id)
        logger.debug("call '%s' ok in %.2f ms", name, (time.perf_counter() - started) * 1000)
        return self.ok_bytes(result, echo_id)

    # ---- safe replay (idempotency) ----
    async def _dispatch_keyed(self, handler: FunctionHandler, full_args: list,
                              env: CallEnvelope, name: str, fire: bool,
                              echo_id: int | None) -> bytes:
        """Dispatch a call carrying an idempotency key.

        Contract: the call is executed to completion **at most once per server
        process** within the window. Duplicates join the in-flight execution or
        replay the recorded outcome — errors included, since a failed attempt
        may have committed a partial side effect and re-running is not safe.
        The execution runs in a connection-independent task, so a client
        disconnect, cancel, or timeout can never kill it mid-side-effect and
        leave a retry to re-execute half-done work."""
        assert env.idempotency_key is not None  # only dispatched here when a key is present
        key = bytes(env.idempotency_key)
        fingerprint = xxhash.xxh3_64_digest(name.encode("utf-8") + b"\x00" + bytes(env.args))
        self._purge_idempotency_window()

        entry = self._idem.get(key)
        if entry is not None:
            if entry.fingerprint != fingerprint:
                return b"" if fire else self.error_bytes(
                    f"{ERR_PREFIX_ARGUMENT}idempotency key reused with a different "
                    f"method or arguments", echo_id)
            if fire:
                return b""                       # already executing/executed: never re-run
            if entry.response is not None:       # completed: replay the recorded outcome
                logger.debug("call '%s' replayed from the idempotency window", name)
                return entry.response
            logger.debug("call '%s' joining the in-flight execution", name)
            future = entry.future
            assert future is not None  # in-progress entries always carry their task
            return await asyncio.shield(future)

        # first sighting only: replays above cost nothing even at capacity
        if self._background_full():
            return b"" if fire else self.error_bytes(_AT_CAPACITY_ERROR, echo_id)

        if fire or env.call_type == int(NOT_AWAITED_RUN_CALL) or not handler.await_result:
            # spawn-type calls: the ack is the recorded outcome; the execution is
            # already connection-independent via _spawn
            entry = _IdemEntry(fingerprint)
            entry.response = self.ok_bytes(None, echo_id)
            entry.done_at = time.monotonic()
            self._idem[key] = entry
            self._idem_bytes += len(entry.response)
            self._idem_done.append((entry.done_at, key))
            self._spawn(handler, full_args)
            return b"" if fire else entry.response

        task = asyncio.ensure_future(self._run_handler(handler, full_args, name, echo_id))
        self._background.add(task)               # drained by graceful shutdown
        entry = _IdemEntry(fingerprint, future=task)
        self._idem[key] = entry

        def _done(t: asyncio.Task) -> None:
            self._background.discard(t)
            if t.cancelled() or t.exception() is not None:
                # never completed (shutdown): forget the key so a retry may re-execute
                if self._idem.get(key) is entry:
                    del self._idem[key]
                return
            response = t.result()
            if len(response) > self.idempotency_max_response_bytes:
                # keep the dedup guarantee, drop the oversized recorded outcome
                response = self.error_bytes(
                    f"{ERR_PREFIX_RUN}the call executed exactly once, but its recorded "
                    f"outcome was too large to retain for replay", echo_id)
            entry.response = response
            entry.done_at = time.monotonic()
            self._idem_bytes += len(response)
            self._idem_done.append((entry.done_at, key))

        task.add_done_callback(_done)
        # shield: cancelling this *stream* (client cancel/timeout/disconnect)
        # must not cancel the execution itself
        return await asyncio.shield(task)

    def _background_full(self) -> bool:
        return len(self._background) >= self.max_background_calls

    def _purge_idempotency_window(self) -> None:
        """Evict completed window entries past their TTL, and the oldest completed
        entries when over the entry or byte capacity. In-progress entries are
        never evicted."""
        now = time.monotonic()
        done_queue = self._idem_done
        while done_queue and (now - done_queue[0][0] >= self.idempotency_ttl
                              or len(self._idem) > self.idempotency_max_entries
                              or self._idem_bytes > self.idempotency_max_bytes):
            done_at, key = done_queue.popleft()
            entry = self._idem.get(key)
            if entry is not None and entry.done_at == done_at:
                self._idem_bytes -= len(entry.response or b"")
                del self._idem[key]

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
        config.idle_timeout = self.idle_timeout
        if self.cert_file and self.key_file:
            cert, key = self.cert_file, self.key_file
        else:
            cert, key = "cert.pem", "key.pem"
            generate_self_signed_cert(cert, key)
        config.load_cert_chain(Path(cert), Path(key))
        return config

    async def start(self, host: str | None = None, port: int | None = None) -> "Receiver":
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
            session_ticket_fetcher=self._session_tickets.pop,
            session_ticket_handler=self._session_tickets.add,
        )
        sock = self._server._transport.get_extra_info("socket")
        if sock is not None:
            self.port = sock.getsockname()[1]
        self._is_running = True
        _announce(f"ezRPC server listening on {self.host}:{self.port}")
        return self

    async def run(self, host: str | None = None, port: int | None = None) -> None:
        """Start serving and block until cancelled/interrupted."""
        await self.start(host, port)
        try:
            await asyncio.Future()
        except (asyncio.CancelledError, KeyboardInterrupt):
            await self.shutdown()

    async def shutdown(self, grace: float | None = None) -> None:
        """Stop the server. In-flight calls (and spawned background calls) get up
        to ``grace`` seconds to finish and flush their responses; stragglers are
        cancelled, then connections are closed and the socket released.

        ``grace`` defaults to the Receiver's configured ``shutdown_grace``; pass
        an explicit value (e.g. ``0`` for an abrupt stop) to override per call."""
        if grace is None:
            grace = self.shutdown_grace
        was_running = self._server is not None
        self._is_running = False

        pending = [t for p in list(self._protocols) for t in list(p._tasks.values())]
        pending += list(self._background)
        pending = [t for t in pending if not t.done()]
        if pending and grace > 0:
            logger.info("shutdown: waiting up to %.1fs for %d in-flight call(s)", grace, len(pending))
            await asyncio.wait(pending, timeout=grace)

        # close connections cleanly while the shared socket is still open, so
        # clients get CONNECTION_CLOSE instead of silence
        for proto in list(self._protocols):
            try:
                proto._cancel_all()
                proto.close()
            except Exception:
                pass
        self._protocols.clear()

        if self._server is not None:
            self._server.close()
            self._server = None
        for task in list(self._background):
            if not task.done():
                task.cancel()
        self._background.clear()
        if was_running:  # announce once, not on redundant shutdown() calls
            _announce("ezRPC server stopped")

    @property
    def is_running(self) -> bool:
        return self._is_running
