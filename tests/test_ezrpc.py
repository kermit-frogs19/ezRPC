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
    EzRPCError, TransportError, CallTimeoutError, CallError,
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
    # the QUIC reset code is translated into a typed, explanatory error
    with pytest.raises(CallError) as exc:
        await client.call("echo_bytes", b"x" * 50_000)
    assert "max request size" in str(exc.value)
    # server survives and still serves normal calls
    assert await client.call("get_sum", 3, 4) == 7


async def test_connection_drop_fails_fast(run_server):
    # when the server goes away mid-connection, an in-flight-style call must fail
    # quickly with a TransportError, not hang until the timeout.
    app = build_app()
    client = await run_server(app, timeout=2.0)
    assert await client.call("get_sum", 1, 1) == 2  # establish the connection
    await app.shutdown()
    with pytest.raises(EzRPCError):
        await client.call("get_sum", 1, 1, timeout=3.0)


# ---------------------------------------------------------------- cancellation & shutdown

async def test_client_cancel_cleans_up_and_stops_handler(run_server):
    # cancelling the calling task must (a) not leak the response waiter and
    # (b) take the same STOP_SENDING path as a timeout, cancelling the handler.
    app = Receiver(host="127.0.0.1")
    state = {"cancelled": False, "finished": False}

    @app.function()
    async def slow_op() -> int:
        try:
            await asyncio.sleep(3)
            state["finished"] = True
            return 1
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    client = await run_server(app)
    await client.ping()
    proto = await client._connect(None, None)
    task = asyncio.create_task(client.call("slow_op", timeout=30))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proto._waiters == {}                     # no leaked waiter
    for _ in range(20):                             # give STOP_SENDING time to arrive
        if state["cancelled"]:
            break
        await asyncio.sleep(0.05)
    assert state["cancelled"] is True
    assert state["finished"] is False


async def test_graceful_shutdown_drains_in_flight(run_server):
    app = Receiver(host="127.0.0.1")

    @app.function()
    async def slowish() -> int:
        await asyncio.sleep(0.5)
        return 42

    client = await run_server(app)
    await client.ping()
    task = asyncio.create_task(client.call("slowish", timeout=10))
    await asyncio.sleep(0.1)
    await app.shutdown(grace=5.0)      # must wait for the in-flight call to flush
    assert await task == 42


async def test_shutdown_grace_configured_on_receiver(run_server):
    # the constructor default applies when shutdown() is called with no args
    # (e.g. by run() on interrupt); grace=0 means abrupt: the in-flight call dies
    app = Receiver(host="127.0.0.1", shutdown_grace=0)

    @app.function()
    async def slowish() -> int:
        await asyncio.sleep(0.5)
        return 42

    client = await run_server(app)
    await client.ping()
    task = asyncio.create_task(client.call("slowish", timeout=10))
    await asyncio.sleep(0.1)
    await app.shutdown()               # no arg -> uses the configured grace of 0
    with pytest.raises(EzRPCError):
        await task


# ---------------------------------------------------------------- rich wire types

class Point(msgspec.Struct):
    x: int
    y: int


async def test_generic_annotations(run_server):
    app = Receiver(host="127.0.0.1")

    @app.function()
    async def total(nums: list[int]) -> int:
        return sum(nums)

    @app.function()
    async def counts(d: dict[str, int]) -> int:
        return sum(d.values())

    @app.function()
    async def is_missing(x: int | None) -> bool:
        return x is None

    client = await run_server(app)
    assert await client.call("total", [1, 2, 3]) == 6
    assert await client.call("counts", {"a": 1, "b": 2}) == 3
    assert await client.call("is_missing", None) is True
    assert await client.call("is_missing", 5) is False
    with pytest.raises(ArgumentError):              # element types are validated too
        await client.call("total", [1, "nope"])


async def test_struct_roundtrip(run_server):
    app = Receiver(host="127.0.0.1")

    @app.function()
    async def move(p: Point) -> Point:
        return Point(p.x + 1, p.y + 1)

    client = await run_server(app)
    # the client sends a plain map; the server gets a validated Point and
    # returns one, which arrives back as a map
    assert await client.call("move", {"x": 1, "y": 2}) == {"x": 2, "y": 3}


