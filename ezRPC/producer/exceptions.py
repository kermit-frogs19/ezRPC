from ezh3.client.exceptions import *


class ArgumentError(HTTPRequestError):
    pass


class ProcedureNameError(HTTPRequestError):
    pass


class ProcedureRunException(HTTPRequestError):
    pass