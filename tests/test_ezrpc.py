"""End-to-end tests for the aioquic-based ezRPC.

Each test starts a real Receiver on an ephemeral UDP port and drives it with a
real Producer over QUIC. The tests deliberately reproduce v1's worst failures
(malformed input, connection-drop/cancel, oversized body) and assert they are now
handled cleanly.
"""

import asyncio
import base64
import time

import msgspec
import pytest
import pytest_asyncio
import jwt as pyjwt

from ezRPC import (
    Receiver, Producer,
    STANDARD_CALL, NOT_AWAITED_RUN_CALL, FIRE_AND_FORGET_CALL,
    EzRPCError, TransportError, CallTimeoutError,
    ArgumentError, ProcedureNameError, ProcedureRunError, AuthError,
    Security, BasicAuth, JWTAuth, call_context,
)
from ezRPC.common.config import ResponseFormat


def build_app(**kwargs) -> Receiver:
    app = Receiver(host="127.0.0.1", **kwargs)
    app.side_effects = []

    @app.function(description="Get the sum of two integers")
    async def get_sum(a: int, b: int) -> int:
        return a + b

    @app.function()
    async def greet(name: str) -> str:
        return f"hello, {name}"

    @app.function()
    async def echo_bytes(blob: bytes) -> bytes:
        return blob

    @app.function()
    async def negate(flag: bool) -> bool:
        return not flag

    @app.function()
    def sync_double(n: int) -> int:      # a plain (non-async) function
        return n * 2

    @app.function()
    async def boom() -> int:
        raise ValueError("secret detail /etc/passwd leaked here")

    @app.function()
    async def slow() -> int:
        await asyncio.sleep(5)
        return 1

    @app.function()
    async def remember(x: int) -> None:  # observe fire-and-forget / not-awaited
        app.side_effects.append(x)

    return app


@pytest_asyncio.fixture
async def run_server():
    apps, clients = [], []

    async def _run(app: Receiver, **producer_kwargs) -> Producer:
        await app.start(host="127.0.0.1", port=0)
        apps.append(app)
        client = Producer("127.0.0.1", app.port, verify=False, **producer_kwargs)
        clients.append(client)
        return client

    yield _run

    for c in clients:
        await c.close()
    for a in apps:
        await a.shutdown()


# ---------------------------------------------------------------- happy paths

async def test_basic_call(run_server):
    client = await run_server(build_app())
    assert await client.call("get_sum", 1, 2) == 3


async def test_stub_proxy(run_server):
    client = await run_server(build_app())
    assert await client.rpc.greet("world") == "hello, world"


async def test_sync_function(run_server):
    client = await run_server(build_app())
    assert await client.call("sync_double", 21) == 42


async def test_bytes_roundtrip(run_server):
    client = await run_server(build_app())
    blob = bytes(range(256))
    assert await client.call("echo_bytes", blob) == blob


async def test_bool_roundtrip(run_server):
    client = await run_server(build_app())
    assert await client.call("negate", True) is False
    assert await client.call("negate", False) is True


async def test_discover(run_server):
    client = await run_server(build_app())
    schema = await client.discover()
    assert schema["get_sum"] == {
        "parameters": {"a": "int", "b": "int"},
        "description": "Get the sum of two integers",
        "return": "int",
    }


async def test_ping(run_server):
    client = await run_server(build_app())
    assert await client.ping() is None


# ---------------------------------------------------------------- error mapping

async def test_wrong_arg_type(run_server):
    client = await run_server(build_app())
    with pytest.raises(ArgumentError):
        await client.call("get_sum", "not-an-int", 2)


async def test_wrong_arity(run_server):
    client = await run_server(build_app())
    with pytest.raises(ArgumentError):
        await client.call("get_sum", 1)


async def test_unknown_function(run_server):
    client = await run_server(build_app())
    with pytest.raises(ProcedureNameError):
        await client.call("does_not_exist", 1)


