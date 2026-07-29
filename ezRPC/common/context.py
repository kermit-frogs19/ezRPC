"""Per-call context.

Passed to the ``before_call`` hook and to ``Security`` schemes, and reachable
from inside a handler via ``call_context()`` (backed by a ContextVar, so it is
async-safe and isolated per call — concurrent calls never see each other's).
"""

import contextvars


class CallContext:
    __slots__ = ("method", "auth", "connection_state", "call_type")

    def __init__(self, method, auth=None, connection_state=None, call_type=0):
        self.method = method
        self.auth = auth                       # the connection's current auth token (str) or None
        # per-connection scratch dict — schemes/hooks may memoize a validated
        # principal here so the expensive check runs once, not per call
        self.connection_state = connection_state if connection_state is not None else {}
        self.call_type = call_type


_current: contextvars.ContextVar = contextvars.ContextVar("ezrpc_call_context")


def call_context() -> "CallContext | None":
    """The current call's context inside a handler, or None if called outside a call."""
    return _current.get(None)
