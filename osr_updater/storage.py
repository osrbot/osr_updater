"""Private, durable storage for vehicle settings and recovery evidence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import core


RAW_NVS_BACKUP_PURPOSES = frozenset(
    {
        "operation_snapshot",
        "restore_safety_snapshot",
    }
)


@dataclass(frozen=True)
class StoredFile:
    path: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class RawNvsBackup:
    data: StoredFile
    metadata: StoredFile
    purpose: str
    device_identity_sha256: str
    selected_firmware_sha256: str
    source_project_version: str | None
    partition_table_sha256: str
    offset: int
    size: int
    captured_at: str


def default_state_directory() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    if not root.is_absolute():
        root = Path.home() / ".local" / "state"
    return root / "osr-updater"


def _safe_directory(directory: Path) -> Path:
    directory = directory.expanduser()
    if not directory.is_absolute():
        raise core.AuditError("updater state directory must be absolute and outside the repository")
    try:
        directory = directory.resolve(strict=False)
    except OSError:
        raise core.AuditError("could not resolve the private updater state directory") from None
    if directory.is_symlink() or core._inside_repository(directory):
        raise core.AuditError("updater state directory must be absolute and outside the repository")
    current = Path(directory.anchor)
    try:
        for part in directory.parts[1:]:
            current /= part
            if current.is_symlink():
                raise OSError("symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise OSError("symlink")
        directory = directory.resolve()
        if core._inside_repository(directory) or directory.stat().st_uid != os.getuid():
            raise OSError("unsafe owner or location")
        os.chmod(directory, 0o700)
    except OSError:
        raise core.AuditError("could not prepare the private updater state directory") from None
    return directory


def write_private_file(
    directory: Path,
    *,
    prefix: str,
    suffix: str,
    data: bytes,
) -> StoredFile:
    if not re_safe_component(prefix) or not re_safe_suffix(suffix):
        raise core.AuditError("private file name is invalid")
    directory = _safe_directory(directory)
    digest = hashlib.sha256(data).hexdigest()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = directory / f"{prefix}-{timestamp}-{digest[:12]}-{uuid.uuid4().hex[:8]}{suffix}"
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{prefix}-", dir=directory)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, final)
        temporary = None
        os.chmod(final, 0o600)
        directory_descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError:
        raise core.AuditError("could not atomically store a private updater file") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    loaded = read_private_file(final, expected_size=len(data), expected_sha256=digest)
    return StoredFile(final, len(loaded), digest)


def re_safe_component(value: str) -> bool:
    return bool(value) and len(value) <= 48 and all(
        character.islower() or character.isdigit() or character == "-"
        for character in value
    )


def re_safe_suffix(value: str) -> bool:
    return value in {".bin", ".json", ".jsonl"}


def read_private_file(
    path: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    portable_read: bool = False,
) -> bytes:
    path = path.expanduser()
    try:
        stat_result = path.stat(follow_symlinks=False)
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or stat_result.st_uid != os.getuid()
            or stat_result.st_mode & (0o022 if portable_read else 0o077)
            or core._inside_repository(path)
        ):
            raise OSError("unsafe private file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError:
        raise core.AuditError("private updater file failed ownership or permission checks") from None
    digest = hashlib.sha256(data).hexdigest()
    if expected_size is not None and len(data) != expected_size:
        raise core.AuditError("private updater file size changed")
    if expected_sha256 is not None and digest != expected_sha256:
        raise core.AuditError("private updater file SHA256 changed")
    return data


def write_raw_nvs_backup(
    data: bytes,
    *,
    directory: Path,
    selected_firmware_sha256: str,
    device_identity_sha256: str,
    source_project_version: str | None,
    partition_table_sha256: str,
    offset: int,
    size: int,
    purpose: str = "operation_snapshot",
) -> RawNvsBackup:
    if (
        len(data) != size
        or not re_full_sha(device_identity_sha256)
        or not re_full_sha(selected_firmware_sha256)
        or not re_full_sha(partition_table_sha256)
        or purpose not in RAW_NVS_BACKUP_PURPOSES
    ):
        raise core.AuditError("complete vehicle-settings backup input is invalid")
    directory = _safe_directory(directory)
    digest = hashlib.sha256(data).hexdigest()
    reusable_data: StoredFile | None = None
    for metadata_path in directory.glob("settings-snapshot-metadata-*.json"):
        try:
            existing = load_raw_nvs_backup(metadata_path)
        except core.FirmwareUpdateError:
            continue
        if (
            existing.device_identity_sha256 == device_identity_sha256
            and existing.offset == offset
            and existing.size == size
            and existing.data.sha256 == digest
        ):
            reusable_data = existing.data
            if (
                existing.selected_firmware_sha256 == selected_firmware_sha256
                and existing.source_project_version == source_project_version
                and existing.partition_table_sha256 == partition_table_sha256
                and existing.purpose == purpose
            ):
                return existing
    prefix = "settings-snapshot"
    data_file = reusable_data or write_private_file(
        directory,
        prefix=f"{prefix}-{device_identity_sha256[:12]}",
        suffix=".bin",
        data=data,
    )
    document: dict[str, Any] = {
        "schema": 1,
        "kind": f"osr_updater_{purpose}",
        "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "selected_firmware_sha256": selected_firmware_sha256,
        "device_identity_sha256": device_identity_sha256,
        "source_project_version": source_project_version,
        "partition_table_sha256": partition_table_sha256,
        "offset": offset,
        "size": size,
        "data_file": data_file.path.name,
        "data_sha256": data_file.sha256,
    }
    raw = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    metadata = write_private_file(
        directory,
        prefix=f"{prefix}-metadata-{device_identity_sha256[:12]}",
        suffix=".json",
        data=raw,
    )
    return RawNvsBackup(
        data=data_file,
        metadata=metadata,
        purpose=purpose,
        device_identity_sha256=device_identity_sha256,
        selected_firmware_sha256=selected_firmware_sha256,
        source_project_version=source_project_version,
        partition_table_sha256=partition_table_sha256,
        offset=offset,
        size=size,
        captured_at=document["captured_at"],
    )


def load_raw_nvs_backup(
    metadata_path: Path,
    *,
    portable_read: bool = False,
) -> RawNvsBackup:
    metadata_path = metadata_path.expanduser().absolute()
    raw = read_private_file(metadata_path, portable_read=portable_read)
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise core.AuditError("vehicle-settings backup metadata is invalid") from None
    purpose = str(document.get("kind", "")).removeprefix("osr_updater_")
    identity = document.get("device_identity_sha256")
    selected_sha = document.get("selected_firmware_sha256")
    data_name = document.get("data_file")
    offset = document.get("offset")
    size = document.get("size")
    data_sha = document.get("data_sha256")
    partition_table_sha = document.get("partition_table_sha256")
    source_project_version = document.get("source_project_version")
    captured_at = document.get("captured_at")
    if (
        document.get("schema") != 1
        or purpose not in RAW_NVS_BACKUP_PURPOSES
        or not isinstance(identity, str)
        or not re_full_sha(identity)
        or not isinstance(selected_sha, str)
        or not re_full_sha(selected_sha)
        or not isinstance(data_name, str)
        or Path(data_name).name != data_name
        or not isinstance(offset, int)
        or offset < 0
        or not isinstance(size, int)
        or size <= 0
        or offset + size > 32 * 1024 * 1024
        or not isinstance(data_sha, str)
        or not re_full_sha(data_sha)
        or not isinstance(partition_table_sha, str)
        or not re_full_sha(partition_table_sha)
        or not isinstance(captured_at, str)
        or not captured_at
        or not (
            source_project_version is None
            or (
                isinstance(source_project_version, str)
                and 0 < len(source_project_version) <= 64
                and all(ord(character) >= 0x20 for character in source_project_version)
            )
        )
    ):
        raise core.AuditError("vehicle-settings backup metadata is invalid")
    data_path = metadata_path.parent / data_name
    data = read_private_file(
        data_path,
        expected_size=size,
        expected_sha256=data_sha,
        portable_read=portable_read,
    )
    return RawNvsBackup(
        data=StoredFile(data_path, len(data), data_sha),
        metadata=StoredFile(metadata_path, len(raw), hashlib.sha256(raw).hexdigest()),
        purpose=purpose,
        device_identity_sha256=identity,
        selected_firmware_sha256=selected_sha,
        source_project_version=source_project_version,
        partition_table_sha256=partition_table_sha,
        offset=offset,
        size=size,
        captured_at=captured_at,
    )


def list_raw_nvs_backups(
    directory: Path,
    *,
    device_identity_sha256: str | None = None,
    offset: int | None = None,
    size: int | None = None,
) -> tuple[RawNvsBackup, ...]:
    """Return one fixed verified baseline per controller and NVS layout."""

    directory = directory.expanduser().absolute()
    if not directory.is_dir() or directory.is_symlink():
        return ()
    grouped: dict[tuple[str, int, int], RawNvsBackup] = {}
    for metadata_path in directory.glob("settings-snapshot-metadata-*.json"):
        try:
            backup = load_raw_nvs_backup(metadata_path)
        except core.FirmwareUpdateError:
            continue
        if (
            device_identity_sha256 is not None
            and backup.device_identity_sha256 != device_identity_sha256
        ):
            continue
        if offset is not None and backup.offset != offset:
            continue
        if size is not None and backup.size != size:
            continue
        key = (
            backup.device_identity_sha256,
            backup.offset,
            backup.size,
        )
        current = grouped.get(key)
        if current is None or _factory_baseline_rank(backup) < _factory_baseline_rank(current):
            grouped[key] = backup
    return tuple(
        sorted(grouped.values(), key=lambda item: item.captured_at, reverse=True)
    )


def _factory_baseline_rank(backup: RawNvsBackup) -> tuple[int, int, str, str]:
    """Keep the earliest useful operation snapshot as the immutable baseline."""

    return (
        0 if backup.purpose == "operation_snapshot" else 1,
        0 if backup.source_project_version is not None else 1,
        backup.captured_at,
        str(backup.metadata.path),
    )


def re_full_sha(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