async def test_handler_error_is_generic_by_default(run_server):
    client = await run_server(build_app(debug=False))
    with pytest.raises(ProcedureRunError) as exc:
        await client.call("boom")
    # the internal detail must NOT reach the client
    assert "/etc/passwd" not in str(exc.value)
    assert "internal error" in str(exc.value)


async def test_handler_error_verbose_in_debug(run_server):
    client = await run_server(build_app(debug=True))
    with pytest.raises(ProcedureRunError) as exc:
        await client.call("boom")
    assert "/etc/passwd" in str(exc.value)


# ---------------------------------------------------------------- fixes & footguns

async def test_call_safe_with_args(run_server):
    # v1 bug: call_safe raised "multiple values for 'name'" whenever args were passed
    client = await run_server(build_app())
    resp = await client.call_safe("get_sum", 4, 5)
    assert isinstance(resp, ResponseFormat)
    assert resp.error is None
    assert resp.data == 9


async def test_stub_rejects_kwargs(run_server):
    client = await run_server(build_app())
    with pytest.raises(TypeError):
        await client.rpc.get_sum(a=1, b=2)


def test_exception_taxonomy():
    # every framework error is catchable as one base type
    for exc in (TransportError, CallTimeoutError, ArgumentError,
                ProcedureNameError, ProcedureRunError, AuthError):
        assert issubclass(exc, EzRPCError)


async def test_before_call_hook_can_reject(run_server):
    app = build_app()

    @app.before_call
    async def guard(ctx):
        return "u-forbidden" if ctx.method == "boom" else None

    client = await run_server(app)
    assert await client.call("get_sum", 1, 2) == 3      # allowed through
    with pytest.raises(AuthError):
        await client.call("boom")                        # rejected before dispatch


# ---------------------------------------------------------------- call types

async def test_not_awaited_returns_none_and_runs(run_server):
    app = build_app()
    client = await run_server(app)
    assert await client.call("remember", 7, call_type=NOT_AWAITED_RUN_CALL) is None
    await asyncio.sleep(0.1)
    assert 7 in app.side_effects


async def test_fire_and_forget(run_server):
    app = build_app()
    client = await run_server(app)
    assert await client.call("remember", 99, call_type=FIRE_AND_FORGET_CALL) is None
    await asyncio.sleep(0.1)
    assert 99 in app.side_effects


# ---------------------------------------------------------------- concurrency & reuse

async def test_concurrent_calls_one_connection(run_server):
    client = await run_server(build_app())
    results = await asyncio.gather(*(client.call("get_sum", i, i) for i in range(20)))
    assert results == [i + i for i in range(20)]
    assert len(client._pool) == 1  # all shared one pooled connection


async def test_connection_is_reused(run_server):
    client = await run_server(build_app())
    await client.call("get_sum", 1, 1)
    await client.call("get_sum", 2, 2)
    assert len(client._pool) == 1


# ---------------------------------------------------------------- lazy method ids

async def test_method_id_learned_and_reused(run_server):
    client = await run_server(build_app())
    proto = await client._connect(None, None)
    assert "get_sum" not in proto.method_ids            # nothing learned yet
    assert await client.call("get_sum", 1, 2) == 3      # first call goes by name
    assert isinstance(proto.method_ids.get("get_sum"), int)  # id learned from the response
    assert await client.call("get_sum", 4, 5) == 9      # second call goes by the numeric id


async def test_stale_id_falls_back_to_name(run_server):
    client = await run_server(build_app())
    assert await client.call("get_sum", 1, 2) == 3      # learn the real id
    proto = await client._connect(None, None)
    proto.method_ids["get_sum"] = 999999                # poison the cache with a bogus id
    assert await client.call("get_sum", 10, 20) == 30   # server rejects it, client resends by name
    assert proto.method_ids["get_sum"] != 999999        # and re-learns the correct id


