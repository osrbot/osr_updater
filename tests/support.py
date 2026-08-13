"""Deterministic firmware, serial, and ROM fixtures for updater tests."""

from __future__ import annotations

import hashlib
import struct
from collections import deque

from osr_updater import core
from osr_updater.images import (
    ESP32S3_DROM_START,
    ESP32S3_IMAGE_CHIP_ID,
    ESP_APP_DESC_MAGIC,
    ESP_CHECKSUM_MAGIC,
    ESP_IMAGE_MAGIC,
)
from osr_updater.recovery import PARTITION_TABLE_OFFSET, PARTITION_TABLE_SIZE
from osr_updater.rom import RomPortHint, RomSecurityInfo


PROFILE_ID = "test"
SOURCE_VERSION = "OSR-TEST-SOURCE"
TARGET_VERSION = "OSR-TEST-TARGET"
NVS_OFFSET = 0x9000
NVS_SIZE = 0x6000
APP_OFFSET = 0x10000


def _fixed(value: str, size: int) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) >= size:
        raise ValueError(value)
    return encoded + b"\0" * (size - len(encoded))


def application_image(version: str = TARGET_VERSION) -> bytes:
    description = struct.pack(
        "<II8s32s32s16s16s32s32sHHB3s72s",
        ESP_APP_DESC_MAGIC,
        0,
        b"\0" * 8,
        _fixed(version, 32),
        _fixed("osr_updater_fixture", 32),
        _fixed("Aug 12 2026", 16),
        _fixed("12:00:00", 16),
        _fixed("v6.0.1", 32),
        b"\0" * 32,
        0,
        0,
        0,
        b"\0" * 3,
        b"\0" * 72,
    )
    segment = description + b"\0" * 768
    header = bytearray(24)
    struct.pack_into("<BBBBI", header, 0, ESP_IMAGE_MAGIC, 1, 0, 0, 0)
    struct.pack_into("<H", header, 12, ESP32S3_IMAGE_CHIP_ID)
    header[23] = 1
    body = bytes(header) + struct.pack("<II", ESP32S3_DROM_START, len(segment)) + segment
    padding = b"\0" * ((15 - (len(body) % 16)) % 16)
    checksum = ESP_CHECKSUM_MAGIC
    for value in segment:
        checksum ^= value
    signed = body + padding + bytes((checksum,))
    return signed + hashlib.sha256(signed).digest()


def partition_entry(
    ptype: int,
    subtype: int,
    offset: int,
    size: int,
    label: str,
) -> bytes:
    return struct.pack(
        "<HBBII16sI",
        0x50AA,
        ptype,
        subtype,
        offset,
        size,
        _fixed(label, 16),
        0,
    )


def recovery_image(version: str = TARGET_VERSION) -> bytes:
    app = application_image(version)
    data = bytearray(APP_OFFSET + len(app))
    boot_segment = b"\x31\x41\x59\x26"
    boot_header = bytearray(24)
    struct.pack_into("<BBBBI", boot_header, 0, ESP_IMAGE_MAGIC, 1, 0, 0, 0)
    struct.pack_into("<H", boot_header, 12, ESP32S3_IMAGE_CHIP_ID)
    boot_header[23] = 1
    boot_body = bytes(boot_header) + struct.pack("<II", 0x40378000, len(boot_segment)) + boot_segment
    boot_padding = b"\0" * ((15 - (len(boot_body) % 16)) % 16)
    boot_checksum = ESP_CHECKSUM_MAGIC
    for value in boot_segment:
        boot_checksum ^= value
    boot_signed = boot_body + boot_padding + bytes((boot_checksum,))
    bootloader = boot_signed + hashlib.sha256(boot_signed).digest()
    data[: len(bootloader)] = bootloader
    table = (
        partition_entry(0x01, 0x02, NVS_OFFSET, NVS_SIZE, "nvs")
        + partition_entry(0x00, 0x00, APP_OFFSET, 0x100000, "factory")
        + b"\xff\xff"
    )
    data[PARTITION_TABLE_OFFSET : PARTITION_TABLE_OFFSET + len(table)] = table
    data[APP_OFFSET : APP_OFFSET + len(app)] = app
    return bytes(data)


def ready_items() -> tuple[core.VehicleConfigItem, ...]:
    items = [
        core.VehicleConfigItem(name, "UNSET", value_type, "-")
        for name, value_type, _size in core.VEHICLE_CONFIG_FIELDS
    ]
    by_name = {item.name: index for index, item in enumerate(items)}
    for name in core.LEVEL_CALIBRATION_OFFSET_FIELDS:
        items[by_name[name]] = core.VehicleConfigItem(name, "SET", "BLOB", "00000000")
    items[by_name[core.LEVEL_CALIBRATION_INIT_FIELD]] = core.VehicleConfigItem(
        core.LEVEL_CALIBRATION_INIT_FIELD,
        "SET",
        "U8",
        "1",
    )
    return tuple(items)


def config_export_lines(project_version: str) -> list[str]:
    items = ready_items()
    digest = core.calculate_vehicle_config_sha256(project_version, PROFILE_ID, 1, items)
    return [
        "CONFIG_EXPORT_BEGIN: ConfigSchema=1, Proto=1.1, Items=24",
        f"CONFIG_EXPORT_SOURCE: ProjectVer={project_version}, Profile={PROFILE_ID}, Schema=1",
        f"CONFIG_EXPORT_TARGET: ProjectVer={project_version}, Profile={PROFILE_ID}, Schema=1",
        f"CONFIG_EXPORT_HASH: BackupSHA={digest}",
        *[
            f"CONFIG_ITEM: Name={item.name}, State={item.state}, "
            f"Type={item.value_type}, Value={item.value}"
            for item in items
        ],
        f"CONFIG_EXPORT_END: Result=OK, Items=24, BackupSHA={digest}, Reason=ok",
    ]


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, duration: float) -> None:
        self.value += max(float(duration), 0.0001)


