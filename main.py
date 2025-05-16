from ezRPC.receiver.receiver import Receiver
from ezRPC.producer.producer import Producer
from ezh3 import Client
import asyncio
import time
import logging

try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    print("system: Using uvloop for asyncio event loop")
except ImportError:
    uvloop = None
    print("system: Failed to import/connect uvloop for asyncio event loop")


app = Receiver(
    enable_tls=False,
    custom_cert_file_loc="/app/cert.pem",
    custom_cert_key_file_loc="/app/key.pem",
    port=8080,
    enable_ipv6=True
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.DEBUG,
)


@app.function()
async def get_sum(a: int, b: int) -> int:
    print(a)
    print(b)
    result = a + b
    return result


@app.function()
async def get_subtraction(a: int, b: int) -> int:
    print(a)
    print(b)
    result = a - b
    return result


@app.function()
async def dummy():
    return


def say() -> str:
    return "Hello!"


class TestClass:
    def __init__(
            self,
            name: str,
            email: str
    ) -> None:
        self.name = name
        self.email = email

    def get_name(self) -> str:
        return self.name

    def get_email(self) -> str:
        return self.email


app.add_function(dummy)
app.add_function(say)
app.add_class_instance(TestClass("John", "john@email.com"))


async def main():
    client = Producer("https://vadim-seliukov-quic-server.com:8080", use_tls=True, timeout=None)     # vadim-seliukov-quic-server.com
    functions = await client.discover()
    print(functions)
    await client.call("dummy")
    await client.close()


if __name__ == "__main__":
    # asyncio.run(main())
    asyncio.run(app.run())


