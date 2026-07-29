"""Runnable end-to-end example: starts a server, calls it, prints the results.

    python example.py
"""

import asyncio

from ezRPC import *


app = Receiver()


@app.function(description="Get the sum of two integers")
async def get_sum(a: int, b: int) -> int:
    return a + b


@app.function()
async def greet(name: str) -> str:
    return f"hello, {name}"


async def main() -> None:
    # Start the server on an ephemeral port (port=0), then read the bound port back.
    await app.start(host="127.0.0.1", port=0)

    # verify=False trusts the auto-generated self-signed dev certificate.
    async with Producer("127.0.0.1", app.port, verify=False) as client:
        print("get_sum(1, 2)      ->", await client.call("get_sum", 1, 2))
        print("rpc.greet('world') ->", await client.rpc.greet("world"))
        print("discover()         ->", await client.discover())
        print("ping()             ->", await client.ping())

    await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
