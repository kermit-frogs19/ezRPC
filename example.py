from ezRPC.receiver.receiver import Receiver
from ezRPC.producer.producer import Producer
import asyncio


app = Receiver()


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


async def sender():
    client = Producer("https://10.0.0.33:8000")
    result = client.call("get_sum", 1, 2)
    print(result)



if __name__ == "__main__":
    asyncio.run(app.run(port=8000, enable_ipv6=True))