def test_unsupported_annotation_rejected_at_registration():
    class NotWireable:
        pass

    app = Receiver(host="127.0.0.1")
    with pytest.raises(TypeError):
        @app.function()
        async def f(x: NotWireable) -> int:
            return 1

    with pytest.raises(TypeError):                  # nested inside a generic too
        @app.function()
        async def g(xs: list[NotWireable]) -> int:
            return 0


# ---------------------------------------------------------------- safe replay (idempotency)

def build_idem_app(**kwargs) -> Receiver:
    app = Receiver(host="127.0.0.1", **kwargs)
    app.counter = 0

    @app.function()
    async def bump(x: int) -> int:
        app.counter += 1
        return x

    @app.function()
    async def bump_slow() -> int:               # counter moves only on *completion*
        await asyncio.sleep(0.5)
        app.counter += 1
        return app.counter

    @app.function()
    async def bump_boom() -> int:
        app.counter += 1
        raise ValueError("side effect then crash")

    return app


async def test_replay_completed_result(run_server):
    app = build_idem_app()
    client = await run_server(app)
    assert await client.call("bump", 7, idempotency_key=b"K1") == 7
    assert await client.call("bump", 7, idempotency_key=b"K1") == 7   # replayed
    assert app.counter == 1                                           # executed once
    assert await client.call("bump", 7, idempotency_key=b"K2") == 7   # new key -> executes
    assert app.counter == 2


async def test_concurrent_duplicates_execute_once(run_server):
    app = build_idem_app()
    client = await run_server(app)
    r1, r2 = await asyncio.gather(
        client.call("bump_slow", idempotency_key=b"C"),
        client.call("bump_slow", idempotency_key=b"C"),
    )
    assert r1 == r2 == 1        # the duplicate joined the in-flight execution
    assert app.counter == 1


async def test_key_reuse_with_different_args_rejected(run_server):
    app = build_idem_app()
    client = await run_server(app)
    assert await client.call("bump", 1, idempotency_key=b"K") == 1
    with pytest.raises(ArgumentError):
        await client.call("bump", 2, idempotency_key=b"K")
    assert app.counter == 1


async def test_error_outcome_replayed_not_rerun(run_server):
    # a failed attempt may have committed a partial side effect — the recorded
    # error is replayed rather than re-executing
    app = build_idem_app(debug=False)
    client = await run_server(app)
    with pytest.raises(ProcedureRunError) as e1:
        await client.call("bump_boom", idempotency_key=b"E")
    with pytest.raises(ProcedureRunError) as e2:
        await client.call("bump_boom", idempotency_key=b"E")
    assert str(e1.value) == str(e2.value)       # identical ref id -> cached, not re-run
    assert app.counter == 1


async def test_disconnect_mid_call_then_retry_executes_once(run_server):
    # the flagship: connection dies mid-execution; the retry on a fresh
    # connection returns the original outcome, and the handler ran exactly once
    app = build_idem_app()
    client = await run_server(app)
    await client.ping()
    proto = await client._connect(None, None)

    task = asyncio.create_task(client.call("bump_slow", idempotency_key=b"D", timeout=10))
    await asyncio.sleep(0.15)
    proto.close()                               # abrupt connection loss mid-call
    with pytest.raises(TransportError):
        await task

    # retry with the same key reconnects and collects the original execution
    assert await client.call("bump_slow", idempotency_key=b"D", timeout=10) == 1
    assert app.counter == 1


async def test_keyed_call_survives_client_cancel(run_server):
    # unlike an unkeyed call, a keyed execution is NOT cancelled with its stream:
    # the key declares "the outcome matters more than my presence"
    app = build_idem_app()
    client = await run_server(app)
    await client.ping()
    task = asyncio.create_task(client.call("bump_slow", idempotency_key=b"S", timeout=30))
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.6)
    assert app.counter == 1                     # ran to completion despite the cancel
    assert await client.call("bump_slow", idempotency_key=b"S") == 1   # replay
    assert app.counter == 1


