from ezRPC.receiver.receiver import Receiver
from ezRPC.producer.producer import Producer
import asyncio


app = Receiver(
    enable_tls=True,
    cert_type="CUSTOM",
    custom_cert_file_loc="/app/cert.pem",
    custom_cert_key_file_loc="/app/key.pem",
    port=8080,
    enable_ipv6=False
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


async def sender():
    client = Producer("https://vadim-seliukov-quic-server.com:8080", use_tls=True)
    result = await client.call("get_sum", 1234, 1234)
    print(result)
    await client.close()

    print("done")


if __name__ == "__main__":
    asyncio.run(app.run())


