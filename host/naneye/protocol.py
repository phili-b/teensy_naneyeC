"""Wire protocol between the Teensy and the host. See spec.md section 7.

Mirrors firmware/src/usb_proto.h. The field offsets are asserted on both sides
(static_assert there, tests/test_protocol.py here) so the two cannot drift apart.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

MAGIC = b"NANE"
MAGIC_U32 = 0x454E414E
VERSION = 1

# type
TYPE_IMAGE = 0
TYPE_COMMAND = 1
TYPE_RESPONSE = 2
TYPE_LOG = 3

# format
FMT_GRAY8 = 0
FMT_GRAY10 = 1
FMT_RAW12 = 2

# flags
FLAG_SYNC_LOST = 1 << 0
FLAG_CLOCK_GAP = 1 << 1
FLAG_FIRST_DISCARDED = 1 << 2
FLAG_CONCEALED = 1 << 3
# Bits 4-6 carry the colour filter array (naneye.color): 0 mono, 1 BGGR, 2 GBRG, 3 GRBG,
# 4 RGGB. The sensor does not say whether it is a colour part, so the firmware is told once
# and repeats it in every frame.
FLAG_CFA_SHIFT = 4
FLAG_CFA_MASK = 7 << 4

HEADER_FMT = "<IBBHIIIHHBBHIIHHIII"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
assert HEADER_SIZE == 52, HEADER_SIZE

# Bytes of the header covered by the CRC (everything before the crc32 field itself).
HEADER_CRC_BYTES = 48

FORMAT_NAMES = {FMT_GRAY8: "gray8", FMT_GRAY10: "gray10", FMT_RAW12: "raw12"}
TYPE_NAMES = {TYPE_IMAGE: "image", TYPE_COMMAND: "command",
              TYPE_RESPONSE: "response", TYPE_LOG: "log"}


@dataclass
class Header:
    magic: int = MAGIC_U32
    version: int = VERSION
    type: int = TYPE_IMAGE
    header_len: int = HEADER_SIZE
    payload_len: int = 0
    frame_counter: int = 0
    timestamp_us: int = 0
    width: int = 0
    height: int = 0
    format: int = FMT_GRAY8
    flags: int = 0
    rows_failed: int = 0
    sclk_hz: int = 0
    exposure_pp: int = 0
    cfg0: int = 0
    cfg1: int = 0
    frames_dropped: int = 0
    pixels_concealed: int = 0   # corrupt pixels replaced by their neighbours' mean
    crc32: int = 0

    def pack(self) -> bytes:
        return struct.pack(
            HEADER_FMT, self.magic, self.version, self.type, self.header_len,
            self.payload_len, self.frame_counter, self.timestamp_us, self.width,
            self.height, self.format, self.flags, self.rows_failed, self.sclk_hz,
            self.exposure_pp, self.cfg0, self.cfg1, self.frames_dropped, self.pixels_concealed,
            self.crc32)

    @classmethod
    def unpack(cls, data: bytes) -> "Header":
        if len(data) < HEADER_SIZE:
            raise ValueError(f"header too short: {len(data)}")
        return cls(*struct.unpack(HEADER_FMT, data[:HEADER_SIZE]))

    # --- convenience -------------------------------------------------------------------
    @property
    def format_name(self) -> str:
        return FORMAT_NAMES.get(self.format, f"format{self.format}")

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type, f"type{self.type}")

    @property
    def sync_lost(self) -> bool:
        return bool(self.flags & FLAG_SYNC_LOST)

    @property
    def cfa(self) -> str:
        """'MONO', or the Bayer pattern of the top-left 2x2 as received."""
        from .color import CFA_BY_CODE, MONO

        return CFA_BY_CODE.get((self.flags & FLAG_CFA_MASK) >> FLAG_CFA_SHIFT, MONO)

    def exposure_us(self) -> float:
        """Effective exposure in microseconds, from the pixel-period count and SCLK."""
        if not self.sclk_hz:
            return 0.0
        return self.exposure_pp * 12 / self.sclk_hz * 1e6

    def describe(self) -> str:
        return (f"frame {self.frame_counter} {self.width}x{self.height} "
                f"{self.format_name} {self.payload_len}B "
                f"t={self.timestamp_us / 1e6:.3f}s exp={self.exposure_us() / 1000:.2f}ms "
                f"sclk={self.sclk_hz / 1e6:.3f}MHz cfg=0x{self.cfg0:04X}/0x{self.cfg1:04X} "
                f"dropped={self.frames_dropped} rows_failed={self.rows_failed} "
                f"concealed={self.pixels_concealed}")


def compute_crc(header_bytes: bytes, payload: bytes) -> int:
    """CRC-32 over the header (minus its own crc field) plus the payload."""
    crc = zlib.crc32(header_bytes[:HEADER_CRC_BYTES])
    if payload:
        crc = zlib.crc32(payload, crc)
    return crc & 0xFFFFFFFF


def build_packet(header: Header, payload: bytes = b"") -> bytes:
    """Serialise a complete packet, filling in payload_len and crc32."""
    header.payload_len = len(payload)
    header.header_len = HEADER_SIZE
    header.crc32 = compute_crc(header.pack(), payload)
    return header.pack() + payload


def check_packet(header: Header, payload: bytes) -> bool:
    return compute_crc(header.pack(), payload) == header.crc32


def text_packet(kind: int, text: str) -> bytes:
    return build_packet(Header(type=kind), text.encode("utf-8", "replace"))
