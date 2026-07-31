"""Runnable end-to-end example: starts a server, calls it, prints the results.

    python example.py
"""

import asyncio
import base64

from ezRPC import *


app = Receiver()

basic_auth = BasicAuth(users={"admin": "hunter2"})


@app.function(description="Get the sum of two integers")
async def get_sum(a: int, b: int) -> int:
    return a + b


@app.function()
async def greet(name: str) -> str:
    return f"hello, {name}"


@app.function()
async def period(base_value: int | float, period_value: int, user=Security(basic_auth)) -> int:
    return base_value ** period_value


async def main() -> None:
    # Start the server on an ephemeral port (port=0), then read the bound port back.
    await app.start(host="127.0.0.1", port=0)

    # verify=False trusts the auto-generated self-signed dev certificate.
    async with Producer("127.0.0.1", app.port, verify=False) as client:
        print("get_sum(1, 2)      ->", await client.call("get_sum", 1, 2))
        print("rpc.greet('world') ->", await client.rpc.greet("world"))
        print("discover()         ->", await client.discover())
        print("ping()             ->", await client.ping())

        # no token: the Security dependency rejects the call with AuthError
        try:
            await client.rpc.period(2, 8)
        except AuthError as e:
            print("period() no auth   -> AuthError:", e)

    # authenticated client: token format is "Basic <base64(user:pass)>"
    token = "Basic " + base64.b64encode(b"admin:hunter2").decode()
    print(token)
    async with Producer("127.0.0.1", app.port, verify=False, auth=token) as client:
        print("period() with auth ->", await client.rpc.period(2, 8))

    await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
