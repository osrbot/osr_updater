"""Validation for externally supplied ESP32-S3 full-flash images."""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import core


PARTITION_TABLE_OFFSET = 0x8000
PARTITION_TABLE_SIZE = 0xC00
PARTITION_ENTRY_SIZE = 32
PARTITION_MAGIC = 0x50AA
PARTITION_END_MAGIC = 0xFFFF
NVS_PARTITION_TYPE = 0x01
NVS_PARTITION_SUBTYPE = 0x02
APP_PARTITION_TYPE = 0x00
ESP_IMAGE_MAGIC = 0xE9
ESP32S3_IMAGE_CHIP_ID = 9
ESP_CHECKSUM_MAGIC = 0xEF
IMAGE_HEADER_BYTES = 24
MIN_RECOVERY_BYTES = PARTITION_TABLE_OFFSET + PARTITION_TABLE_SIZE
MAX_RECOVERY_BYTES = 32 * 1024 * 1024


class RecoveryImageError(core.PackageValidationError):
    pass


@dataclass(frozen=True)
class PartitionEntry:
    type: int
    subtype: int
    offset: int
    size: int
    label: str
    flags: int


@dataclass(frozen=True)
class RecoveryImage:
    path: Path
    data: bytes
    size: int
    sha256: str
    partitions: tuple[PartitionEntry, ...]
    partition_table_sha256: str | None
    nvs_offset: int | None
    nvs_size: int | None
    minimum_flash_size: int

    def safe_summary(self) -> dict[str, Any]:
        return {
            "bytes": self.size,
            "sha256": self.sha256,
            "chip": "ESP32-S3",
            "format": "full_flash",
            "nvs_offset": self.nvs_offset,
            "nvs_bytes": self.nvs_size,
            "partition_table_sha256": self.partition_table_sha256,
            "validation": "passed",
        }


def parse_partition_table(raw_table: bytes) -> tuple[PartitionEntry, ...]:
    """Parse one raw ESP-IDF partition table without trusting file metadata."""

    if len(raw_table) != PARTITION_TABLE_SIZE:
        raise RecoveryImageError("partition table length is invalid")
    entries: list[PartitionEntry] = []
    for offset in range(0, len(raw_table), PARTITION_ENTRY_SIZE):
        raw = raw_table[offset : offset + PARTITION_ENTRY_SIZE]
        magic = struct.unpack_from("<H", raw)[0]
        if magic in {PARTITION_END_MAGIC, 0xEBEB}:
            break
        if magic != PARTITION_MAGIC:
            raise RecoveryImageError("partition table contains an invalid entry")
        _magic, ptype, subtype, address, size, raw_label, flags = struct.unpack(
            "<HBBII16sI", raw
        )
        label_bytes = raw_label.split(b"\x00", 1)[0]
        try:
            label = label_bytes.decode("ascii")
        except UnicodeDecodeError:
            raise RecoveryImageError("partition label is not ASCII") from None
        if (
            not label
            or address % 0x1000
            or size <= 0
            or address + size < address
            or address + size > MAX_RECOVERY_BYTES
        ):
            raise RecoveryImageError("partition table contains invalid bounds")
        entries.append(PartitionEntry(ptype, subtype, address, size, label, flags))
    if not entries:
        raise RecoveryImageError("partition table is empty")
    ordered = sorted(entries, key=lambda entry: entry.offset)
    for previous, current in zip(ordered, ordered[1:]):
        if previous.offset + previous.size > current.offset:
            raise RecoveryImageError("partition table contains overlapping partitions")
    return tuple(entries)


def _read_regular_file(path: Path) -> bytes:
    path = path.expanduser()
    try:
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise OSError
        size = path.stat().st_size
        if not MIN_RECOVERY_BYTES <= size <= MAX_RECOVERY_BYTES:
            raise RecoveryImageError(
                f"full-flash image size must be between {MIN_RECOVERY_BYTES} and "
                f"{MAX_RECOVERY_BYTES} bytes"
            )
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            chunks: list[bytes] = []
            remaining = size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
    except RecoveryImageError:
        raise
    except OSError:
        raise RecoveryImageError(
            "full-flash image must be an absolute, regular, non-symlink file"
        ) from None
    data = b"".join(chunks)
    if len(data) != size:
        raise RecoveryImageError("full-flash image changed or was truncated while reading")
    return data


