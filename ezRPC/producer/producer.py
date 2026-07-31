"""The RPC client.

A ``Producer`` pools one QUIC connection per (host, port) and issues calls over
fresh streams. Connection setup is single-flight (concurrent first calls share
one connection instead of each opening — and leaking — their own), and calls are
encoded/decoded with msgspec throughout.
"""

import ssl
import asyncio
import copy
import uuid
from collections import defaultdict
from contextlib import AsyncExitStack
from functools import partial
from urllib.parse import urlparse
from typing import Any, cast

import certifi
import msgspec
from msgspec.msgpack import Ext
from aioquic.asyncio.client import connect
from aioquic.quic.configuration import QuicConfiguration
from aioquic.tls import SessionTicket

from ezRPC.common.config import (
    ALPN_PROTOCOL, DEFAULT_PORT, DEFAULT_TIMEOUT, STANDARD_CALL, FIRE_AND_FORGET_CALL,
    ResponseFormat, DISCOVER_SYSTEM_PROCEDURE_NAME, PING_SYSTEM_PROCEDURE_NAME,
    ERR_PREFIX_ARGUMENT, ERR_PREFIX_NAME, ERR_PREFIX_RUN, ERR_PREFIX_AUTH, ERR_PREFIX_UNKNOWN_ID,
    HASH_EXT_CODE, HASH_NAME_THRESHOLD, method_hash,
    DEFAULT_IDLE_TIMEOUT, RETRY_BACKOFF_BASE, RETRY_BACKOFF_CAP,
)
from ezRPC.common.exceptions import (
    EzRPCError, TransportError, CallTimeoutError, ArgumentError, ProcedureNameError,
    ProcedureRunError, AuthError, CallError,
)
from ezRPC.producer.protocol import RPCClientProtocol
from ezRPC.producer.stub_proxy import StubProxy

_UNSET = object()


def _parse_target(url: str, port: int | None) -> tuple[str | None, int | None]:
    if not url:
        return None, port
    if "://" in url:
        parsed = urlparse(url)
        return parsed.hostname, (parsed.port or port or DEFAULT_PORT)
    if url.count(":") == 1:  # "host:port" (IPv4 / hostname)
        host, _, raw_port = url.partition(":")
        return host, int(raw_port)
    return url, (port or DEFAULT_PORT)


# wire error prefix -> client-side exception type
_ERROR_TYPES: dict[str, type[CallError]] = {
    ERR_PREFIX_ARGUMENT: ArgumentError,
    ERR_PREFIX_NAME: ProcedureNameError,
    ERR_PREFIX_RUN: ProcedureRunError,
    ERR_PREFIX_AUTH: AuthError,
}
_ERR_PREFIX_LEN = len(ERR_PREFIX_RUN)   # all prefixes share one length


def _map_error(error: str) -> EzRPCError:
    exc_type = _ERROR_TYPES.get(error[:_ERR_PREFIX_LEN])
    if exc_type is None:
        return CallError(error)
    return exc_type(error[_ERR_PREFIX_LEN:])


