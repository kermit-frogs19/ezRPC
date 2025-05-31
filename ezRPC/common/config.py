import msgspec
from typing import Any
from enum import Enum, IntEnum


DEFAULT_METHOD: str = "POST"
DEFAULT_PATH: str = "/ezrpc"
DISCOVER_SYSTEM_PROCEDURE_NAME: str = ":d"
PING_SYSTEM_PROCEDURE_NAME: str = ":p"


class CallType(IntEnum):
    STANDARD_CALL = 0
    NOT_AWAITED_RUN_CALL = 1
    FIRE_AND_FORGET_CALL = 2


STANDARD_CALL = CallType.STANDARD_CALL
NOT_AWAITED_RUN_CALL = CallType.NOT_AWAITED_RUN_CALL
FIRE_AND_FORGET_CALL = CallType.FIRE_AND_FORGET_CALL


class StandardCallFormat(msgspec.Struct, array_like=True):
    function_name: str
    call_type: int
    args: tuple


class StandardResponseFormat(msgspec.Struct, array_like=True):
    error: str | None
    data: Any


SUPPORTED_TYPES: list = [int, float, str, list, tuple, dict, None]


