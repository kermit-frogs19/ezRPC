"""Unified exception hierarchy.

Everything ezRPC can raise descends from ``EzRPCError``, so a single
``except EzRPCError`` catches both transport failures and application errors —
unlike v1, where connection failures escaped the framework's own exception family.

    EzRPCError
    ├── TransportError        connect / handshake / drop / stream reset
    │   └── CallTimeoutError  the call did not complete within its timeout
    └── CallError             the server received the call but could not complete it
        ├── ArgumentError     arguments did not match the function signature
        ├── ProcedureNameError no function with that name is registered
        ├── ProcedureRunError  the function raised while running
        └── AuthError          rejected by an authentication/authorization hook
"""


class EzRPCError(Exception):
    """Root of every error ezRPC can raise."""


class TransportError(EzRPCError):
    """Connection setup, handshake, drop, or stream-level failure."""


class CallTimeoutError(TransportError):
    """The call did not receive a response within its timeout."""


class CallError(EzRPCError):
    """The server received the call but could not complete it."""


class ArgumentError(CallError):
    """Arguments did not match the target function's signature."""


class ProcedureNameError(CallError):
    """No function with the requested name is registered."""


class ProcedureRunError(CallError):
    """The target function raised an exception while running."""


class AuthError(CallError):
    """The call was rejected by an authentication/authorization hook."""