class Producer:
    def __init__(self, url: str = "", port: int | None = None, *,
                 verify: bool | str = False, timeout: float = DEFAULT_TIMEOUT,
                 hash_first_call: bool = False, auth=None, retries: int = 0,
                 idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
                 keepalive: float | None = None) -> None:
        host, resolved_port = _parse_target(url, port)
        self.host = host
        self.port = resolved_port or DEFAULT_PORT
        self.timeout = timeout
        self.verify = verify
        self.hash_first_call = hash_first_call
        self.retries = retries                  # default transport-error retries per call
        # ping each pooled connection every `keepalive` seconds so it survives idle
        # gaps instead of silently dying at the idle timeout and paying a full
        # re-handshake on the next call. None (default) = no keepalive.
        self.keepalive = keepalive
        self._auth = auth                       # a token str, or a callable() -> token str
        self._hash_cache: dict[str, Ext] = {}   # method name -> precomputed hash Ext

        config = QuicConfiguration(is_client=True, alpn_protocols=[ALPN_PROTOCOL])
        config.idle_timeout = idle_timeout
        if verify is False:
            config.verify_mode = ssl.CERT_NONE
        elif verify is True:
            config.verify_mode = ssl.CERT_REQUIRED
            config.load_verify_locations(cafile=certifi.where())
        else:  # a path to a CA / pinned certificate
            config.verify_mode = ssl.CERT_REQUIRED
            config.load_verify_locations(cafile=verify)
        self._config = config

        self._pool: dict[tuple, RPCClientProtocol] = {}
        self._stacks: dict[tuple, AsyncExitStack] = {}
        self._locks: dict[tuple, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._keepalives: dict[tuple, asyncio.Task] = {}
        # latest TLS session ticket per target: reconnects resume the TLS session
        # (PSK handshake, no certificate re-validation). A consumed/expired ticket
        # simply falls back to a full handshake.
        self._session_tickets: dict[tuple, SessionTicket] = {}
        self.rpc = StubProxy(self)

    def set_auth(self, auth) -> None:
        """Change the auth token (str or a provider callable). The new token is
        re-sent on the next call of each connection."""
        self._auth = auth

    def _current_auth(self) -> str | None:
        auth = self._auth
        return auth() if callable(auth) else auth

    def _auth_to_send(self, proto) -> str | None:
        # send the token only when it differs from what this connection last got
        token = self._current_auth()
        if token is not None and token != proto.sent_auth:
            proto.sent_auth = token
            return token
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _connect(self, host: str | None, port: int | None) -> RPCClientProtocol:
        host = host or self.host
        port = port or self.port
        if host is None:
            raise TransportError("no server host configured on this Producer")
        key = (host, port)

        proto = self._pool.get(key)
        if proto is not None and proto.alive:
            return proto

        async with self._locks[key]:  # single-flight: only one connect per target
            proto = self._pool.get(key)
            if proto is not None and proto.alive:
                return proto
            if proto is not None:  # dead connection lingering in the pool — evict it
                self._pool.pop(key, None)
                stale_ka = self._keepalives.pop(key, None)
                if stale_ka is not None:
                    stale_ka.cancel()
                stale = self._stacks.pop(key, None)
                if stale is not None:
                    try:
                        await stale.aclose()
                    except Exception:
                        pass

            config = self._config
            ticket = self._session_tickets.get(key)
            if ticket is not None:
                # per-attempt copy: the shared config must not carry another
                # target's ticket into a concurrent connect
                config = copy.copy(self._config)
                config.session_ticket = ticket

            stack = AsyncExitStack()
            try:
                proto = cast(RPCClientProtocol, await asyncio.wait_for(
                    stack.enter_async_context(
                        connect(host, port, configuration=config,
                                create_protocol=RPCClientProtocol,
                                session_ticket_handler=partial(self._save_ticket, key))
                    ),
                    timeout=self.timeout,
                ))
            except Exception as e:
                try:
                    await stack.aclose()
                except Exception:
                    pass
                if isinstance(e, asyncio.TimeoutError):
                    raise TransportError(
                        f"connection to {host}:{port} timed out after {self.timeout}s"
                    ) from None
                raise TransportError(
                    f"failed to connect to {host}:{port} — {e.__class__.__name__}: {e}"
                ) from e

            self._pool[key] = proto
            self._stacks[key] = stack
            if self.keepalive:
                self._keepalives[key] = asyncio.create_task(
                    self._keepalive_loop(key, proto, self.keepalive))
            return proto

    def _save_ticket(self, key: tuple, ticket: SessionTicket) -> None:
        # keep only the newest ticket per target; the server issues a fresh one
        # on every handshake, so each reconnect re-arms the next resumption
        self._session_tickets[key] = ticket

    async def _keepalive_loop(self, key: tuple, proto: RPCClientProtocol,
                              interval: float) -> None:
        # QUIC-level PING (no RPC dispatch); any packet resets both idle timers
        try:
            while proto.alive:
                await asyncio.sleep(interval)
                if not proto.alive:
                    break
                try:
                    await proto.ping()
                except Exception:
                    break
        finally:
            if self._keepalives.get(key) is asyncio.current_task():
                self._keepalives.pop(key, None)

    async def call(self, name: str, *args, host: str | None = None, port: int | None = None,
                   timeout=_UNSET, call_type: int = STANDARD_CALL, safe: bool = False,
                   idempotency_key: bytes | str | None = None, retries=_UNSET) -> Any:
        """Call ``name`` with positional ``args``.

        Safe replay: pass ``idempotency_key`` (or ``retries > 0``, which
        auto-generates one key for all attempts of this logical call) and the
        server executes the call at most once per process — a retry after a
        connection drop returns the original outcome instead of re-executing.
        Only transport failures are retried; server outcomes (``CallError``)
        and exhausted time budgets (``CallTimeoutError``) never are."""
        retries = self.retries if retries is _UNSET else int(retries)
        key = idempotency_key
        if isinstance(key, str):        # the wire type is canonically bytes
            key = key.encode("utf-8")
        if key is None and retries > 0:
            key = uuid.uuid4().bytes    # one key across all attempts: never a blind retry
        attempt = 0
        while True:
            try:
                return await self._call_once(name, args, host, port, timeout,
                                             call_type, safe, key)
            except CallTimeoutError:
                raise                   # the attempt consumed its full time budget
            except TransportError:
                if key is None or attempt >= retries:
                    raise
                await asyncio.sleep(min(RETRY_BACKOFF_BASE * (2 ** attempt), RETRY_BACKOFF_CAP))
                attempt += 1

    async def _call_once(self, name: str, args: tuple, host: str | None, port: int | None,
                         timeout, call_type: int, safe: bool,
                         idempotency_key: bytes | None) -> Any:
        timeout = self.timeout if timeout is _UNSET else timeout
        proto = await self._connect(host, port)

        # address by the learned id if we have one for this connection; otherwise by name
        # (or by a compact 64-bit hash on the first call, when hash_first_call is on)
        cached_id = proto.method_ids.get(name)
        method = cached_id if cached_id is not None else self._first_call_method(name)
        auth = self._auth_to_send(proto)

        if int(call_type) == int(FIRE_AND_FORGET_CALL):
            await self._send(proto, method, name, args, call_type, auth, False,
                             timeout, idempotency_key)
            return None

        resp = await self._exchange(proto, method, name, args, call_type, auth,
                                    timeout, idempotency_key)

        # stale id: we addressed by id but the server didn't recognize it — resend by name
        if (isinstance(method, int) and resp.error is not None
                and resp.error.startswith(ERR_PREFIX_UNKNOWN_ID)):
            proto.method_ids.pop(name, None)
            proto.sent_auth = None            # server bounced before caching auth — resend it
            auth = self._auth_to_send(proto)
            resp = await self._exchange(proto, name, name, args, call_type, auth,
                                        timeout, idempotency_key)

        if safe:
            return resp
        if resp.error is not None:
            raise _map_error(resp.error)
        return resp.data

    async def _exchange(self, proto, method, name: str, args: tuple, call_type: int,
                        auth: str | None, timeout, idempotency_key: bytes | None) -> ResponseFormat:
        """One request/response round-trip: send, decode, and learn the method id
        the server may have taught us."""
        raw = await self._send(proto, method, name, args, call_type, auth, True,
                               timeout, idempotency_key)
        resp = self._decode_response(raw)
        if resp.method_id is not None:
            proto.method_ids[name] = resp.method_id
        return resp

    def _first_call_method(self, name: str):
        # long names go out as a compact hash on the first call (before the id is known);
        # short names stay as strings (a hash would be bigger). System names stay strings.
        if self.hash_first_call and len(name) >= HASH_NAME_THRESHOLD:
            ext = self._hash_cache.get(name)
            if ext is None:
                ext = Ext(HASH_EXT_CODE, method_hash(name))
                self._hash_cache[name] = ext
            return ext
        return name

    async def _send(self, proto, method, name, args, call_type, auth, await_result,
                    timeout, idempotency_key=None):
        try:
            if idempotency_key is not None:
                payload = msgspec.msgpack.encode(
                    [method, int(call_type), args, auth, idempotency_key])
            elif auth is not None:
                payload = msgspec.msgpack.encode([method, int(call_type), args, auth])
            else:
                payload = msgspec.msgpack.encode([method, int(call_type), args])
        except (msgspec.EncodeError, TypeError) as e:
            raise ArgumentError(f"unsupported argument type in call to '{name}': {e}") from e
        return await proto.request(payload, await_result=await_result, timeout=timeout)

    @staticmethod
    def _decode_response(raw: bytes) -> ResponseFormat:
        try:
            return msgspec.msgpack.decode(raw, type=ResponseFormat)
        except msgspec.DecodeError as e:
            raise TransportError(f"malformed response from server: {e}") from e

    async def call_safe(self, name: str, *args, host: str | None = None, port: int | None = None,
                        timeout=_UNSET, idempotency_key: bytes | str | None = None,
                        retries=_UNSET) -> ResponseFormat:
        return await self.call(name, *args, host=host, port=port, timeout=timeout, safe=True,
                               idempotency_key=idempotency_key, retries=retries)

    async def discover(self, host: str | None = None, port: int | None = None) -> dict:
        return await self.call(DISCOVER_SYSTEM_PROCEDURE_NAME, host=host, port=port)

    async def ping(self, host: str | None = None, port: int | None = None):
        return await self.call(PING_SYSTEM_PROCEDURE_NAME, host=host, port=port)

    async def close(self) -> None:
        for task in self._keepalives.values():
            task.cancel()
        self._keepalives.clear()
        for key in list(self._stacks):
            stack = self._stacks.pop(key, None)
            self._pool.pop(key, None)
            if stack is not None:
                try:
                    await stack.aclose()
                except Exception:
                    pass
        self._locks.clear()