async def test_timeout_then_same_key_collects_result(run_server):
    app = build_idem_app()
    client = await run_server(app)
    with pytest.raises(CallTimeoutError):
        await client.call("bump_slow", idempotency_key=b"T", timeout=0.2)
    # the execution kept running server-side; the retry gets its result
    assert await client.call("bump_slow", idempotency_key=b"T", timeout=5) == 1
    assert app.counter == 1


async def test_auto_retry_on_transport_error(run_server):
    app = build_idem_app()
    client = await run_server(app)
    real = client._call_once
    seen = {"attempts": 0, "keys": []}

    async def flaky(name, args, host, port, timeout, call_type, safe, idem_key):
        seen["attempts"] += 1
        seen["keys"].append(idem_key)
        if seen["attempts"] == 1:
            raise TransportError("simulated drop")
        return await real(name, args, host, port, timeout, call_type, safe, idem_key)

    client._call_once = flaky
    assert await client.call("bump", 5, retries=2) == 5
    assert seen["attempts"] == 2
    assert seen["keys"][0] is not None and seen["keys"][0] == seen["keys"][1]  # same auto-key


async def test_server_error_not_retried(run_server):
    # a CallError is an authoritative server outcome — retrying it is wrong
    app = build_idem_app(debug=False)
    client = await run_server(app)
    with pytest.raises(ProcedureRunError):
        await client.call("bump_boom", retries=3)
    assert app.counter == 1


async def test_idempotency_window_ttl(run_server):
    app = build_idem_app(idempotency_ttl=0.2)
    client = await run_server(app)
    assert await client.call("bump", 1, idempotency_key=b"X") == 1
    await asyncio.sleep(0.35)                   # entry expires
    assert await client.call("bump", 1, idempotency_key=b"X") == 1
    assert app.counter == 2                     # window forgot the key -> re-executed


# ---------------------------------------------------------------- session resumption

async def test_reconnect_resumes_tls_session(run_server):
    app = build_idem_app()
    client = await run_server(app)
    await client.call("bump", 1)
    proto1 = await client._connect(None, None)
    assert proto1._quic.tls.session_resumed is False     # first connection: full handshake
    for _ in range(20):                                  # ticket arrives just after handshake
        if client._session_tickets:
            break
        await asyncio.sleep(0.05)
    assert client._session_tickets                       # client stored the server's ticket

    proto1.close()                                       # connection dies
    await asyncio.sleep(0.1)
    assert await client.call("bump", 2) == 2             # transparent reconnect
    proto2 = await client._connect(None, None)
    assert proto2 is not proto1
    assert proto2._quic.tls.session_resumed is True      # PSK resumption, no cert re-validation


async def test_resumption_composes_with_safe_replay(run_server):
    # the mobile story end-to-end: drop mid-call, resume the session, replay the key
    app = build_idem_app()
    client = await run_server(app)
    await client.ping()
    for _ in range(20):
        if client._session_tickets:
            break
        await asyncio.sleep(0.05)

    proto = await client._connect(None, None)
    task = asyncio.create_task(client.call("bump_slow", idempotency_key=b"R", timeout=10))
    await asyncio.sleep(0.15)
    proto.close()                                        # dies mid-execution
    with pytest.raises(TransportError):
        await task

    assert await client.call("bump_slow", idempotency_key=b"R", timeout=10) == 1
    assert app.counter == 1                              # executed exactly once
    proto2 = await client._connect(None, None)
    assert proto2._quic.tls.session_resumed is True      # and the retry rode a resumed session


def test_session_ticket_store_is_bounded():
    from ezRPC.receiver.receiver import _SessionTicketStore
    from aioquic.tls import SessionTicket

    def make(n: int) -> SessionTicket:
        return SessionTicket(
            age_add=0, cipher_suite=None, not_valid_after=None, not_valid_before=None,
            resumption_secret=b"", server_name="s", ticket=n.to_bytes(4, "big"),
        )

    store = _SessionTicketStore(max_entries=3)
    for i in range(5):
        store.add(make(i))
    assert store.pop((0).to_bytes(4, "big")) is None     # oldest two evicted
    assert store.pop((1).to_bytes(4, "big")) is None
    assert store.pop((4).to_bytes(4, "big")) is not None # newest kept
    assert store.pop((4).to_bytes(4, "big")) is None     # and tickets are single-use