def _validate_bootloader(data: bytes) -> None:
    try:
        magic, segment_count, _flash_mode, _flash_size_frequency, _entrypoint = (
            struct.unpack_from("<BBBBI", data, 0)
        )
        chip_id = struct.unpack_from("<H", data, 12)[0]
        append_digest = data[23]
    except (IndexError, struct.error):
        raise RecoveryImageError("full-flash bootloader header is truncated") from None
    if magic != ESP_IMAGE_MAGIC or not 1 <= segment_count <= 16:
        raise RecoveryImageError("full-flash image does not contain a valid bootloader")
    if chip_id != ESP32S3_IMAGE_CHIP_ID:
        raise RecoveryImageError("full-flash image target is not ESP32-S3")
    if append_digest not in {0, 1}:
        raise RecoveryImageError("full-flash bootloader validation mode is invalid")

    cursor = IMAGE_HEADER_BYTES
    checksum = ESP_CHECKSUM_MAGIC
    for _index in range(segment_count):
        if cursor + 8 > min(len(data), PARTITION_TABLE_OFFSET):
            raise RecoveryImageError("full-flash bootloader segment header is truncated")
        _address, size = struct.unpack_from("<II", data, cursor)
        cursor += 8
        if size == 0 or size % 4 or cursor + size > min(len(data), PARTITION_TABLE_OFFSET):
            raise RecoveryImageError("full-flash bootloader segment is invalid or truncated")
        segment = data[cursor : cursor + size]
        for value in segment:
            checksum ^= value
        cursor += size

    checksum_offset = cursor + ((15 - (cursor % 16)) % 16)
    if checksum_offset >= min(len(data), PARTITION_TABLE_OFFSET):
        raise RecoveryImageError("full-flash bootloader checksum is missing")
    if data[checksum_offset] != checksum:
        raise RecoveryImageError("full-flash bootloader checksum is invalid")
    bootloader_end = checksum_offset + 1
    if append_digest == 1:
        digest_end = bootloader_end + 32
        if digest_end > min(len(data), PARTITION_TABLE_OFFSET):
            raise RecoveryImageError("full-flash bootloader validation hash is missing")
        if data[bootloader_end:digest_end] != hashlib.sha256(
            data[:bootloader_end]
        ).digest():
            raise RecoveryImageError("full-flash bootloader validation hash is invalid")


def validate_recovery_bytes(data: bytes, *, path: Path | None = None) -> RecoveryImage:
    if not isinstance(data, bytes) or not MIN_RECOVERY_BYTES <= len(data) <= MAX_RECOVERY_BYTES:
        raise RecoveryImageError("file is not a supported ESP32-S3 full-flash image")
    _validate_bootloader(data)
    table = data[PARTITION_TABLE_OFFSET : PARTITION_TABLE_OFFSET + PARTITION_TABLE_SIZE]
    try:
        partitions = parse_partition_table(table)
    except RecoveryImageError:
        partitions = ()
    partition_table_sha256 = hashlib.sha256(table).hexdigest() if partitions else None
    nvs = [
        entry
        for entry in partitions
        if entry.type == NVS_PARTITION_TYPE
        and entry.subtype == NVS_PARTITION_SUBTYPE
        and entry.label == "nvs"
    ]
    standard_nvs = nvs[0] if len(nvs) == 1 else None
    minimum_flash_size = len(data)
    if partitions:
        minimum_flash_size = max(
            minimum_flash_size,
            max(entry.offset + entry.size for entry in partitions),
        )
    return RecoveryImage(
        path=path or Path("<memory>"),
        data=data,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        partitions=partitions,
        partition_table_sha256=partition_table_sha256,
        nvs_offset=None if standard_nvs is None else standard_nvs.offset,
        nvs_size=None if standard_nvs is None else standard_nvs.size,
        minimum_flash_size=minimum_flash_size,
    )


def validate_recovery_file(path: Path) -> RecoveryImage:
    absolute = path.expanduser().absolute()
    return validate_recovery_bytes(_read_regular_file(absolute), path=absolute)


def parse_current_partition_table(data: bytes) -> tuple[PartitionEntry, ...]:
    """Parse a partition table read directly from a connected controller."""

    return parse_partition_table(data)


def find_nvs_partition(entries: tuple[PartitionEntry, ...]) -> PartitionEntry:
    nvs = [
        entry
        for entry in entries
        if entry.type == NVS_PARTITION_TYPE
        and entry.subtype == NVS_PARTITION_SUBTYPE
        and entry.label == "nvs"
    ]
    if len(nvs) != 1:
        raise RecoveryImageError(
            "connected controller does not have one standard vehicle-settings partition"
        )
    return nvs[0]
