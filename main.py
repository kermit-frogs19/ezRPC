import asyncio
import logging
import os

from ezRPC import Receiver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = Receiver()


@app.function(description="Get the sum of two integers")
async def get_sum(a: int, b: int) -> int:
    return a + b


if __name__ == "__main__":
    # Local dev server. A self-signed cert is generated automatically; connect a
    # client with Producer("127.0.0.1:8000", verify=False). The Docker image
    # overrides the bind address via EZRPC_HOST=0.0.0.0.
    asyncio.run(app.run(host=os.environ.get("EZRPC_HOST", "127.0.0.1"),
                        port=int(os.environ.get("EZRPC_PORT", "8000"))))
