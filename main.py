# This is a sample Python script.
import asyncio

# Press Shift+F10 to execute it or replace it with your code.
# Press Double Shift to search everywhere for classes, files, tool windows, actions, and settings.
from ezh3 import Client
from ezRPC import Producer


def print_hi(name):
    # Use a breakpoint in the code line below to debug your script.
    print(f'Hi, {name}')  # Press Ctrl+F8 to toggle the breakpoint.


async def sender():
    client = Producer("https://[fe80::bb3b:fde9:23a5:1a5f%8]:8000", use_tls=False, timeout=None)   # "https://vadim-seliukov-quic-server.com"

    try:
        sum = await client.call("get_sum", 125, 97)
        print(sum)

        subtraction = await client.call("get_subtraction", 125, 97)
        print(subtraction)

        subtraction = await client.rpc.get_subtraction(125, 97)
        print(subtraction)


        difference = await client.call("get_difference", 125, 97)
        print(difference)
    except BaseException as e:
        print(e)
    finally:
        await client.close()
    print("done")


# Press the green button in the gutter to run the script.
if __name__ == '__main__':
    asyncio.run(sender())

# See PyCharm help at https://www.jetbrains.com/help/pycharm/
