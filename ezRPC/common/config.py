"""Wire format, call types, and transport constants for ezRPC.

ezRPC runs directly on raw QUIC (aioquic) — no HTTP/3. One QUIC bidirectional
stream carries one call: the client writes the request bytes and closes its send
side (FIN); the server reads until FIN, runs the function, writes the response
bytes and closes its send side. The stream boundary delimits the message, so no
length framing is needed on the wire.

Request bytes  = msgpack([function_name, call_type, args])
Response bytes = msgpack([error, data])
"""

import xxhash
import msgspec
from enum import IntEnum
from typing import Any
from msgspec.msgpack import Ext


# ---- transport ----
ALPN_PROTOCOL: str = "ezrpc/1"          # negotiated at handshake; version lives here
DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8000
DEFAULT_TIMEOUT: float = 5.0

# QUIC stream ids carry their type in the low two bits; 0b00 = client-initiated
# bidirectional — the only kind that carries a call.
STREAM_TYPE_MASK: int = 0x3
CLIENT_BIDI_STREAM: int = 0x0

# ---- operational defaults (each overridable on the Receiver / Producer) ----
DEFAULT_IDLE_TIMEOUT: float = 60.0                          # QUIC idle timeout, both sides
# drain budget on shutdown: >= DEFAULT_TIMEOUT (covers what a default client
# still waits for), well under supervisor kill windows (~30s)
DEFAULT_SHUTDOWN_GRACE: float = 5.0
DEFAULT_MAX_REQUEST_BYTES: int = 8 * 1024 * 1024
DEFAULT_MAX_BACKGROUND_CALLS: int = 1000
DEFAULT_IDEMPOTENCY_TTL: float = 600.0
DEFAULT_IDEMPOTENCY_MAX_ENTRIES: int = 10_000
DEFAULT_IDEMPOTENCY_MAX_RESPONSE_BYTES: int = 1024 * 1024
DEFAULT_IDEMPOTENCY_MAX_BYTES: int = 64 * 1024 * 1024

# client retry backoff: RETRY_BACKOFF_BASE * 2**attempt, capped at RETRY_BACKOFF_CAP
RETRY_BACKOFF_BASE: float = 0.05
RETRY_BACKOFF_CAP: float = 1.0

# server-side TLS session-ticket store: bounds memory; each ticket is a few
# hundred bytes and single-use (fetched tickets are popped, an anti-replay measure)
SESSION_TICKET_STORE_SIZE: int = 1024

# ---- built-in (system) procedures ----
DISCOVER_SYSTEM_PROCEDURE_NAME: str = ":d"
PING_SYSTEM_PROCEDURE_NAME: str = ":p"

# ---- QUIC application error codes used on RESET_STREAM / STOP_SENDING ----
ERR_CLIENT_CANCEL: int = 0x100
ERR_DEADLINE_EXCEEDED: int = 0x101
ERR_REQUEST_TOO_LARGE: int = 0x104

# ---- error-string prefixes: map a wire error to a client-side exception type ----
ERR_PREFIX_ARGUMENT: str = "a-"
ERR_PREFIX_NAME: str = "n-"
ERR_PREFIX_RUN: str = "r-"
ERR_PREFIX_AUTH: str = "u-"
ERR_PREFIX_UNKNOWN_ID: str = "i-"   # server didn't recognize a method id; client resends by name


class CallType(IntEnum):
    STANDARD_CALL = 0          # request -> response
    NOT_AWAITED_RUN_CALL = 1   # server runs it in the background, acks immediately
    FIRE_AND_FORGET_CALL = 2   # server runs it in the background, sends no response


STANDARD_CALL = CallType.STANDARD_CALL
NOT_AWAITED_RUN_CALL = CallType.NOT_AWAITED_RUN_CALL
FIRE_AND_FORGET_CALL = CallType.FIRE_AND_FORGET_CALL


class CallEnvelope(msgspec.Struct, array_like=True):
    """Server-side routing envelope. Decoded once; ``args`` stays deferred as raw
    bytes so it can be validated against the target function's typed signature.

    ``method`` addresses the function three ways: the name (str), a small
    server-assigned integer id (int) once the client has learned it, or a 64-bit
    name hash (Ext, code HASH_EXT_CODE) for a compact first call — see the lazy
    method-id scheme in the Receiver/Producer."""
    method: int | str | Ext
    call_type: int
    args: msgspec.Raw
    # opaque auth token, sent only when it changes on a connection (0 bytes otherwise)
    auth: str | None = None
    # idempotency key (canonically bytes on the wire; clients encode str keys as
    # UTF-8): same key -> the call executes to completion at most once per server
    # process within the dedup window; duplicates replay the outcome.
    # (array_like encoding is positional, so this field name never hits the wire)
    idempotency_key: bytes | None = None


class ResponseFormat(msgspec.Struct, array_like=True):
    error: str | None
    data: Any
    # sent only when the call was addressed by name: the id to use next time
    method_id: int | None = None


# ---- method-name hashing (compact first-call addressing) ----
HASH_EXT_CODE: int = 1          # MessagePack ext type code meaning "64-bit method-name hash"
HASH_NAME_THRESHOLD: int = 10   # hash a first call only when the name is at least this many bytes


def method_hash(name: str) -> bytes:
    """Stable 64-bit (8-byte, big-endian) method-name hash via XXH3-64. XXH3 is
    natively 64-bit with canonical libraries in every language, so a non-Python SDK
    can compute the identical value for cross-language method routing."""
    return xxhash.xxh3_64_digest(name.encode("utf-8"))