class FakeSerial:
    def __init__(self, handler, writes: list[str]):
        self.handler = handler
        self.writes = writes
        self.pending: deque[bytes] = deque()
        self.is_open = True

    def write(self, data: bytes) -> int:
        command = data.decode("ascii").rstrip("\n")
        self.writes.append(command)
        response = self.handler(command)
        if isinstance(response, BaseException):
            raise response
        if response is None:
            response = []
        if isinstance(response, str):
            response = [response]
        for line in response:
            self.pending.append((line + "\n").encode("ascii"))
        return len(data)

    def flush(self) -> None:
        return None

    def readline(self) -> bytes:
        return self.pending.popleft() if self.pending else b""

    def reset_input_buffer(self) -> None:
        self.pending.clear()

    def reset_output_buffer(self) -> None:
        return None

    def close(self) -> None:
        self.is_open = False


class SequenceFactory:
    def __init__(self, connections):
        self.connections = deque(connections)
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self.connections:
            raise OSError("no connection")
        connection = self.connections.popleft()
        if isinstance(connection, BaseException):
            raise connection
        return connection


def managed_handler(
    project_version: str,
    *,
    begin_error: str | None = None,
    post_profile_state: str = "READY",
    export_error: bool = False,
):
    transferred = {"written": 0, "size": 0}

    def handler(command: str):
        if command in {"v 0.00 0.00", "stream off"}:
            return []
        if command == "fw version":
            return f"FW_VERSION: ProjectVer={project_version}, Proto=1.1"
        if command == "profile get":
            enabled = "Yes" if post_profile_state == "READY" else "No"
            return (
                f"PROFILE: ID={PROFILE_ID}, Schema=1, State={post_profile_state}, "
                f"Motion={enabled}, Writes={enabled}"
            )
        if command == "fw status":
            return "FW: active=No written=0 size=0 next_seq=0 running=ota_0 next=ota_1"
        if command == "b":
            return "b 12.1"
        if command == "status":
            return [
                "Status: Speed=0.000m/s, Target=0.000m/s, Voltage=12.1V, "
                "Control=Serial, SpeedMode=30%, Static=Yes",
                "IMU: BiasReady=Yes, LevelCal=Yes, GyroBias=0,0,0, LevelOffset=0,0,0",
            ]
        if command == "config export":
            if export_error:
                return "ERROR unsupported"
            return config_export_lines(project_version)
        if command.startswith("fw begin "):
            if begin_error:
                return f"ERROR {begin_error}"
            transferred["written"] = 0
            transferred["size"] = int(command.split()[2])
            return f"OK fw begin part=ota_1 size={transferred['size']}"
        if command.startswith("fw data "):
            _fw, _data, seq_text, payload = command.split()
            transferred["written"] += len(bytes.fromhex(payload))
            return f"OK fw data {int(seq_text)} {transferred['written']}"
        if command == "fw end":
            return "OK fw reboot"
        if command == "fw abort":
            return "OK fw abort"
        return "ERROR unsupported"

    return handler


class FakeRomSession:
    def __init__(
        self,
        existing_recovery: bytes,
        settings: bytes,
        *,
        identity: str = "a" * 64,
        corrupt_readback: bool = False,
    ):
        self.security = RomSecurityInfo(
            "ESP32-S3",
            16 * 1024 * 1024,
            identity,
            False,
            False,
            False,
        )
        self.port_hint = RomPortHint(
            device="/dev/ttyACM-test",
            usb_location="test-usb-port",
            serial_number="test-controller",
        )
        self.existing_recovery = existing_recovery
        self.settings = settings
        self.corrupt_readback = corrupt_readback
        self.erased = False
        self.writes: list[tuple[int, str, int, int]] = []
        self.reset = False
        self.closed = False

    def read_flash(self, offset: int, size: int) -> bytes:
        if offset == PARTITION_TABLE_OFFSET and size == PARTITION_TABLE_SIZE:
            return self.existing_recovery[offset : offset + size]
        if offset == NVS_OFFSET and size == NVS_SIZE:
            if self.corrupt_readback and any(item[0] == NVS_OFFSET for item in self.writes):
                return b"\xff" * size
            return self.settings
        return b"\xff" * size

    def erase_flash(self) -> None:
        self.erased = True
        self.settings = b"\xff" * NVS_SIZE

    def write_flash(
        self,
        offset: int,
        data: bytes,
        *,
        flash_size: int,
        progress=None,
    ) -> None:
        if progress is not None:
            progress(0, len(data))
            progress(len(data) // 2, len(data))
            progress(len(data), len(data))
        self.writes.append((offset, hashlib.sha256(data).hexdigest(), len(data), flash_size))
        if offset == 0:
            self.existing_recovery = data
            if len(data) >= NVS_OFFSET + NVS_SIZE:
                self.settings = data[NVS_OFFSET : NVS_OFFSET + NVS_SIZE]
        elif offset == NVS_OFFSET:
            self.settings = data

    def hard_reset(self) -> None:
        self.reset = True

    def close(self) -> None:
        self.closed = True


class FakeRomFactory:
    def __init__(self, sessions):
        self.sessions = deque(sessions)
        self.calls = []

    def open(self, port: str, *, baud: int, hint=None):
        self.calls.append((port, baud, hint))
        if not self.sessions:
            raise OSError("no ROM session")
        return self.sessions.popleft()
