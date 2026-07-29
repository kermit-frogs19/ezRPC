"""The RPC client.

A ``Producer`` pools one QUIC connection per (host, port) and issues calls over
fresh streams. Connection setup is single-flight (concurrent first calls share
one connection instead of each opening — and leaking — their own), and calls are
encoded/decoded with msgspec throughout.
"""

import ssl
import asyncio
from collections import defaultdict
from contextlib import AsyncExitStack
from urllib.parse import urlparse
from typing import Any

import certifi
import msgspec
from msgspec.msgpack import Ext
from aioquic.asyncio.client import connect
from aioquic.quic.configuration import QuicConfiguration

from ezRPC.common.config import (
    ALPN_PROTOCOL, DEFAULT_PORT, DEFAULT_TIMEOUT, STANDARD_CALL, FIRE_AND_FORGET_CALL,
    ResponseFormat, DISCOVER_SYSTEM_PROCEDURE_NAME, PING_SYSTEM_PROCEDURE_NAME,
    ERR_PREFIX_ARGUMENT, ERR_PREFIX_NAME, ERR_PREFIX_RUN, ERR_PREFIX_AUTH, ERR_PREFIX_UNKNOWN_ID,
    HASH_EXT_CODE, HASH_NAME_THRESHOLD, method_hash,
)
from ezRPC.common.exceptions import (
    EzRPCError, TransportError, ArgumentError, ProcedureNameError, ProcedureRunError,
    AuthError, CallError,
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


def _map_error(error: str) -> EzRPCError:
    if error.startswith(ERR_PREFIX_ARGUMENT):
        return ArgumentError(error[2:])
    if error.startswith(ERR_PREFIX_NAME):
        return ProcedureNameError(error[2:])
    if error.startswith(ERR_PREFIX_RUN):
        return ProcedureRunError(error[2:])
    if error.startswith(ERR_PREFIX_AUTH):
        return AuthError(error[2:])
    return CallError(error)


class Producer:
    def __init__(self, url: str = "", port: int | None = None, *,
                 verify: bool | str = False, timeout: float = DEFAULT_TIMEOUT,
                 hash_first_call: bool = False, auth=None) -> None:
        host, resolved_port = _parse_target(url, port)
        self.host = host
        self.port = resolved_port or DEFAULT_PORT
        self.timeout = timeout
        self.verify = verify
        self.hash_first_call = hash_first_call
        self._auth = auth                       # a token str, or a callable() -> token str
        self._hash_cache: dict[str, Ext] = {}   # method name -> precomputed hash Ext

        config = QuicConfiguration(is_client=True, alpn_protocols=[ALPN_PROTOCOL])
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
        self.rpc = StubProxy(self)

    def set_auth(self, auth) -> None:
        """Change the auth token (str or a provider callable). The new token is
        re-sent on the next call of each connection."""
        self._auth = auth

    def _current_auth(self) -> str | None:
        a = self._auth
        return a() if callable(a) else a

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
                stale = self._stacks.pop(key, None)
                if stale is not None:
                    try:
                        await stale.aclose()
                    except Exception:
                        pass

            stack = AsyncExitStack()
            try:
                proto = await asyncio.wait_for(
                    stack.enter_async_context(
                        connect(host, port, configuration=self._config,
                                create_protocol=RPCClientProtocol)
                    ),
                    timeout=self.timeout,
                )
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
            return proto

    async def call(self, name: str, *args, host: str = None, port: int = None,
                   timeout=_UNSET, call_type: int = STANDARD_CALL, safe: bool = False) -> Any:
        timeout = self.timeout if timeout is _UNSET else timeout
        proto = await self._connect(host, port)
        await_result = int(call_type) != int(FIRE_AND_FORGET_CALL)

        # address by the learned id if we have one for this connection; otherwise by name
        # (or by a compact 64-bit hash on the first call, when hash_first_call is on)
        cached_id = proto.method_ids.get(name)
        method = cached_id if cached_id is not None else self._first_call_method(name)
        auth = self._auth_to_send(proto)
        raw = await self._send(proto, method, name, args, call_type, auth, await_result, timeout)
        if not await_result:
            return None

        resp = self._decode_response(raw)
        if resp.method_id is not None:               # the server just taught us this method's id
            proto.method_ids[name] = resp.method_id

        # stale id: we addressed by id but the server didn't recognize it — resend by name
        if (isinstance(method, int) and resp.error is not None
                and resp.error.startswith(ERR_PREFIX_UNKNOWN_ID)):
            proto.method_ids.pop(name, None)
            proto.sent_auth = None            # server bounced before caching auth — resend it
            auth = self._auth_to_send(proto)
            raw = await self._send(proto, name, name, args, call_type, auth, await_result, timeout)
            resp = self._decode_response(raw)
            if resp.method_id is not None:
                proto.method_ids[name] = resp.method_id

        if safe:
            return resp
        if resp.error is not None:
            raise _map_error(resp.error)
        return resp.data

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

    async def _send(self, proto, method, name, args, call_type, auth, await_result, timeout):
        try:
            if auth is None:
                payload = msgspec.msgpack.encode([method, int(call_type), args])
            else:
                payload = msgspec.msgpack.encode([method, int(call_type), args, auth])
        except (msgspec.EncodeError, TypeError) as e:
            raise ArgumentError(f"unsupported argument type in call to '{name}': {e}") from e
        return await proto.request(payload, await_result=await_result, timeout=timeout)

    @staticmethod
    def _decode_response(raw: bytes) -> ResponseFormat:
        try:
            return msgspec.msgpack.decode(raw, type=ResponseFormat)
        except msgspec.DecodeError as e:
            raise TransportError(f"malformed response from server: {e}") from e

    async def call_safe(self, name: str, *args, host: str = None, port: int = None,
                        timeout=_UNSET) -> ResponseFormat:
        return await self.call(name, *args, host=host, port=port, timeout=timeout, safe=True)

    async def discover(self, host: str = None, port: int = None) -> dict:
        return await self.call(DISCOVER_SYSTEM_PROCEDURE_NAME, host=host, port=port)

    async def ping(self, host: str = None, port: int = None):
        return await self.call(PING_SYSTEM_PROCEDURE_NAME, host=host, port=port)

    async def close(self) -> None:
        for key in list(self._stacks):
            stack = self._stacks.pop(key, None)
            self._pool.pop(key, None)
            if stack is not None:
                try:
                    await stack.aclose()
                except Exception:
                    pass
        self._locks.clear()
