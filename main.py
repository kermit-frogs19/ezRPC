from ezRPC.receiver.receiver import Receiver
import asyncio


app = Receiver()


@app.function(description="Get the sum of 2 integers")
async def get_sum(a: int, b: int) -> int:
    print(f"Got new call for function get_sum: a = {a}, b = {b}")
    result = a + b
    print(f"Call result is - {result}")
    return result


if __name__ == "__main__":
    asyncio.run(app.run(host="0.0.0.0", port=8000))


