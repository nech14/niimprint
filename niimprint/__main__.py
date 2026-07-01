import asyncio
import logging
import re

import click
from PIL import Image

from niimprint import BluetoothClassicTransport, BluetoothTransport, PrinterClient, SerialTransport


@click.group()
def cli():
    pass


@cli.command("print")
@click.option(
    "-m",
    "--model",
    type=click.Choice(["b1", "b18", "b21", "d11", "d110"], False),
    default="b21",
    show_default=True,
    help="Niimbot printer model",
)
@click.option(
    "-c",
    "--conn",
    type=click.Choice(["usb", "bluetooth", "bt", "ble"]),
    default="usb",
    show_default=True,
    help="Connection type: usb, bluetooth/bt = classic RFCOMM/SPP, ble = BLE/GATT",
)
@click.option(
    "-a",
    "--addr",
    help="Bluetooth MAC address, Bluetooth serial port such as COM7, or serial device path",
)
@click.option(
    "--bt-channel",
    type=click.IntRange(1, 30),
    default=1,
    show_default=True,
    help="RFCOMM channel for classic Bluetooth/SPP",
)
@click.option(
    "--bt-write-delay",
    type=float,
    default=0.003,
    show_default=True,
    help="Delay after every classic Bluetooth/SPP write, seconds. Increase if labels are blank.",
)
@click.option(
    "--bt-chunk-size",
    type=click.IntRange(0, 512),
    default=0,
    show_default=True,
    help="Chunk size for classic Bluetooth/SPP writes. 0 disables chunking.",
)
@click.option(
    "--bt-line-delay",
    type=float,
    default=0.003,
    show_default=True,
    help="Delay after every raster line over classic Bluetooth/SPP, seconds.",
)
@click.option(
    "--bitmap-counts",
    type=click.Choice(["zero", "split", "total"]),
    default=None,
    help="Black-pixel counter mode for bitmap rows. Defaults to split for classic Bluetooth, zero for USB/BLE.",
)
@click.option(
    "--bitmap-mode",
    type=click.IntRange(0, 255),
    default=None,
    help="Last byte in 0x85 bitmap-row header. Defaults to 1.",
)
@click.option(
    "--bitmap-compress/--no-bitmap-compress",
    default=None,
    help="Skip blank rows and merge repeated rows. Defaults on for classic Bluetooth.",
)
@click.option(
    "--bitmap-batch-size",
    type=click.IntRange(1, 20),
    default=None,
    help="Raster packets per write. Defaults to 5 for classic Bluetooth, 1 for USB/BLE.",
)
@click.option(
    "--bt-send-connect/--bt-no-send-connect",
    default=True,
    show_default=True,
    help="Send the special 0x03 + Connect packet before printing over classic Bluetooth.",
)
@click.option(
    "--bt-prefix-packets",
    is_flag=True,
    help="Prefix every regular Niimbot packet with 0x03 over classic Bluetooth.",
)
@click.option(
    "-d",
    "--density",
    type=click.IntRange(1, 5),
    default=5,
    show_default=True,
    help="Print density",
)
@click.option(
    "-r",
    "--rotate",
    type=click.Choice(["0", "90", "180", "270"]),
    default="0",
    show_default=True,
    help="Image rotation (clockwise)",
)
@click.option(
    "-i",
    "--image",
    type=click.Path(exists=True),
    required=True,
    help="Image path",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable verbose logging",
)
def print_cmd(model, conn, addr, bt_channel, bt_write_delay, bt_chunk_size, bt_line_delay, bitmap_counts, bitmap_mode, bitmap_compress, bitmap_batch_size, bt_send_connect, bt_prefix_packets, density, rotate, image, verbose):
    logging.basicConfig(
        level="DEBUG" if verbose else "INFO",
        format="%(levelname)s | %(module)s:%(funcName)s:%(lineno)d - %(message)s",
    )

    if conn == "ble":
        assert addr is not None, "--addr argument required for BLE connection"
        addr = addr.upper()
        assert re.fullmatch(r"([0-9A-F]{2}:){5}([0-9A-F]{2})", addr), "Bad BLE MAC address"

    if conn in ("bluetooth", "bt"):
        transport = BluetoothClassicTransport(
            addr or "auto",
            channel=bt_channel,
            write_delay=bt_write_delay,
            chunk_size=bt_chunk_size,
            raster_line_delay=bt_line_delay,
        )
    elif conn == "ble":
        transport = BluetoothTransport(addr)
    else:
        port = addr if addr is not None else "auto"
        transport = SerialTransport(port=port)

    if model in ("b1", "b18", "b21"):
        max_width_px = 320 #384
        max_height_px = 320 #230  # assume 3x5 stickers
    else:
        max_width_px = 96
        max_height_px = 57  # assume 3x5 stickers

    if model in ("b18", "d11", "d110") and density > 3:
        logging.warning("%s only supports density up to 3", model.upper())
        density = 3

    image_obj = Image.open(image)
    if rotate != "0":
        # PIL library rotates counter clockwise, so we need to multiply by -1
        image_obj = image_obj.rotate(-int(rotate), expand=True)
    if max_width_px < image_obj.size[0]:
        ratio = max_width_px / image_obj.size[0] * 0.98
        nwidth = int(image_obj.size[0] * ratio)
        nheight = int(image_obj.size[1] * ratio)
        image_obj.thumbnail((nwidth, nheight), Image.Resampling.LANCZOS)

    image_obj = place_on_white_background(image_obj, max_width_px, max_height_px)
    if bitmap_counts is None:
        bitmap_counts = "split" if conn in ("bluetooth", "bt") else "zero"

    if bitmap_mode is None:
        bitmap_mode = 1

    if bitmap_compress is None:
        bitmap_compress = conn in ("bluetooth", "bt")

    if bitmap_batch_size is None:
        bitmap_batch_size = 5 if conn in ("bluetooth", "bt") else 1

    packet_prefix = b"\x03" if bt_prefix_packets and conn in ("bluetooth", "bt") else b""
    printer = PrinterClient(
        transport,
        bitmap_count_mode=bitmap_counts,
        packet_prefix=packet_prefix,
        bitmap_mode=bitmap_mode,
        compress_bitmap=bitmap_compress,
        bitmap_batch_size=bitmap_batch_size,
        official_flow=conn in ("bluetooth", "bt"),
    )
    send_connect = bt_send_connect if conn in ("bluetooth", "bt") else False

    async def run_print():
        try:
            await printer.print_image(image_obj, density=density, send_connect=send_connect)
        finally:
            if isinstance(transport, BluetoothTransport):
                await transport.disconnect()
            elif hasattr(transport, "close"):
                transport.close()

    asyncio.run(run_print())


@cli.command("devices")
@click.option(
    "-c",
    "--conn",
    type=click.Choice(["ble", "bluetooth", "bt", "serial"]),
    default="ble",
    show_default=True,
    help="Device discovery/listing transport",
)
def devices_cmd(conn):
    if conn == "ble":
        from niimprint.check_bluetooth import list_ble_devices

        async def run():
            await list_ble_devices()

        asyncio.run(run())
        return

    from niimprint.transports.bluetooth_classic import BluetoothClassicTransport

    ports = BluetoothClassicTransport.list_serial_ports()
    if not ports:
        click.echo("No serial ports detected")
        return

    for port in ports:
        click.echo(f"{port.device}: {port.description} [{port.hwid}]")


def place_on_white_background(image: Image.Image, width: int, height: int) -> Image.Image:
    """Place the image in the center of a fixed white background."""
    background = Image.new("RGB", (width, height), (255, 255, 255))
    img_w, img_h = image.size
    offset = ((width - img_w) // 2, (height - img_h + 8) // 2)
    background.paste(image, offset)
    return background


if __name__ == "__main__":
    cli()
