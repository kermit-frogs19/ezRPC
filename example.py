from ezRPC.receiver.receiver import Receiver
import asyncio


app = Receiver()


@app.function()
async def get_sum(a: int, b: int) -> int:
    return a + b

if __name__ == "__main__":
    asyncio.run(app.run())