# ---------------------------------------------------------------- transport knobs & caps

async def test_keepalive_keeps_idle_connection_alive(run_server):
    app = Receiver(host="127.0.0.1", idle_timeout=1.0)

    @app.function()
    async def n() -> int:
        return 1

    client = await run_server(app, idle_timeout=1.0, keepalive=0.3)
    await client.call("n")
    proto = await client._connect(None, None)
    await asyncio.sleep(2.0)                    # well past the 1s idle timeout
    assert proto.alive                          # keepalive pings kept it open
    assert await client.call("n") == 1
    assert (await client._connect(None, None)) is proto   # same connection, no re-handshake


async def test_idle_connection_without_keepalive_reconnects(run_server):
    app = Receiver(host="127.0.0.1", idle_timeout=0.5)

    @app.function()
    async def n() -> int:
        return 1

    client = await run_server(app, idle_timeout=0.5)      # no keepalive
    await client.call("n")
    proto = await client._connect(None, None)
    await asyncio.sleep(1.2)                    # past the idle timeout
    assert not proto.alive                      # connection died silently
    assert await client.call("n") == 1          # but the pool transparently reconnects
    assert (await client._connect(None, None)) is not proto


async def test_background_call_capacity(run_server):
    app = Receiver(host="127.0.0.1", max_background_calls=2)
    app.counter = 0

    @app.function()
    async def slow_bg() -> None:
        await asyncio.sleep(1.0)

    client = await run_server(app)
    assert await client.call("slow_bg", call_type=NOT_AWAITED_RUN_CALL) is None
    assert await client.call("slow_bg", call_type=NOT_AWAITED_RUN_CALL) is None
    with pytest.raises(ProcedureRunError) as exc:         # third spawn is refused
        await client.call("slow_bg", call_type=NOT_AWAITED_RUN_CALL)
    assert "capacity" in str(exc.value)


async def test_oversized_outcome_dedups_but_does_not_replay(run_server):
    # the execution stays at-most-once, but a huge recorded outcome is dropped
    app = build_idem_app(idempotency_max_response_bytes=128)

    @app.function()
    async def big() -> bytes:
        app.counter += 1
        return b"x" * 1024

    client = await run_server(app)
    assert await client.call("big", idempotency_key=b"B") == b"x" * 1024   # original: full result
    with pytest.raises(ProcedureRunError) as exc:                          # replay: dropped outcome
        await client.call("big", idempotency_key=b"B")
    assert "executed exactly once" in str(exc.value)
    assert app.counter == 1                                                # never re-executed


async def test_idem_window_byte_budget_evicts_oldest(run_server):
    # total byte budget: oldest completed entries evict when it overflows
    app = build_idem_app(idempotency_max_bytes=300)

    @app.function()
    async def blob(tag: str) -> bytes:
        app.counter += 1
        return b"y" * 150

    client = await run_server(app)
    await client.call("blob", "a", idempotency_key=b"K-a")
    await client.call("blob", "b", idempotency_key=b"K-b")
    await client.call("blob", "c", idempotency_key=b"K-c")   # pushes over 300B -> evicts K-a
    assert app.counter == 3
    await client.call("blob", "a", idempotency_key=b"K-a")   # forgotten -> re-executes
    assert app.counter == 4


async def test_buggy_scheme_is_generic_server_error(run_server):
    # a Security scheme that crashes (not an AuthError) must come back as a
    # generic, logged server error — never leak, never masquerade as auth
    app = Receiver(host="127.0.0.1", debug=False)

    def bad_scheme(ctx):
        raise ValueError("db-password-hunter2")

    @app.function()
    async def guarded(user=Security(bad_scheme)) -> str:
        return "never"

    client = await run_server(app)
    with pytest.raises(ProcedureRunError) as exc:
        await client.call("guarded")
    assert "hunter2" not in str(exc.value)
    assert "internal error" in str(exc.value)
    assert "ref " in str(exc.value)                 # proves it went through the logged path
