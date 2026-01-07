# import asyncio
# from bleak import BleakScanner
#
# async def main():
#     devices = await BleakScanner.discover()
#     for d in devices:
#         print(d.address, d.name)
#
# asyncio.run(main())
#
#

from bleak import BleakClient, BleakScanner

async def main():
    devices = await BleakScanner.discover()
    printer_addr = "C3:08:13:07:15:85"
    async with BleakClient(printer_addr) as client:
        for service in client.services:
            print(service.uuid)
            for char in service.characteristics:
                print("  ", char.uuid, char.properties)

import asyncio
asyncio.run(main())