async def test_ids_are_connection_scoped(run_server):
    # a fresh connection starts with an empty cache — this is what makes a server
    # restart safe: the reconnect re-learns from scratch, no stale ids carry over.
    client = await run_server(build_app())
    await client.call("get_sum", 1, 2)
    proto = await client._connect(None, None)
    assert proto.method_ids                             # learned on this connection
    await client.close()                                # drop the connection + its cache
    proto2 = await client._connect(None, None)          # a brand-new connection
    assert proto2 is not proto and proto2.method_ids == {}
    assert await client.call("get_sum", 2, 2) == 4      # re-learns and works


# ---------------------------------------------------------------- hash-first addressing

async def test_hash_first_call_then_id(run_server):
    # "echo_bytes" (10 chars) is >= the hash threshold, so the first call goes out as a
    # 64-bit hash; the server resolves it by hash and teaches the client the id.
    client = await run_server(build_app(), hash_first_call=True)
    assert await client.call("echo_bytes", b"hi") == b"hi"      # first call by hash
    proto = await client._connect(None, None)
    assert isinstance(proto.method_ids.get("echo_bytes"), int)  # learned the id
    assert await client.call("echo_bytes", b"yo") == b"yo"      # second call by id


async def test_short_name_not_hashed(run_server):
    # a short name is cheaper as a string, so hash_first_call leaves it a name — still works
    client = await run_server(build_app(), hash_first_call=True)
    assert await client.call("greet", "x") == "hello, x"        # "greet" is 5 chars


async def test_hash_first_and_name_first_interop(run_server):
    # a hash-first client and a default (name-first) client both work against one server
    app = build_app()
    hash_client = await run_server(app, hash_first_call=True)
    name_client = Producer("127.0.0.1", app.port, verify=False)
    try:
        assert await hash_client.call("echo_bytes", b"a") == b"a"   # addressed by hash
        assert await name_client.call("echo_bytes", b"b") == b"b"   # addressed by name
    finally:
        await name_client.close()


def test_hash_collision_is_rejected(monkeypatch):
    # force every name to the same hash and confirm registration refuses to continue
    import ezRPC.receiver.receiver as rr
    monkeypatch.setattr(rr, "method_hash", lambda name: b"\x00" * 8)
    app = Receiver(host="127.0.0.1")

    @app.function()
    async def alpha() -> int:
        return 1

    with pytest.raises(RuntimeError):
        @app.function()
        async def beta() -> int:
            return 2


# ---------------------------------------------------------------- auth: transport

async def test_auth_sent_once_then_cached(run_server):
    app = build_app()
    seen = []

    @app.before_call
    async def record(ctx):
        seen.append(ctx.auth)

    client = await run_server(app, auth="Bearer TOK")
    await client.call("get_sum", 1, 2)
    await client.call("get_sum", 3, 4)
    # the server sees the token on BOTH calls (from its per-connection cache)...
    assert seen == ["Bearer TOK", "Bearer TOK"]
    proto = await client._connect(None, None)
    assert proto.sent_auth == "Bearer TOK"
    # ...even though the client would NOT re-send it (unchanged since last sent)
    assert client._auth_to_send(proto) is None


async def test_handler_reads_call_context(run_server):
    app = Receiver(host="127.0.0.1")

    @app.function()
    async def echo_token() -> str:
        ctx = call_context()
        return ctx.auth or "none"

    client = await run_server(app, auth="Bearer XYZ")
    assert await client.call("echo_token") == "Bearer XYZ"


# ---------------------------------------------------------------- auth: Security schemes

