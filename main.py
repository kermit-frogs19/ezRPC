from ezRPC import Receiver
import asyncio


app = Receiver()


@app.function(description="Get the sum of two integers")
async def get_sum(a: int, b: int) -> int:
    return a + b


if __name__ == "__main__":
    # Local dev server. A self-signed cert is generated automatically; connect a
    # client with Producer("127.0.0.1:8000", verify=False).
    asyncio.run(app.run(host="127.0.0.1", port=8000))
