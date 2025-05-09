from ezRPC.receiver.receiver import Receiver
from ezRPC.producer.producer import Producer
from ezh3 import Client
import asyncio
import time

try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    print("system: Using uvloop for asyncio event loop")
except ImportError:
    uvloop = None
    print("system: Failed to import/connect uvloop for asyncio event loop")


app = Receiver(
    enable_tls=True,
    custom_cert_file_loc="/app/cert.pem",
    custom_cert_key_file_loc="/app/key.pem",
    port=8080,
    enable_ipv6=True
)


@app.function(description="Get a sum of 2 integers")
async def get_sum(a: int, b: int) -> int:
    print(a)
    print(b)
    result = a + b
    return result


@app.function(description="Get a subtraction of 2 integers")
async def get_subtraction(a: int, b: int) -> int:
    print(a)
    print(b)
    result = a - b
    return result


@app.function()
async def dummy():
    return


async def sender():
    client = Producer("https://example:8080", use_tls=True)
    result = await client.rpc.dummy()
    result = await client.rpc.dummy()
    result = await client.rpc.dummy()

    print("first request made, doing the rest...")
    start_time = time.time()
    for i in range(100):
        await client.rpc.dummy()
    end_time = time.time()
    elapsed_time = end_time - start_time
    print(f"Request batch completed. Elapsed time: {elapsed_time:.2f} seconds")
    await client.close()


if __name__ == "__main__":
    asyncio.run(sender())
    # asyncio.run(app.run())


