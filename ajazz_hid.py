"""ajazz_hid.py -- HID transport for the AJAZZ AK820 Max Plus.

Scope:

* Phase 1 -- device discovery and the ``0x07`` lighting-configuration packet.
* Phase 2 -- the per-key RGB table (``0xF6``..``0xFB`` + ``0x0A`` commit) used by
  Custom Mode, exposed through :func:`upload_keymap`.

The vendor utility (``DeviceDriver.exe``) drives the keyboard through plain
``WriteFile`` calls on a HID handle, using 65-byte output reports (report ID
``0x00`` + 64 payload bytes) and the header ``00 AA 55 CC 33``.  This module
reproduces exactly that, so no third-party driver or DLL is required.

Two backends are provided:

``Win32Backend``
    Pure ``ctypes`` over ``hid.dll`` / ``setupapi.dll``.  This is the default on
    Windows and has no external dependencies.

``HidApiBackend``
    Optional wrapper around the ``hid`` (hidapi) package, used automatically
    when it is importable.  Useful for cross-platform work on Phase 3+.

Protocol reference: see ``PROTOCOL.md`` in this directory.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

# --------------------------------------------------------------------------
# Device identity
# --------------------------------------------------------------------------

VENDOR_ID = 0x1A2C
PRODUCT_ID = 0xA036

#: USB-reported product string of the keyboard ("AK820").
PRODUCT_NAME = "AK820"

REPORT_ID = 0x00
REPORT_SIZE = 65  # 1 report-ID byte + 64 payload bytes

MAGIC = bytes((0xAA, 0x55, 0xCC, 0x33))

#: HID usage page used by the vendor-defined lighting interface.
#: Confirmed on hardware: MI_00 exposes usage page 0xFF02, usage 0x0002,
#: with a 65-byte output report -- that is the collection the vendor tool writes to.
VENDOR_USAGE_PAGE = 0xFF02


# --------------------------------------------------------------------------
# Per-key RGB table geometry
# --------------------------------------------------------------------------

#: Addressable LED slots.  The table is 118 x (R, G, B) = 354 bytes.
KEY_COUNT = 118
#: Number of HID reports the flat table is split across.
KEY_CHUNKS = 6
#: Payload bytes per report -- buffer bytes [6..64].
CHUNK_LEN = 59
#: Total table size in bytes.
KEY_TABLE_SIZE = KEY_COUNT * 3  # 354

#: Default file name for the index -> key-label map produced by matrix_mapper.py.
KEYMAP_MATRIX_FILE = "keymap_matrix.json"

#: Small pause between the seven keymap writes (seconds).  Purely defensive:
#: back-to-back reports usually work, but a short gap avoids firmware drops.
KEYMAP_WRITE_DELAY = 0.001

#: Short alias, e.g. ``(255, 0, 0)``.
RGB = tuple[int, int, int]


# --------------------------------------------------------------------------
# Protocol enumerations
# --------------------------------------------------------------------------


class Command(IntEnum):
    """Opcode carried at buffer offset ``[5]``."""

    LIGHTING = 0x07        #: lighting configuration (this module)
    MODE_ARM_B = 0x09      #: second Custom-Mode arm frame (measured, see below)
    KEYMAP_COMMIT = 0x0A   #: commit the per-key RGB table (Phase 2)
    MODE_ARM_A = 0x0B      #: first Custom-Mode arm frame (measured, see below)
    KEEPALIVE = 0x0E       #: periodic heartbeat, ~5.02 s
    KEYMAP_CHUNK_BASE = 0xF6  #: 0xF6..0xFB carry the per-key table (Phase 2)


class Mode(IntEnum):
    """Lighting mode, offset ``[6]``.

    These are wire values, not positions in the vendor UI list.  ``CUSTOM`` is
    ``0x17``: captured from ``DeviceDriver.exe`` on 2026-10-28 when the app was
    switched back to the per-key page.  The previously assumed ``18`` sends an
    *animated* effect (rainbow on this firmware), never the uploaded table.
    """

    LEFT_RIGHT_RIPPLES = 0
    STARRY_DOTS = 1
    UP_DOWN_RIPPLES = 2
    BIDIRECTIONAL_EJECTION = 3
    SPECTRAL_CYCLE = 4
    KALEIDOSCOPE = 5
    STATIC = 6
    OVERALL_BREATHING = 7
    CROSS_OPERATION = 8
    SINGLE_KEY_BREATHING = 9
    JIANGSHAN = 10
    TOP3_BOTTOM3 = 11
    RAIN_PAVILION = 12
    WINDMILL = 13
    SINE_WAVE = 14
    GALLOPING_HORSE = 15
    MUSIC_RHYTHM = 16
    SCREEN_RHYTHM = 17
    CUSTOM = 0x17


class Color(IntEnum):
    """Palette index, offset ``[10]``.  The firmware resolves the real RGB."""

    RED = 0
    GREEN = 1
    BLUE = 2
    YELLOW = 3
    MAGENTA = 4
    CYAN = 5
    WHITE = 6
    CUSTOM = 7


#: Brightness and speed are 1-based in the UI and 0-based on the wire.
LEVEL_MIN = 1
LEVEL_MAX = 5

#: Kaleidoscope is the only mode observed setting a non-zero variant byte.
MODE_VARIANT = {Mode.KALEIDOSCOPE: 0x01}


def encode_level(level: int) -> int:
    """Convert a UI level (1..5) to its on-wire value (0x00..0x04)."""
    if not LEVEL_MIN <= level <= LEVEL_MAX:
        raise ValueError(
            f"level must be {LEVEL_MIN}..{LEVEL_MAX}, got {level!r} "
            "(the UI slider tops out at 5, which encodes as 0x04)"
        )
    return level - LEVEL_MIN


# --------------------------------------------------------------------------
# Packet construction
# --------------------------------------------------------------------------


def build_lighting_packet(
    mode: Mode | int,
    brightness: int = LEVEL_MAX,
    speed: int = 4,
    color: Color | int = Color.CYAN,
    variant: Optional[int] = None,
) -> bytes:
    """Build a complete 65-byte ``0x07`` lighting report.

    Buffer layout (see PROTOCOL.md section 2)::

        [0]     0x00                     report ID
        [1..4]  AA 55 CC 33              magic
        [5]     0x07                     command
        [6]     mode                     0x00..0x17
        [7]     brightness - 1           0x00..0x04
        [8]     speed - 1                0x00..0x04
        [9]     variant                  mode specific (0, or 1 for Kaleidoscope)
        [10]    colour palette index     0x00..0x07
        [11..64] 0x00                    padding
    """
    mode = Mode(int(mode))
    color = Color(int(color))

    if variant is None:
        variant = MODE_VARIANT.get(mode, 0x00)

    payload = bytearray(REPORT_SIZE - 1)  # 64 bytes after the report ID
    payload[0:4] = MAGIC
    payload[4] = Command.LIGHTING
    payload[5] = mode
    payload[6] = encode_level(brightness)
    payload[7] = encode_level(speed)
    payload[8] = variant & 0xFF
    payload[9] = color

    return bytes((REPORT_ID,)) + bytes(payload)


def static_cyan_packet(brightness: int = LEVEL_MAX, speed: int = 4) -> bytes:
    """The Phase 1 acceptance packet: Static constant light, cyan."""
    return build_lighting_packet(Mode.STATIC, brightness, speed, Color.CYAN)


def format_packet(packet: bytes) -> str:
    """Render a packet as space-separated hex, matching the captures in PROTOCOL.md."""
    return " ".join(f"{b:02x}" for b in packet)


# --------------------------------------------------------------------------
# Per-key RGB table (0xF6..0xFB + 0x0A commit)
# --------------------------------------------------------------------------


def _check_channel(index: int, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"key {index}: colour channel {value!r} is not an integer")
    if not 0 <= value <= 255:
        raise ValueError(f"key {index}: colour channel {value} outside 0..255")
    return int(value)


def pack_key_table(keys: Sequence[RGB]) -> bytes:
    """Flatten 118 ``(r, g, b)`` tuples into the 354-byte wire table."""
    if len(keys) != KEY_COUNT:
        raise ValueError(f"expected {KEY_COUNT} key colours, got {len(keys)}")

    table = bytearray(KEY_TABLE_SIZE)
    for index, rgb in enumerate(keys):
        try:
            r, g, b = rgb
        except (TypeError, ValueError):
            raise ValueError(
                f"key {index}: expected an (r, g, b) tuple, got {rgb!r}"
            ) from None
        base = 3 * index
        table[base] = _check_channel(index, r)
        table[base + 1] = _check_channel(index, g)
        table[base + 2] = _check_channel(index, b)
    return bytes(table)


def parse_key_table(table: bytes | bytearray) -> list[RGB]:
    """Split the 354-byte table back into 118 ``(r, g, b)`` tuples."""
    if len(table) != KEY_TABLE_SIZE:
        raise ValueError(
            f"key table must be {KEY_TABLE_SIZE} bytes, got {len(table)}"
        )
    return [
        (table[3 * i], table[3 * i + 1], table[3 * i + 2]) for i in range(KEY_COUNT)
    ]


def build_key_table(
    colours: Mapping[int, RGB] | None = None,
    background: RGB = (0, 0, 0),
) -> bytes:
    """Build a table from a sparse ``{index: (r, g, b)}`` map.

    Every index not present in ``colours`` gets ``background`` (black by default).
    """
    keys: list[RGB] = [background] * KEY_COUNT
    for index, rgb in (colours or {}).items():
        if not isinstance(index, int) or not 0 <= index < KEY_COUNT:
            raise ValueError(f"key index {index!r} outside 0..{KEY_COUNT - 1}")
        keys[index] = rgb
    return pack_key_table(keys)


def build_keymap_chunks(table: bytes) -> list[bytes]:
    """Split a 354-byte table into the six ``0xF6``..``0xFB`` reports.

    The table is a flat byte stream, so RGB triples straddle report boundaries
    (59 is not a multiple of 3).  Do not split on key boundaries.
    """
    if len(table) != KEY_TABLE_SIZE:
        raise ValueError(
            f"key table must be {KEY_TABLE_SIZE} bytes, got {len(table)}"
        )

    packets: list[bytes] = []
    for chunk in range(KEY_CHUNKS):
        payload = bytearray(REPORT_SIZE - 1)
        payload[0:4] = MAGIC
        payload[4] = int(Command.KEYMAP_CHUNK_BASE) + chunk
        start = chunk * CHUNK_LEN
        payload[5 : 5 + CHUNK_LEN] = table[start : start + CHUNK_LEN]
        packets.append(bytes((REPORT_ID,)) + bytes(payload))
    return packets


def build_commit_packet() -> bytes:
    """The ``0x0A`` frame that makes the keyboard apply the uploaded table."""
    payload = bytearray(REPORT_SIZE - 1)
    payload[0:4] = MAGIC
    payload[4] = Command.KEYMAP_COMMIT
    return bytes((REPORT_ID,)) + bytes(payload)


def build_opcode_packet(command: Command | int) -> bytes:
    """Header + opcode, everything else zero (``0x09``, ``0x0A``, ``0x0B``, ``0x0E``)."""
    payload = bytearray(REPORT_SIZE - 1)
    payload[0:4] = MAGIC
    payload[4] = int(command)
    return bytes((REPORT_ID,)) + bytes(payload)


def build_keepalive_packet() -> bytes:
    """The ``0x0E`` heartbeat: header + opcode, everything else zero."""
    return build_opcode_packet(Command.KEEPALIVE)


# --------------------------------------------------------------------------
# Device info / backend abstraction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceInfo:
    path: str
    vendor_id: int
    product_id: int
    usage_page: Optional[int] = None
    usage: Optional[int] = None
    output_report_length: Optional[int] = None
    product_string: Optional[str] = None

    @property
    def is_vendor_interface(self) -> bool:
        """True for vendor-defined usage pages (0xFF00..0xFFFF)."""
        return self.usage_page is not None and 0xFF00 <= self.usage_page <= 0xFFFF

    def describe(self) -> str:
        return (
            f"VID_{self.vendor_id:04X} PID_{self.product_id:04X} "
            f"usage_page=0x{(self.usage_page or 0):04X} "
            f"usage=0x{(self.usage or 0):04X} "
            f"out_len={self.output_report_length} "
            f"product={self.product_string!r}\n    {self.path}"
        )


class AjazzError(RuntimeError):
    """Raised when the keyboard cannot be opened or written to."""


class AjazzKeyboard:
    """Open keyboard handle.  Use as a context manager."""

    def __init__(self, device: "BaseBackend", info: DeviceInfo) -> None:
        self._device = device
        self.info = info

    # -- lifecycle ------------------------------------------------------
    def __enter__(self) -> "AjazzKeyboard":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._device.close()

    # -- I/O ------------------------------------------------------------
    def send(self, packet: bytes) -> int:
        """Write one 65-byte report.  Returns the number of bytes accepted."""
        if len(packet) != REPORT_SIZE:
            raise ValueError(f"packet must be {REPORT_SIZE} bytes, got {len(packet)}")
        return self._device.write(packet)

    def set_lighting(
        self,
        mode: Mode | int,
        brightness: int = LEVEL_MAX,
        speed: int = 4,
        color: Color | int = Color.CYAN,
        variant: Optional[int] = None,
    ) -> int:
        return self.send(build_lighting_packet(mode, brightness, speed, color, variant))

    def set_static(self, color: Color | int = Color.CYAN, brightness: int = LEVEL_MAX,
                   speed: int = 4) -> int:
        return self.set_lighting(Mode.STATIC, brightness, speed, color)

    # -- per-key RGB ----------------------------------------------------
    def send_keymap(
        self,
        table: bytes,
        commit: bool = True,
        delay: float = KEYMAP_WRITE_DELAY,
    ) -> int:
        """Push a 354-byte table and (optionally) commit it.

        Returns the total number of bytes accepted (455 when committed).
        """
        total = 0
        for packet in build_keymap_chunks(table):
            total += self.send(packet)
            if delay:
                time.sleep(delay)

        if commit:
            total += self.send(build_commit_packet())
        return total

    def upload_keymap(
        self,
        keys: Sequence[RGB],
        commit: bool = True,
        delay: float = KEYMAP_WRITE_DELAY,
    ) -> int:
        """Send 118 ``(r, g, b)`` tuples as six reports plus the commit frame."""
        return self.send_keymap(pack_key_table(keys), commit=commit, delay=delay)

    def fill_key_colour(
        self,
        index: int,
        rgb: RGB = (255, 255, 255),
        commit: bool = True,
        delay: float = KEYMAP_WRITE_DELAY,
    ) -> int:
        """Light a single slot and black out everything else (mapper probe)."""
        return self.send_keymap(build_key_table({index: rgb}), commit=commit, delay=delay)

    def enter_custom_mode(
        self,
        brightness: int = LEVEL_MAX,
        speed: int = 4,
        color: Color | int = Color.CUSTOM,
    ) -> int:
        """Ask the keyboard to display the uploaded per-key table.

        Measured against the vendor app on 2026-10-28; the app emits exactly::

            0x07  mode=0x17 brightness speed variant=0 color=7
            0x0B  (empty)
            0x09  (empty)
            ... then the ``0xF6``..``0xFB`` table and, later, ``0x0A``

        All three frames are required: ``0x07 mode=0x17`` alone leaves the board
        animating, and the table is ignored until the two arm frames follow.
        Returns the number of bytes accepted.
        """
        total = self.set_lighting(Mode.CUSTOM, brightness, speed, color)
        total += self.send(build_opcode_packet(Command.MODE_ARM_A))
        total += self.send(build_opcode_packet(Command.MODE_ARM_B))
        return total


class BaseBackend:
    """Minimal write/close interface shared by the backends."""

    def write(self, packet: bytes) -> int:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


# --------------------------------------------------------------------------
# Backend: pure ctypes over hid.dll / setupapi.dll (Windows)
# --------------------------------------------------------------------------


class Win32Backend(BaseBackend):
    """HID access through Win32 itself -- no third-party dependency.

    Mirrors what the vendor tool does: ``CreateFile`` on the device interface
    path, then ``WriteFile`` with the 65-byte report.
    """

    def __init__(self, path: str) -> None:
        if sys.platform != "win32":
            raise AjazzError("Win32Backend is Windows-only; use HidApiBackend")

        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._create_file = k32.CreateFileW
        self._create_file.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self._create_file.restype = wintypes.HANDLE

        self._write_file = k32.WriteFile
        self._write_file.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        self._write_file.restype = wintypes.BOOL

        self._close_handle = k32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL

        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3

        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        handle = self._create_file(
            path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle is None or handle == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise AjazzError(
                f"CreateFileW failed for {path!r} (winerror {err}). "
                "If the device is busy, close the AJAZZ vendor app and retry."
            )

        self._handle = handle

    def write(self, packet: bytes) -> int:
        ctypes = self._ctypes
        wintypes = self._wintypes

        buf = ctypes.create_string_buffer(packet, len(packet))
        written = wintypes.DWORD(0)
        ok = self._write_file(self._handle, buf, len(packet), ctypes.byref(written), None)
        if not ok:
            err = ctypes.get_last_error()
            raise AjazzError(f"WriteFile failed (winerror {err})")
        return int(written.value)

    def close(self) -> None:
        if self._handle and self._handle != -1:
            self._close_handle(self._handle)
            self._handle = None


def _win32_enumerate_interfaces() -> list[str]:
    """Return every present HID device-interface path."""
    import ctypes
    from ctypes import wintypes

    hid = ctypes.WinDLL("hid", use_last_error=True)
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
        ]

    class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
            ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_void_p),
        ]

    class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", ctypes.c_wchar * 1)]

    hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
    hid.HidD_GetHidGuid.restype = None

    DIGCF_PRESENT = 0x02
    DIGCF_DEVICEINTERFACE = 0x10

    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD,
    ]
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE

    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID),
        wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL

    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL

    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))

    handle = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    if handle == wintypes.HANDLE(-1).value:
        raise AjazzError("SetupDiGetClassDevsW failed")

    paths: list[str] = []
    try:
        index = 0
        while True:
            iface = SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                handle, None, ctypes.byref(guid), index, ctypes.byref(iface)
            ):
                break
            index += 1

            required = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                handle, ctypes.byref(iface), None, 0, ctypes.byref(required), None
            )
            if required.value == 0:
                continue

            detail = ctypes.create_string_buffer(required.value)
            # cbSize must be set before the second call.
            ctypes.cast(detail, ctypes.POINTER(wintypes.DWORD))[0] = ctypes.sizeof(
                SP_DEVICE_INTERFACE_DETAIL_DATA_W
            )
            ok = setupapi.SetupDiGetDeviceInterfaceDetailW(
                handle,
                ctypes.byref(iface),
                ctypes.cast(detail, ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W)),
                required.value,
                ctypes.byref(required),
                None,
            )
            if ok:
                paths.append(ctypes.wstring_at(ctypes.addressof(detail) + 4))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(handle)

    return paths


def _win32_probe_path(path: str) -> Optional[DeviceInfo]:
    """Open ``path`` read-only and return its identity, or ``None`` on failure."""
    import ctypes
    from ctypes import wintypes

    hid = ctypes.WinDLL("hid", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class HIDD_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Size", wintypes.ULONG), ("VendorID", wintypes.USHORT),
            ("ProductID", wintypes.USHORT), ("VersionNumber", wintypes.USHORT),
        ]

    class HIDP_CAPS(ctypes.Structure):
        _fields_ = [
            ("Usage", wintypes.USHORT), ("UsagePage", wintypes.USHORT),
            ("InputReportByteLength", wintypes.USHORT),
            ("OutputReportByteLength", wintypes.USHORT),
            ("FeatureReportByteLength", wintypes.USHORT),
            ("Reserved", wintypes.USHORT * 17),
            ("NumberLinkCollectionNodes", wintypes.USHORT),
            ("NumberInputButtonCaps", wintypes.USHORT),
            ("NumberInputValueCaps", wintypes.USHORT),
            ("NumberInputDataIndices", wintypes.USHORT),
            ("NumberOutputButtonCaps", wintypes.USHORT),
            ("NumberOutputValueCaps", wintypes.USHORT),
            ("NumberOutputDataIndices", wintypes.USHORT),
            ("NumberFeatureButtonCaps", wintypes.USHORT),
            ("NumberFeatureValueCaps", wintypes.USHORT),
            ("NumberFeatureDataIndices", wintypes.USHORT),
        ]

    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
    hid.HidD_GetAttributes.restype = wintypes.BOOL
    hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    hid.HidD_GetPreparsedData.restype = wintypes.BOOL
    hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    hid.HidD_FreePreparsedData.restype = wintypes.BOOL
    hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
    hid.HidP_GetCaps.restype = wintypes.INT
    hid.HidD_GetProductString.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
    hid.HidD_GetProductString.restype = wintypes.BOOL

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3

    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    handle = k32.CreateFileW(
        path, GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None,
    )
    if handle is None or handle == INVALID_HANDLE_VALUE:
        return None

    try:
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
        if not hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
            return None

        usage_page = usage = out_len = None
        preparsed = ctypes.c_void_p()
        if hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            try:
                caps = HIDP_CAPS()
                if hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) >= 0:
                    usage = int(caps.Usage)
                    usage_page = int(caps.UsagePage)
                    out_len = int(caps.OutputReportByteLength)
            finally:
                hid.HidD_FreePreparsedData(preparsed)

        name_buf = ctypes.create_unicode_buffer(128)
        product = None
        if hid.HidD_GetProductString(handle, name_buf, ctypes.sizeof(name_buf)):
            product = name_buf.value or None

        return DeviceInfo(
            path=path,
            vendor_id=int(attrs.VendorID),
            product_id=int(attrs.ProductID),
            usage_page=usage_page,
            usage=usage,
            output_report_length=out_len,
            product_string=product,
        )
    finally:
        k32.CloseHandle(handle)


# --------------------------------------------------------------------------
# Backend: hidapi (optional)
# --------------------------------------------------------------------------


class HidApiBackend(BaseBackend):
    """Thin adapter over the ``hid`` (hidapi) package."""

    def __init__(self, path: bytes | str) -> None:
        import hid  # noqa: PLC0415 - optional dependency

        self._dev = hid.device()
        self._dev.open_path(path)

    def write(self, packet: bytes) -> int:
        return int(self._dev.write(packet))

    def close(self) -> None:
        try:
            self._dev.close()
        except Exception:  # pragma: no cover - best effort
            pass


def _hidapi_available() -> bool:
    try:
        import hid  # noqa: F401,PLC0415
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------
# Public discovery API
# --------------------------------------------------------------------------

_VID_PID_RE = re.compile(r"vid_([0-9a-fA-F]{4})&pid_([0-9a-fA-F]{4})")


def enumerate_devices(
    vendor_id: int = VENDOR_ID,
    product_id: int = PRODUCT_ID,
) -> list[DeviceInfo]:
    """List HID interfaces belonging to the keyboard.

    Prefers the vendor-defined collection if it can be determined, but returns
    every matching interface so callers can inspect or override the choice.
    """
    if sys.platform == "win32":
        found: list[DeviceInfo] = []
        for path in _win32_enumerate_interfaces():
            match = _VID_PID_RE.search(path)
            if not match:
                continue
            path_vid = int(match.group(1), 16)
            path_pid = int(match.group(2), 16)
            if path_vid != vendor_id or path_pid != product_id:
                continue
            info = _win32_probe_path(path)
            if info is None:
                continue
            # A few low-level ``\kbd`` collection paths report all-zero
            # attributes; the interface path itself is authoritative.
            if info.vendor_id == 0 and info.product_id == 0:
                info = replace(info, vendor_id=path_vid, product_id=path_pid)
            found.append(info)
        found.sort(key=lambda d: (not d.is_vendor_interface, d.path))
        return found

    if _hidapi_available():
        import hid  # noqa: PLC0415

        infos = []
        for entry in hid.enumerate(vendor_id, product_id):
            infos.append(
                DeviceInfo(
                    path=entry["path"].decode(errors="replace")
                    if isinstance(entry["path"], bytes) else str(entry["path"]),
                    vendor_id=entry["vendor_id"],
                    product_id=entry["product_id"],
                    usage_page=entry.get("usage_page"),
                    usage=entry.get("usage"),
                    product_string=entry.get("product_string"),
                )
            )
        return infos

    raise AjazzError("no usable HID backend on this platform")


def pick_device(devices: Iterable[DeviceInfo]) -> DeviceInfo:
    """Choose the interface that carries the lighting reports.

    Preference order:
      1. vendor-defined usage page (0xFF00) with a 65-byte output report
      2. vendor-defined usage page
      3. any interface with a 65-byte output report
      4. first interface
    """
    candidates = list(devices)
    if not candidates:
        raise AjazzError(
            f"AK820 not found (looking for VID_{VENDOR_ID:04X} PID_{PRODUCT_ID:04X}). "
            "Check the USB cable / 2.4 GHz dongle and that the keyboard is powered on."
        )

    def score(d: DeviceInfo) -> tuple[int, int]:
        vendor = d.is_vendor_interface
        exact_len = d.output_report_length in (REPORT_SIZE, REPORT_SIZE + 1)
        if vendor and exact_len:
            return (0, 0)
        if vendor:
            return (1, 0)
        if exact_len:
            return (2, 0)
        return (3, 0)

    return sorted(candidates, key=score)[0]


def open_device(
    path: Optional[str] = None,
    vendor_id: int = VENDOR_ID,
    product_id: int = PRODUCT_ID,
) -> AjazzKeyboard:
    """Open the keyboard, auto-selecting the correct HID interface."""
    if path is None:
        path = pick_device(enumerate_devices(vendor_id, product_id)).path

    if sys.platform == "win32":
        backend: BaseBackend = Win32Backend(path)
    elif _hidapi_available():
        backend = HidApiBackend(path)
    else:
        raise AjazzError("no usable HID backend on this platform")

    info = DeviceInfo(path=path, vendor_id=vendor_id, product_id=product_id)
    return AjazzKeyboard(backend, info)


def upload_keymap(
    keys: Sequence[RGB],
    path: Optional[str] = None,
    commit: bool = True,
    delay: float = KEYMAP_WRITE_DELAY,
    enter_custom: bool = True,
    brightness: int = LEVEL_MAX,
) -> int:
    """One-shot helper: open the keyboard, (optionally) select Custom Mode,
    upload 118 ``(r, g, b)`` tuples and commit.

    Returns the number of bytes accepted.
    """
    with open_device(path=path) as kb:
        if enter_custom:
            kb.enter_custom_mode(brightness=brightness)
        return kb.upload_keymap(keys, commit=commit, delay=delay)


# --------------------------------------------------------------------------
# keymap_matrix.json helpers
# --------------------------------------------------------------------------


def load_keymap_matrix(path: str | os.PathLike[str] = KEYMAP_MATRIX_FILE) -> dict[int, str]:
    """Read an index -> key-label map.  Returns ``{}`` when the file is absent.

    Accepts both this module's format (``{"index_to_key": {...}}``) and a bare
    ``{"15": "Esc"}`` object.
    """
    p = Path(path)
    if not p.exists():
        return {}

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AjazzError(f"could not read {p}: {exc}") from exc

    entries = raw.get("index_to_key") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        entries = raw if isinstance(raw, dict) else {}

    mapping: dict[int, str] = {}
    for key, label in entries.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(label, str) and label.strip():
            mapping[index] = label.strip()
    return dict(sorted(mapping.items()))


def save_keymap_matrix(
    index_to_key: Mapping[int, str],
    path: str | os.PathLike[str] = KEYMAP_MATRIX_FILE,
) -> Path:
    """Write the map atomically, with both directions for convenience."""
    clean = {int(i): str(v).strip() for i, v in index_to_key.items() if str(v).strip()}

    key_to_index: dict[str, int] = {}
    for index in sorted(clean):
        key_to_index.setdefault(clean[index], index)

    doc = {
        "format": 1,
        "key_count": KEY_COUNT,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "index_to_key": {str(k): clean[k] for k in sorted(clean)},
        "key_to_index": dict(sorted(key_to_index.items())),
    }

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


__all__ = [
    "VENDOR_ID", "PRODUCT_ID", "PRODUCT_NAME", "REPORT_SIZE", "MAGIC",
    "VENDOR_USAGE_PAGE", "KEY_COUNT", "KEY_CHUNKS", "CHUNK_LEN", "KEY_TABLE_SIZE",
    "KEYMAP_MATRIX_FILE", "KEYMAP_WRITE_DELAY", "RGB",
    "Command", "Mode", "Color", "LEVEL_MIN", "LEVEL_MAX",
    "DeviceInfo", "AjazzError", "AjazzKeyboard",
    "build_lighting_packet", "static_cyan_packet", "format_packet",
    "encode_level", "enumerate_devices", "pick_device", "open_device",
    "pack_key_table", "parse_key_table", "build_key_table",
    "build_keymap_chunks", "build_commit_packet", "build_keepalive_packet",
    "upload_keymap", "load_keymap_matrix", "save_keymap_matrix",
]
