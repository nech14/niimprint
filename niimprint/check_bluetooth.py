import asyncio
from bleak import BleakScanner, BleakClient


async def list_ble_devices():
    devices = await BleakScanner.discover()
    for d in devices:
        print(d.address, d.name)



async def characteristics_uuid_device(printer_addr: str):
    devices = await BleakScanner.discover()
    async with BleakClient(printer_addr) as client:
        for service in client.services:
            print(service.uuid)
            for char in service.characteristics:
                print("  ", char.uuid, char.properties)





if __name__ == "__main__":
    printer_addr = "C3:08:13:07:15:85"
    asyncio.run(list_ble_devices())
    asyncio.run(characteristics_uuid_device(printer_addr))

