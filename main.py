# This is a sample Python script.
import asyncio

# Press Shift+F10 to execute it or replace it with your code.
# Press Double Shift to search everywhere for classes, files, tool windows, actions, and settings.
from ezh3 import Client

def print_hi(name):
    # Use a breakpoint in the code line below to debug your script.
    print(f'Hi, {name}')  # Press Ctrl+F8 to toggle the breakpoint.


async def sender():
    client = Client("https://vadim-seliukov-quic-server.com", use_tls=True)   # "https://vadim-seliukov-quic-server.com"
    response = await client.post("/echo", headers={"User-Agent": "Vadim Seliukov"}, json={"message": "A message from Vadim"})
    response = await client.post("/echo", headers={"User-Agent": "Vadim Seliukov"}, json={"message": "A message from Vadim"})
    response = await client.post("/echo", headers={"User-Agent": "Vadim Seliukov"}, json={"message": "A message from Vadim"})
    response = await client.post("/echo", headers={"User-Agent": "Vadim Seliukov"}, json={"message": "A message from Vadim"})
    response = await client.post("/echo", headers={"User-Agent": "Vadim Seliukov"}, json={"message": "A message from Vadim"})
    response = await client.post("/echo", headers={"User-Agent": "Vadim Seliukov"}, json={"message": "A message from Vadim"})
    response = await client.post("/echo", headers={"User-Agent": "Vadim Seliukov"}, json={"message": "A message from Vadim"})


    print(response, response.text)
    response.raise_for_status()
    await client.close()
    print("done")



# Press the green button in the gutter to run the script.
if __name__ == '__main__':
    asyncio.run(sender())

# See PyCharm help at https://www.jetbrains.com/help/pycharm/
