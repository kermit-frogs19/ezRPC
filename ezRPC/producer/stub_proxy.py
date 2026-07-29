"""Attribute-style call sugar: ``client.rpc.get_sum(1, 2)`` -> ``client.call("get_sum", 1, 2)``.

Keyword arguments are rejected loudly. The wire format is positional-only, so a
kwarg cannot be delivered; v1 either raised a confusing error or silently
swallowed a kwarg named like a transport option (e.g. ``url=``) and misrouted the
call. Failing fast with a clear message prevents both.
"""


class StubProxy:
    def __init__(self, client) -> None:
        self._client = client

    def __getattr__(self, name: str):
        async def call_func(*args, **kwargs):
            if kwargs:
                raise TypeError(
                    f"ezRPC calls are positional-only; got keyword argument(s) "
                    f"{list(kwargs)} for '{name}'. Pass the arguments positionally."
                )
            return await self._client.call(name, *args)
        return call_func
