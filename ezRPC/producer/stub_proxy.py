
class StubProxy:
    def __init__(self, client):
        self._client = client

    def __getattr__(self, name: str):
        async def call_func(*args, **kwargs):
            return await self._client.call(name, *args, **kwargs)
        return call_func