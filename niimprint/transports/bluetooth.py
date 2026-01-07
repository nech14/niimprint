import asyncio
import logging

from bleak import BleakClient

logger = logging.getLogger("BluetoothTransport")

CHARACTERISTIC_UUID = "bef8d6c9-9c21-4c9e-b632-bd58c1009f9f"


class BluetoothTransport:
    """BLE-транспорт для B21S."""
    def __init__(self, address: str):
        self.address = address
        self.client = BleakClient(address)
        self._connected = False

    async def connect(self):
        if not self._connected:
            await self.client.connect()
            self._connected = True
            _ = self.client.services
            logger.debug(f"Connected to {self.address} and services discovered")

    async def disconnect(self):
        if self._connected:
            await self.client.disconnect()
            self._connected = False
            logger.debug(f"Disconnected from {self.address}")

    async def write(self, data: bytes):
        await self.connect()
        mtu = 20  # BLE ограничение на один пакет
        for i in range(0, len(data), mtu):
            chunk = data[i:i+mtu]
            await self.client.write_gatt_char(CHARACTERISTIC_UUID, chunk, response=False)
            await asyncio.sleep(0.01)

    async def read(self) -> bytes:
        await self.connect()
        data = await self.client.read_gatt_char(CHARACTERISTIC_UUID)
        return data

    async def handshake(self):
        await self.write(b"\x55\x55\x21\x01\x01\x21\xaa\xaa")
        await asyncio.sleep(0.05)
        await self.write(b"\x55\x55\x23\x01\x01\x23\xaa\xaa")
        await asyncio.sleep(0.05)