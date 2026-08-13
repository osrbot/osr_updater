"""Firmware-file classification shared by the native interface and tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import core
from .images import ApplicationImage, ImageValidationError, validate_application_file
from .recovery import RecoveryImage, RecoveryImageError, validate_recovery_file


@dataclass(frozen=True)
class FirmwareSelection:
    path: Path
    kind: str
    size: int
    sha256: str
    image: ApplicationImage | RecoveryImage

    def safe_summary(self) -> dict[str, Any]:
        return {
            "file": self.path.name,
            "kind": self.kind,
            "bytes": self.size,
            "sha256": self.sha256,
        }


def inspect_firmware_file(path: Path) -> FirmwareSelection:
    absolute = path.expanduser().absolute()
    try:
        application = validate_application_file(absolute)
    except ImageValidationError as application_error:
        try:
            recovery = validate_recovery_file(absolute)
        except RecoveryImageError as recovery_error:
            raise core.PackageValidationError(
                "the selected file is neither a supported ESP32-S3 application "
                f"nor a full-flash image ({application_error}; {recovery_error})"
            ) from None
        return FirmwareSelection(
            absolute,
            "recovery",
            recovery.size,
            recovery.sha256,
            recovery,
        )
    return FirmwareSelection(
        absolute,
        "application",
        application.size,
        application.sha256,
        application,
    )