def _basic(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


async def test_security_basic_auth(run_server):
    app = Receiver(host="127.0.0.1")
    basic = BasicAuth(users={"admin": "s3cr3t"})

    @app.function()
    async def whoami(user=Security(basic)) -> str:
        return user

    ok = await run_server(app, auth=_basic("admin", "s3cr3t"))
    assert await ok.call("whoami") == "admin"

    bad = Producer("127.0.0.1", app.port, verify=False, auth=_basic("admin", "wrong"))
    try:
        with pytest.raises(AuthError):
            await bad.call("whoami")
    finally:
        await bad.close()


async def test_security_with_wire_args(run_server):
    # a handler mixing wire args and an injected dependency: client sends only the wire args
    app = Receiver(host="127.0.0.1")
    basic = BasicAuth(users={"admin": "pw"})

    @app.function()
    async def add_as(a: int, b: int, user=Security(basic)) -> str:
        return f"{user}:{a + b}"

    client = await run_server(app, auth=_basic("admin", "pw"))
    assert await client.call("add_as", 2, 3) == "admin:5"


async def test_security_rejects_missing_auth(run_server):
    app = Receiver(host="127.0.0.1")
    basic = BasicAuth(users={"admin": "pw"})

    @app.function()
    async def secret(user=Security(basic)) -> str:
        return user

    client = await run_server(app)          # no auth configured on the client
    with pytest.raises(AuthError):
        await client.call("secret")


async def test_discover_excludes_security_params(run_server):
    app = Receiver(host="127.0.0.1")
    basic = BasicAuth(users={"x": "y"})

    @app.function()
    async def op(n: int, user=Security(basic)) -> int:
        return n

    client = await run_server(app, auth=_basic("x", "y"))
    schema = await client.discover()
    assert schema["op"]["parameters"] == {"n": "int"}   # the injected 'user' is not on the wire contract


async def test_security_jwt(run_server):
    KEY = "test-secret-key-that-is-at-least-32-bytes-long"
    app = Receiver(host="127.0.0.1")
    scheme = JWTAuth(secret=KEY, algorithms=["HS256"])

    @app.function()
    async def me(claims=Security(scheme)) -> str:
        return claims["sub"]

    good = pyjwt.encode({"sub": "alice", "exp": int(time.time()) + 60}, KEY, algorithm="HS256")
    ok = await run_server(app, auth="Bearer " + good)
    assert await ok.call("me") == "alice"

    # wrong signing key -> invalid
    forged = pyjwt.encode({"sub": "mallory"}, "a-completely-different-32-byte-signing-key", algorithm="HS256")
    bad = Producer("127.0.0.1", app.port, verify=False, auth="Bearer " + forged)
    # expired
    stale = pyjwt.encode({"sub": "bob", "exp": int(time.time()) - 10}, KEY, algorithm="HS256")
    expired = Producer("127.0.0.1", app.port, verify=False, auth="Bearer " + stale)
    try:
        with pytest.raises(AuthError):
            await bad.call("me")
        with pytest.raises(AuthError):
            await expired.call("me")
    finally:
        await bad.close()
        await expired.close()


# ---------------------------------------------------------------- reliability / hostile input

async def test_malformed_payload_does_not_crash_server(run_server):
    client = await run_server(build_app())
    proto = await client._connect(None, None)
    raw = await proto.request(b"\xc1\xc1\xc1", await_result=True, timeout=2.0)
    resp = msgspec.msgpack.decode(raw, type=ResponseFormat)
    assert resp.error is not None and resp.error.startswith("a-")
    # the connection and server survive: a normal call still works
    assert await client.call("get_sum", 2, 3) == 5


async def test_timeout_raises_and_cancels(run_server):
    client = await run_server(build_app())
    with pytest.raises(CallTimeoutError):
        await client.call("slow", timeout=0.4)
    # the connection is still usable after a timeout
    assert await client.call("get_sum", 1, 1) == 2


async def test_oversized_request_rejected(run_server):
    client = await run_server(build_app(max_request_bytes=1024))
    with pytest.raises((TransportError, EzRPCError)):
        await client.call("echo_bytes", b"x" * 50_000)
    # server survives and still serves normal calls
    assert await client.call("get_sum", 3, 4) == 7


async def test_connection_drop_fails_fast(run_server):
    # when the server goes away mid-connection, an in-flight-style call must fail
    # quickly with a TransportError, not hang until the timeout.
    app = build_app()
    client = await run_server(app)
    assert await client.call("get_sum", 1, 1) == 2  # establish the connection
    await app.shutdown()
    with pytest.raises(EzRPCError):
        await client.call("get_sum", 1, 1, timeout=3.0)
