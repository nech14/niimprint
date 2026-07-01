import asyncio
import logging
import math
import struct

from PIL import Image, ImageOps


from niimprint.enums.request import RequestCodeEnum
from niimprint.packet import NiimbotPacket
from niimprint.transports.bluetooth import BluetoothTransport

logger = logging.getLogger("PrinterClient")


def _packet_to_int(x):
    return int.from_bytes(x.data, "big")


class PrinterClient:
    """Асинхронный клиент печати поверх любого транспорта проекта."""

    def __init__(
        self,
        transport,
        bitmap_count_mode: str = "zero",
        packet_prefix: bytes = b"",
        bitmap_mode: int = 1,
        compress_bitmap: bool = False,
        bitmap_batch_size: int = 1,
        official_flow: bool = False,
    ):
        self._transport = transport
        self._packetbuf = bytearray()
        self.bitmap_count_mode = bitmap_count_mode
        self.packet_prefix = packet_prefix
        self.bitmap_mode = bitmap_mode
        self.compress_bitmap = compress_bitmap
        self.bitmap_batch_size = max(1, bitmap_batch_size)
        self.official_flow = official_flow

    async def print_image(self, image: Image.Image, density: int = 3, send_connect: bool = False):
        if send_connect:
            await self.connect_printer()

        if self.official_flow:
            await self.set_label_type(1)
            await self.set_label_density(density)
        else:
            await self.set_label_density(density)
            await self.set_label_type(1)

        await self.start_print()
        if self.official_flow:
            await self.get_print_status()
        await self.start_page_print()

        logger.debug(f"Setting dimensions: {image.height}x{image.width}")
        await self.set_dimension(image.height, image.width)

        raster_start_delay = getattr(self._transport, "raster_start_delay", 0)
        if raster_start_delay > 0:
            await asyncio.sleep(raster_start_delay)

        raster_line_delay = getattr(self._transport, "raster_line_delay", 0)
        batch = []
        for pkt in self._encode_image(image):
            if self.official_flow and pkt.type != 0x85:
                if batch:
                    await self._send_batch(batch)
                    batch = []
                    if raster_line_delay > 0:
                        await asyncio.sleep(raster_line_delay)
                await self._send(pkt)
                if raster_line_delay > 0:
                    await asyncio.sleep(raster_line_delay)
                continue

            batch.append(pkt)
            if len(batch) >= self.bitmap_batch_size:
                await self._send_batch(batch)
                batch = []
                if raster_line_delay > 0:
                    await asyncio.sleep(raster_line_delay)

        if batch:
            await self._send_batch(batch)
            if raster_line_delay > 0:
                await asyncio.sleep(raster_line_delay)

        # await asyncio.sleep(1)
        await self.end_page_print()
        status = await self.get_print_status()
        self._log_buffer(str(status), b"")
        await asyncio.sleep(1)

        while not await self.end_print():
            await asyncio.sleep(0.1)

    @staticmethod
    def _count_bits(data: bytes) -> int:
        return sum(byte.bit_count() for byte in data)

    def _bitmap_counts(self, line_data: bytes) -> tuple[int, int, int]:
        # 0x85 packet contains three bytes with black-pixel counters.
        # Some devices tolerate zero counters over USB/BLE, but reject/ignore
        # bitmap rows over classic Bluetooth SPP unless counters are real.
        mode = self.bitmap_count_mode
        if mode == "zero":
            return (0, 0, 0)

        if mode == "total":
            total = self._count_bits(line_data)
            return (0, total & 0xFF, (total >> 8) & 0xFF)

        if mode == "split":
            # Niimbot 0x85 stores black-pixel counters for up to three
            # 16-byte bands. B21 at 384 px is 16/16/16 bytes; the current
            # 320 px canvas is 16/16/8. Do not split into equal thirds.
            return (
                self._count_bits(line_data[:16]),
                self._count_bits(line_data[16:32]),
                self._count_bits(line_data[32:48]),
            )

        raise ValueError(f"Unsupported bitmap_count_mode: {mode}")

    def _iter_image_lines(self, image: Image.Image):
        img = ImageOps.invert(image.convert("L")).convert("1")
        for y in range(img.height):
            line_bits = [img.getpixel((x, y)) for x in range(img.width)]
            line_bits = "".join("0" if pix == 0 else "1" for pix in line_bits)
            yield y, int(line_bits, 2).to_bytes(math.ceil(img.width / 8), "big")

    def _encode_line_packet(self, y: int, line_data: bytes, repeat: int):
        counts = self._bitmap_counts(line_data)
        header = struct.pack(">H3BB", y, *counts, repeat)
        return NiimbotPacket(0x85, header + line_data)

    def _encode_blank_run_packets(self, y: int, count: int):
        while count > 0:
            chunk = min(count, 255)
            yield NiimbotPacket(0x84, struct.pack(">HB", y, chunk))
            y += chunk
            count -= chunk

    def _encode_image(self, image: Image.Image):
        if not self.compress_bitmap:
            for y, line_data in self._iter_image_lines(image):
                yield self._encode_line_packet(y, line_data, self.bitmap_mode)
            return

        pending_y = None
        pending_line = None
        repeat = 0
        blank_y = None
        blank_count = 0

        def flush_pending():
            nonlocal pending_y, pending_line, repeat
            if pending_line is not None:
                pkt = self._encode_line_packet(pending_y, pending_line, repeat)
                pending_y = None
                pending_line = None
                repeat = 0
                return pkt
            return None

        for y, line_data in self._iter_image_lines(image):
            if self._count_bits(line_data) == 0:
                pkt = flush_pending()
                if pkt is not None:
                    yield pkt
                if self.official_flow:
                    if blank_y is None:
                        blank_y = y
                    blank_count += 1
                continue

            if blank_count:
                for pkt in self._encode_blank_run_packets(blank_y, blank_count):
                    yield pkt
                blank_y = None
                blank_count = 0

            if pending_line == line_data and repeat < 255:
                repeat += 1
                continue

            pkt = flush_pending()
            if pkt is not None:
                yield pkt

            pending_y = y
            pending_line = line_data
            repeat = 1

        pkt = flush_pending()
        if pkt is not None:
            yield pkt

        if blank_count:
            for pkt in self._encode_blank_run_packets(blank_y, blank_count):
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
        # Раскомментируй строку ниже, если хочешь видеть спам из пакетов 0x85 в консоли
        self._log_buffer("send", packet.to_bytes())
        transport = self._transport
        data = self.packet_prefix + packet.to_bytes()
        if isinstance(transport, BluetoothTransport):
            await self._transport.write(data)
        else:
            await asyncio.to_thread(self._transport.write, data)

    async def _send_batch(self, packets: list[NiimbotPacket]):
        if len(packets) == 1:
            await self._send(packets[0])
            return

        for packet in packets:
            self._log_buffer("send", packet.to_bytes())

        data = b"".join(self.packet_prefix + packet.to_bytes() for packet in packets)
        transport = self._transport
        if isinstance(transport, BluetoothTransport):
            await self._transport.write(data)
        else:
            await asyncio.to_thread(self._transport.write, data)

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



    async def connect_printer(self):
        # Connect command is special: it is the only known packet with a 0x03
        # prefix before the regular 55 55 packet header. It is mainly useful
        # for classic Bluetooth experiments; USB/BLE usually work without it.
        packet = b"\x03" + NiimbotPacket(0xC1, b"\x01").to_bytes()
        await self._send_raw(packet)
        await asyncio.sleep(0.2)

    async def _send_raw(self, data: bytes):
        self._log_buffer("send_raw", data)
        transport = self._transport
        if isinstance(transport, BluetoothTransport):
            await self._transport.write(data)
        else:
            await asyncio.to_thread(self._transport.write, data)

    async def set_label_type(self, n):
        pkt = await self._transceive(RequestCodeEnum.SET_LABEL_TYPE, bytes([n]), 16)
        return bool(pkt.data[0]) if pkt else True  # B21S может не присылать ответ

    async def set_label_density(self, n):
        pkt = await self._transceive(RequestCodeEnum.SET_LABEL_DENSITY, bytes([n]), 16)
        return bool(pkt.data[0]) if pkt else True

    async def start_print(self):
        data = b"\x00\x01" if self.official_flow else b"\x01"
        pkt = await self._transceive(RequestCodeEnum.START_PRINT, data)
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
        data = struct.pack(">HHH", w, h, 1) if self.official_flow else struct.pack(">HH", w, h)
        pkt = await self._transceive(RequestCodeEnum.SET_DIMENSION, data)
        return bool(pkt.data[0]) if pkt else True

    async def get_print_status(self):
        pkt = await self._transceive(RequestCodeEnum.GET_PRINT_STATUS, b"\x01", 16)
        if pkt is None or len(pkt.data) < 4:
            return {"page": 0, "progress1": 0, "progress2": 0}
        page, progress1, progress2 = struct.unpack(">HBB", pkt.data[:4])
        return {"page": page, "progress1": progress1, "progress2": progress2}