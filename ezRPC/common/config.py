import msgspec
from typing import Any


DEFAULT_METHOD: str = "POST"
DEFAULT_PATH: str = "/ezrpc"
DISCOVER_SYSTEM_PROCEDURE_NAME: str = ":d"
PING_SYSTEM_PROCEDURE_NAME: str = ":p"


class CallType(int):
    pass


STANDARD_CALL = CallType(0)
NOT_AWAITED_RUN_CALL = CallType(1)
FIRE_AND_FORGET_CALL = CallType(2)


class StandardCallFormat(msgspec.Struct, array_like=True):
    function_name: str
    call_type: int
    args: tuple


class StandardResponseFormat(msgspec.Struct, array_like=True):
    error: str | None
    data: Any