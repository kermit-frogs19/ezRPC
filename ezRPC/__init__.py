import logging as _logging

from ezRPC.receiver.receiver import Receiver
from ezRPC.producer.producer import Producer
from ezRPC.security import Security, BasicAuth, BearerAuth, JWTAuth
from ezRPC.common.context import CallContext, call_context
from ezRPC.common.config import (
    CallType, STANDARD_CALL, NOT_AWAITED_RUN_CALL, FIRE_AND_FORGET_CALL,
    DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TIMEOUT,
)
from ezRPC.common.exceptions import (
    EzRPCError, TransportError, CallTimeoutError, CallError,
    ArgumentError, ProcedureNameError, ProcedureRunError, AuthError,
)

# library convention: emit nothing unless the application configures logging
_logging.getLogger("ezrpc").addHandler(_logging.NullHandler())

__all__ = [
    "Receiver", "Producer",
    "Security", "BasicAuth", "BearerAuth", "JWTAuth",
    "CallContext", "call_context",
    "CallType", "STANDARD_CALL", "NOT_AWAITED_RUN_CALL", "FIRE_AND_FORGET_CALL",
    "DEFAULT_HOST", "DEFAULT_PORT", "DEFAULT_TIMEOUT",
    "EzRPCError", "TransportError", "CallTimeoutError", "CallError",
    "ArgumentError", "ProcedureNameError", "ProcedureRunError", "AuthError",
]
