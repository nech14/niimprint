import asyncio
import logging
import math
import struct

from PIL import Image, ImageOps


from niimprint.enums.request import RequestCodeEnum
from niimprint.packet import NiimbotPacket
from niimprint.transports import BluetoothTransport

logger = logging.getLogger("BluetoothTransport")


def _packet_to_int(x):
    return int.from_bytes(x.data, "big")


class PrinterClient:
    """Асинхронный клиент для B21S через BLE."""
    def __init__(self, transport: BluetoothTransport):
        self._transport = transport
        self._packetbuf = bytearray()

    async def print_image(self, image: Image.Image, density: int = 3):
        # await self._transport.handshake()

        await self.set_label_density(density)
        await self.set_label_type(1)

        await self.start_print()
        await self.start_page_print()
        await self.set_dimension(image.height, image.width)

        for pkt in self._encode_image(image):
            await self._send(pkt)
            await asyncio.sleep(0.01)

        await asyncio.sleep(1)
        await self.end_page_print()
        status = await self.get_print_status()
        self._log_buffer(str(status), b"")
        await asyncio.sleep(1)

        while not await self.end_print():
            await asyncio.sleep(0.1)

    def _encode_image(self, image: Image):
        img = ImageOps.invert(image.convert("L")).convert("1")
        for y in range(img.height):
            line_data = [img.getpixel((x, y)) for x in range(img.width)]
            line_data = "".join("0" if pix == 0 else "1" for pix in line_data)
            line_data = int(line_data, 2).to_bytes(math.ceil(img.width / 8), "big")
            counts = (0, 0, 0)  # It seems like you can always send zeros
            header = struct.pack(">H3BB", y, *counts, 1)
            pkt = NiimbotPacket(0x85, header + line_data)
            yield pkt

    async def _recv(self):
        transport = self._transport
        if isinstance(transport, BluetoothTransport):
            data = await self._transport.read()
        else:
            data = await asyncio.to_thread(self._transport.read, 1024)

        self._packetbuf.extend(data)
        packets = []
        while len(self._packetbuf) > 4:
            pkt_len = self._packetbuf[3] + 7
            if len(self._packetbuf) >= pkt_len:
                packet = NiimbotPacket.from_bytes(self._packetbuf[:pkt_len])
                self._log_buffer("recv", packet.to_bytes())
                packets.append(packet)
                del self._packetbuf[:pkt_len]
            else:
                break
        return packets

    async def _send(self, packet: NiimbotPacket):
        transport = self._transport
        if isinstance(transport, BluetoothTransport):
            await self._transport.write(packet.to_bytes())
        else:
            await asyncio.to_thread(self._transport.write, packet.to_bytes())


    def _log_buffer(self, prefix: str, buff: bytes):
        msg = ":".join(f"{b:02x}" for b in buff)
        logger.debug(f"{prefix}: {msg}")

    async def _transceive(self, reqcode, data, respoffset=1):
        respcode = respoffset + reqcode
        packet  = NiimbotPacket(reqcode, data)
        self._log_buffer("send", packet.to_bytes())
        await self._send(packet )

        resp = None
        for _ in range(6):
            packets = await self._recv()
            for p in packets:
                if p.type == 219:
                    raise ValueError("Printer error")
                elif packet.type == 0:
                    raise NotImplementedError
                elif p.type == respcode:
                    resp = p
            if resp:
                return resp
            await asyncio.sleep(0.1)
        return resp



    async def set_label_type(self, n):
        pkt = await self._transceive(RequestCodeEnum.SET_LABEL_TYPE, bytes([n]), 16)
        return bool(pkt.data[0]) if pkt else True  # B21S может не присылать ответ

    async def set_label_density(self, n):
        pkt = await self._transceive(RequestCodeEnum.SET_LABEL_DENSITY, bytes([n]), 16)
        return bool(pkt.data[0]) if pkt else True

    async def start_print(self):
        pkt = await self._transceive(RequestCodeEnum.START_PRINT, b"\x01")
        return bool(pkt.data[0]) if pkt else True

    async def end_print(self):
        pkt = await self._transceive(RequestCodeEnum.END_PRINT, b"\x01")
        return bool(pkt.data[0]) if pkt else True

    async def start_page_print(self):
        pkt = await self._transceive(RequestCodeEnum.START_PAGE_PRINT, b"\x01")
        return bool(pkt.data[0]) if pkt else True

    async def end_page_print(self):
        pkt = await self._transceive(RequestCodeEnum.END_PAGE_PRINT, b"\x01")
        return bool(pkt.data[0]) if pkt else True

    async def set_dimension(self, w, h):
        pkt = await self._transceive(RequestCodeEnum.SET_DIMENSION, struct.pack(">HH", w, h))
        return bool(pkt.data[0]) if pkt else True

    async def get_print_status(self):
        pkt = await self._transceive(RequestCodeEnum.GET_PRINT_STATUS, b"\x01", 16)
        if pkt is None or len(pkt.data) < 4:
            return {"page": 0, "progress1": 0, "progress2": 0}
        page, progress1, progress2 = struct.unpack(">HBB", pkt.data[:4])
        return {"page": page, "progress1": progress1, "progress2": progress2}