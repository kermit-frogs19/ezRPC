# ezRPC

[![CI](https://github.com/kermit-frogs19/ezRPC/actions/workflows/ci.yml/badge.svg)](https://github.com/kermit-frogs19/ezRPC/actions/workflows/ci.yml)

Typed RPC over **raw QUIC** with a MessagePack wire format. Register plain Python
functions on a server, call them from a client as if they were local — with
type-validated arguments, connection-level auth, safe replay for retries, and a
clean exception taxonomy.

```python
# server
@app.function()
async def get_sum(a: int, b: int) -> int:
    return a + b

# client
await client.call("get_sum", 1, 2)   # -> 3
```

## Why raw QUIC (not HTTP)

- **One bidirectional QUIC stream per call.** The stream's FIN delimits the
  message — no length framing, no HTTP semantics, no header compression to
  deadlock on.
- **Failure is delivered, not swallowed.** A dropped connection fails in-flight
  calls immediately instead of hanging until a timeout. A client that stops
  waiting (timeout *or* cancellation) tells the server via STOP_SENDING, and the
  running handler is cancelled.
- **Always encrypted** (TLS 1.3), multiplexed, connection reuse across calls.

## Install

```
pip install git+https://github.com/kermit-frogs19/ezRPC.git
```

Python ≥ 3.11. For JWT auth support: `pip install "ezRPC[jwt] @ git+https://github.com/kermit-frogs19/ezRPC.git"`.

## Quickstart

**server.py**

```python
import asyncio
import logging

from ezRPC import Receiver

logging.basicConfig(level=logging.INFO)
app = Receiver()

@app.function(description="Get the sum of two integers")
async def get_sum(a: int, b: int) -> int:
    return a + b

@app.function()
async def greet(name: str) -> str:
    return f"hello, {name}"

if __name__ == "__main__":
    asyncio.run(app.run(host="127.0.0.1", port=8000))
```

**client.py**

```python
import asyncio

from ezRPC import Producer

async def main():
    # verify=False trusts the server's auto-generated self-signed dev certificate
    async with Producer("127.0.0.1:8000", verify=False) as client:
        print(await client.call("get_sum", 1, 2))    # -> 3
        print(await client.rpc.greet("world"))       # -> "hello, world"
        print(await client.discover())               # machine-readable schema of all methods
        print(await client.ping())                   # -> None

asyncio.run(main())
```

A single-file runnable version of this is in [`example.py`](example.py); a
minimal server in [`main.py`](main.py).

## Typed arguments

Handler signatures are the wire contract. Arguments are decoded and
type-validated by [msgspec](https://jcristharif.com/msgspec/) against the
annotations, so a bad call fails with a clear `ArgumentError` before your code
runs. Anything msgspec can decode is supported — builtins, generics, optionals,
and `msgspec.Struct` records:

```python
import msgspec

class User(msgspec.Struct):
    id: int
    name: str

@app.function()
async def rename(user: User, names: list[str], note: str | None) -> User:
    ...
```

Calls are **positional-only** on the wire; keyword arguments are rejected loudly
on the client. An annotation msgspec cannot decode (an arbitrary class) is a
`TypeError` at registration time, not a surprise at call time.

## Authentication

The client carries one opaque auth token per connection (sent once, ~0 bytes
afterwards); the server enforces it with FastAPI-style `Security()` dependencies:

```python
from ezRPC import Receiver, Producer, Security, JWTAuth

app = Receiver()
jwt_auth = JWTAuth(secret=KEY, algorithms=["HS256"])

@app.function()
async def me(user_id: int, claims=Security(jwt_auth)) -> dict:
    return {"caller": claims["sub"], "user_id": user_id}   # client sends only user_id

client = Producer("127.0.0.1:8000", verify=False, auth="Bearer <jwt>")
```

Built-in schemes: `BasicAuth` (constant-time compare), `BearerAuth` (bring your
own `verify(token)`), `JWTAuth` (PyJWT, pinned algorithms). A scheme is any
callable `scheme(ctx) -> principal` that returns a truthy principal or raises
`AuthError`. Handlers without a `Security()` parameter are public — same model
as any web framework. A global `@app.before_call` hook runs before every call
for cross-cutting concerns (audit, rate limiting), and `discovery=False` on
registration hides a method from `discover()`.

## Call types

```python
await client.call("f", x)                                    # request/response (default)
await client.call("f", x, call_type=NOT_AWAITED_RUN_CALL)    # server acks, runs in background
await client.call("f", x, call_type=FIRE_AND_FORGET_CALL)    # no response at all
```

## Safe replay (idempotency)

The question every RPC system must answer: the connection dies mid-call — *did
the server execute it?* With an idempotency key you don't have to know:

```python
# retries auto-generate one key for all attempts: reconnect + safe replay,
# never a double-execution
await client.call("charge_card", user_id, cents, retries=3)

# or control the key yourself (e.g. derive it from your domain)
await client.call("charge_card", user_id, cents, idempotency_key=b"order-4711")
```

The semantics, precisely:

- A keyed call **executes to completion at most once per server process** within
  the window (`Receiver(idempotency_ttl=600, idempotency_max_entries=10_000)`).
- Duplicates — retries, reconnects, concurrent sends — join the in-flight
  execution or replay the recorded outcome. **Errors are outcomes too**: a failed
  attempt may have committed a partial side effect, so the recorded error is
  replayed rather than re-executing.
- A keyed execution is **decoupled from its connection**: a client disconnect,
  cancel, or timeout never kills it mid-side-effect. The retry collects the
  result — even from a brand-new connection.
- Reusing a key with a different method or arguments is rejected
  (`ArgumentError`) instead of silently replaying the wrong outcome.
- The client never blind-retries: only transport failures are retried — a server
  outcome (`CallError`) and an exhausted time budget (`CallTimeoutError`) never
  are.
- The window lives in memory: a server restart clears it, so the guarantee is
  **per-process**. For exactly-once across restarts, add a durable key check
  inside the handler.

## Errors

Everything ezRPC raises descends from `EzRPCError`:

```
EzRPCError
├── TransportError        connect / handshake / drop / stream reset
│   └── CallTimeoutError  the call did not complete within its timeout
└── CallError             the server received the call but could not complete it
    ├── ArgumentError     arguments did not match the function signature
    ├── ProcedureNameError no function with that name is registered
    ├── ProcedureRunError  the function raised while running
    └── AuthError         rejected by an authentication scheme or hook
```

Handler exceptions are logged in full on the server and returned to the caller
as a generic message with a reference id — internal detail never crosses the
wire. Set `Receiver(debug=True)` to get verbose errors during development
(automatically on when bound to localhost).

## TLS / certificates

QUIC is always encrypted, so the server always has a certificate:

- **Development** — pass nothing: the server generates a self-signed cert for
  `localhost`; connect with `Producer(..., verify=False)`.
- **Pinned** — `Producer(..., verify="path/to/ca-or-cert.pem")` trusts a
  specific certificate or private CA.
- **Production** — `Receiver(cert_file=..., key_file=...)` with a real
  certificate, and `Producer(..., verify=True)` to validate against the public
  CA bundle.

**Session resumption is automatic.** The server hands each connection a TLS
session ticket (single-use, bounded in-memory store); the client presents it on
reconnect for a PSK handshake that skips certificate re-validation — measured
~2.6× faster reconnects. A consumed or expired ticket silently falls back to a
full handshake. 0-RTT early data is deliberately not used: it is replayable by
an attacker, so calls only ever ride the confirmed handshake (a future opt-in
may allow idempotency-keyed calls in early data, where the dedup window absorbs
replays).

## Operational notes

- **Request size cap:** `Receiver(max_request_bytes=...)` (default 8 MiB) —
  oversized requests are refused mid-transfer and surface on the client as a
  `CallError`.
- **Idle connections & keepalive:** both sides take `idle_timeout=` (default
  60 s). A pooled client connection that idles past it dies silently and the
  next call pays a full re-handshake — set `Producer(keepalive=15)` to ping the
  connection and keep it warm instead.
- **Background-call cap:** `Receiver(max_background_calls=1000)` bounds
  concurrently tracked background work (fire-and-forget, not-awaited, and keyed
  executions); beyond it new spawns are refused with a clear error. Replays of
  already-recorded outcomes always work, even at capacity.
- **Idempotency window memory:** outcomes larger than
  `idempotency_max_response_bytes` (1 MiB) are executed and deduped but not
  retained for replay; `idempotency_max_bytes` (64 MiB) bounds the whole window,
  evicting the oldest completed entries first.
- **Graceful shutdown:** `await app.shutdown()` waits for in-flight calls to
  finish and flushes their responses before closing connections. The drain
  budget is configured with `Receiver(shutdown_grace=5.0)` and can be overridden
  per call (`shutdown(grace=0)` for an abrupt stop).
- **Logging:** everything is emitted on the `"ezrpc"` logger. Silent by default,
  except the two lifecycle lines (server started / stopped), which are printed
  even when logging is unconfigured. Enable everything with
  `logging.basicConfig(...)` — per-call timings at `DEBUG` level. aioquic's own
  `"quic"` logger chats at `INFO`; quiet it with
  `logging.getLogger("quic").setLevel(logging.WARNING)`.

## Development

```
pip install -e ".[test]"
pytest -q
```

The test suite starts real servers on ephemeral UDP ports and drives them over
QUIC — including malformed payloads, oversized requests, connection drops,
cancellation, and auth flows.

## License

[MIT](LICENSE)
