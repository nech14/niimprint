import logging
import re
import socket
import time
from contextlib import suppress

import serial
from serial.tools.list_ports import comports as list_comports

from niimprint.transports.base import BaseTransport

logger = logging.getLogger("BluetoothClassicTransport")

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_SERIAL_PORT_RE = re.compile(r"^(COM\d+|/dev/.+|[A-Za-z]:?[/\\].+)$", re.IGNORECASE)


class BluetoothClassicTransport(BaseTransport):
    """Classic Bluetooth SPP/RFCOMM transport.

    There are two practical ways to use classic Bluetooth Serial Port Profile:

    * Windows/macOS/Linux mapped serial port, for example ``COM7`` or
      ``/dev/rfcomm0``. This is the most reliable mode on Windows.
    * Raw RFCOMM socket by Bluetooth MAC + channel. This only works when the
      printer exposes an SPP/RFCOMM service and the OS/Python build supports
      Bluetooth sockets.

    Both modes expose a byte stream to the printer protocol, so the rest of the
    code can use this transport exactly like USB serial.
    """

    def __init__(
        self,
        address: str = "auto",
        channel: int = 1,
        timeout: float = 2.0,
        write_delay: float = 0.003,
        chunk_size: int = 0,
        raster_line_delay: float = 0.003,
        raster_start_delay: float = 0.1,
    ):
        self.address = address
        self.channel = channel
        self.timeout = timeout
        # Classic Bluetooth SPP has a much smaller and less predictable buffer
        # than USB serial. Without pacing, some printers accept the control
        # packets but silently drop raster line packets, which results in a
        # blank label/feed-only print.
        self.write_delay = write_delay
        self.chunk_size = chunk_size
        self.raster_line_delay = raster_line_delay
        self.raster_start_delay = raster_start_delay
        self._socket: socket.socket | None = None
        self._serial: serial.Serial | None = None

    @staticmethod
    def is_mac_address(value: str) -> bool:
        return bool(_MAC_RE.fullmatch(value or ""))

    @staticmethod
    def looks_like_serial_port(value: str) -> bool:
        return bool(_SERIAL_PORT_RE.fullmatch(value or ""))

    @staticmethod
    def list_serial_ports():
        return list(list_comports())

    @classmethod
    def _detect_bluetooth_serial_port(cls) -> str:
        ports = cls.list_serial_ports()
        bluetooth_ports = []
        for port in ports:
            text = " ".join(
                str(part or "")
                for part in (port.device, port.name, port.description, port.hwid)
            ).lower()
            if any(
                marker in text
                for marker in (
                    "bluetooth",
                    "bthenum",
                    "bthmodem",
                    "rfcomm",
                    "serial over bluetooth",
                    "standard serial over bluetooth",
                )
            ):
                bluetooth_ports.append(port)

        if len(bluetooth_ports) == 1:
            return bluetooth_ports[0].device

        if len(bluetooth_ports) == 0:
            all_ports = "\n".join(
                f"- {p.device}: {p.description} [{p.hwid}]" for p in ports
            )
            hint = f"\nDetected serial ports:\n{all_ports}" if all_ports else ""
            raise RuntimeError(
                "No Bluetooth serial/SPP COM port detected. Pair the printer and "
                "create an outgoing Bluetooth COM port in OS Bluetooth settings, "
                "then pass it as '-a COMx'."
                + hint
            )

        msg = "Multiple Bluetooth serial/SPP ports detected; pass one with '-a <port>':"
        for port in bluetooth_ports:
            msg += f"\n- {port.device}: {port.description} [{port.hwid}]"
        raise RuntimeError(msg)

    def _connect_serial(self, port: str):
        self._serial = serial.Serial(
            port=port,
            baudrate=115200,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )
        logger.debug("Connected to Bluetooth serial port %s", port)

    def _connect_rfcomm_socket(self, address: str):
        if not hasattr(socket, "AF_BLUETOOTH"):
            raise RuntimeError(
                "This Python build does not expose AF_BLUETOOTH sockets. On "
                "Windows, pair the printer, create/use its outgoing Bluetooth COM "
                "port, and run '-c bluetooth -a COMx'."
            )

        try:
            proto = getattr(socket, "BTPROTO_RFCOMM", 3)
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, proto)
            sock.settimeout(self.timeout)
            sock.connect((address, self.channel))
        except OSError as exc:
            raise RuntimeError(
                f"Cannot connect to {address} via RFCOMM channel {self.channel}. "
                "This often means the printer does not expose classic Bluetooth "
                "SPP/RFCOMM, the address/channel is wrong, or Windows requires a "
                "mapped outgoing COM port instead. Try '-c bluetooth -a COMx' "
                "after creating the Bluetooth COM port in Windows settings."
            ) from exc

        self._socket = sock
        logger.debug("Connected to %s via RFCOMM channel %s", address, self.channel)

    def connect(self):
        if self._socket is not None or self._serial is not None:
            return

        target = self.address
        if not target or target.lower() == "auto":
            target = self._detect_bluetooth_serial_port()

        if self.looks_like_serial_port(target):
            self._connect_serial(target)
            return

        if self.is_mac_address(target):
            self._connect_rfcomm_socket(target.upper())
            return

        raise RuntimeError(
            f"Unsupported Bluetooth target '{target}'. Use a mapped serial port "
            "like COM7 or /dev/rfcomm0, or a Bluetooth MAC address for raw RFCOMM."
        )

    def close(self):
        if self._socket is not None:
            with suppress(OSError):
                self._socket.close()
            self._socket = None

        if self._serial is not None:
            with suppress(Exception):
                self._serial.close()
            self._serial = None

    def read(self, length: int = 1024) -> bytes:
        self.connect()
        if self._serial is not None:
            return self._serial.read(length)

        assert self._socket is not None
        try:
            return self._socket.recv(length)
        except (TimeoutError, socket.timeout):
            return b""

    def _paced_chunks(self, data: bytes):
        if self.chunk_size and self.chunk_size > 0:
            for pos in range(0, len(data), self.chunk_size):
                yield data[pos : pos + self.chunk_size]
        else:
            yield data

    def write(self, data: bytes):
        self.connect()
        written = 0

        if self._serial is not None:
            for chunk in self._paced_chunks(data):
                written += self._serial.write(chunk)
                # Force pyserial/Windows to hand the chunk to the Bluetooth COM
                # driver before we queue the next protocol packet.
                with suppress(Exception):
                    self._serial.flush()
                if self.write_delay > 0:
                    time.sleep(self.write_delay)
            return written

        assert self._socket is not None
        for chunk in self._paced_chunks(data):
            self._socket.sendall(chunk)
            written += len(chunk)
            if self.write_delay > 0:
                time.sleep(self.write_delay)
        return written
