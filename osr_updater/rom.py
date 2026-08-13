"""Narrow, testable esptool adapter for full-flash installation."""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from . import core


class RomOperationError(core.FirmwareUpdateError):
    exit_code = 5


ESPRESSIF_USB_VID = 0x303A
ESP32S3_USB_SERIAL_JTAG_PID = 0x1001
ROM_PORT_WAIT_SECONDS = 20.0


@dataclass(frozen=True)
class RomPortHint:
    """Bind a re-enumerated ROM port to the controller's USB connection."""

    device: str | None = None
    usb_location: str | None = None
    serial_number: str | None = None


def _physical_usb_location(location: str | None) -> str | None:
    if not location:
        return None
    return location.split(":", 1)[0]


def _serial_ports() -> tuple[Any, ...]:
    try:
        from serial.tools import list_ports

        return tuple(list_ports.comports())
    except Exception:
        return ()


def preferred_controller_port(
    current: str,
    ports: tuple[Any, ...] | None = None,
) -> str:
    """Choose the stable controller alias or one unambiguous Espressif port."""

    alias = "/dev/osrbot_base"
    available_ports = _serial_ports() if ports is None else ports
    devices = {str(item.device) for item in available_ports}
    if current and current != alias and (
        current in devices or os.path.exists(current)
    ):
        return current
    if os.path.exists(alias):
        return alias
    rom_ports = [
        str(item.device)
        for item in available_ports
        if item.vid == ESPRESSIF_USB_VID
        and item.pid == ESP32S3_USB_SERIAL_JTAG_PID
    ]
    if len(rom_ports) == 1:
        return rom_ports[0]
    espressif_ports = [
        str(item.device)
        for item in available_ports
        if item.vid == ESPRESSIF_USB_VID
    ]
    if len(espressif_ports) == 1:
        return espressif_ports[0]
    return current or alias


def capture_rom_port_hint(port: str) -> RomPortHint:
    """Capture USB topology before BOOT mode changes the tty device name."""

    resolved = os.path.realpath(port)
    for item in _serial_ports():
        if item.device == port or os.path.realpath(item.device) == resolved:
            return RomPortHint(
                device=item.device,
                usb_location=_physical_usb_location(item.location),
                serial_number=item.serial_number,
            )
    return RomPortHint(device=resolved if os.path.exists(resolved) else None)


def rom_port_candidates(port: str, hint: RomPortHint | None) -> tuple[str, ...]:
    """Return only ports that can safely represent the same ESP32-S3."""

    ports = _serial_ports()
    candidates: list[str] = []

    def add(candidate: str | None) -> None:
        if candidate and candidate not in candidates and os.path.exists(candidate):
            candidates.append(candidate)

    if hint is not None and hint.usb_location is not None:
        for item in ports:
            if _physical_usb_location(item.location) == hint.usb_location:
                add(item.device)
        return tuple(candidates)

    if hint is not None and hint.serial_number:
        for item in ports:
            if item.serial_number == hint.serial_number:
                add(item.device)
        if candidates:
            return tuple(candidates)

    add(port)
    if hint is not None:
        add(hint.device)
    rom_ports = [
        item.device
        for item in ports
        if item.vid == ESPRESSIF_USB_VID
        and item.pid == ESP32S3_USB_SERIAL_JTAG_PID
    ]
    if len(rom_ports) == 1:
        add(rom_ports[0])
    return tuple(candidates)


@dataclass(frozen=True)
class RomSecurityInfo:
    chip: str
    flash_size: int
    device_identity_sha256: str
    secure_boot: bool
    secure_download: bool
    flash_encryption: bool

    def validate_supported(
        self,
        *,
        expected_flash_size: int | None = None,
        minimum_flash_size: int | None = None,
    ) -> None:
        if self.chip != "ESP32-S3":
            raise RomOperationError("ROM target is not ESP32-S3")
        if expected_flash_size is not None and self.flash_size != expected_flash_size:
            raise RomOperationError("device flash size does not match the full-flash image")
        if minimum_flash_size is not None and self.flash_size < minimum_flash_size:
            raise RomOperationError("selected full-flash image does not fit the connected flash")
        if self.secure_boot or self.secure_download or self.flash_encryption:
            raise RomOperationError(
                "device security configuration is not supported by full-flash installation"
            )


class RomSession(Protocol):
    security: RomSecurityInfo
    port_hint: RomPortHint

    def read_flash(self, offset: int, size: int) -> bytes: ...

    def erase_flash(self) -> None: ...

    def write_flash(
        self,
        offset: int,
        data: bytes,
        *,
        flash_size: int,
        progress: Callable[[int, int], None] | None = None,
    ) -> None: ...

    def hard_reset(self) -> None: ...

    def close(self) -> None: ...


class RomFactory(Protocol):
    def open(
        self,
        port: str,
        *,
        baud: int,
        hint: RomPortHint | None = None,
    ) -> RomSession: ...


class EsptoolRomSession:
    def __init__(
        self,
        rom: Any,
        stub: Any,
        security: RomSecurityInfo,
        commands: Any,
        port_hint: RomPortHint,
    ):
        self._rom = rom
        self._stub = stub
        self._commands = commands
        self.security = security
        self.port_hint = port_hint
        self._closed = False

    def _call(self, label: str, function: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except Exception:
            raise RomOperationError(f"ROM operation failed during {label}") from None

    def read_flash(self, offset: int, size: int) -> bytes:
        data = self._call(
            "flash read",
            self._commands.read_flash,
            self._stub,
            offset,
            size,
            output=None,
            flash_size="keep",
            no_progress=True,
        )
        if not isinstance(data, bytes) or len(data) != size:
            raise RomOperationError("ROM flash read returned an incomplete result")
        return data

    def erase_flash(self) -> None:
        self._call("full erase", self._commands.erase_flash, self._stub, force=False)

    def write_flash(
        self,
        offset: int,
        data: bytes,
        *,
        flash_size: int,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        if not data or offset < 0 or offset + len(data) > flash_size:
            raise RomOperationError("ROM flash write range is invalid")
        flash_size_name = f"{flash_size // (1024 * 1024)}MB"
        logger = getattr(self._commands, "log", None)
        original_progress = getattr(logger, "progress_bar", None)
        report_progress = progress is not None and callable(original_progress)
        if progress is not None:
            progress(0, len(data))

        if report_progress:
            def progress_bar(
                cur_iter: int,
                total_iters: int,
                **_details: Any,
            ) -> None:
                progress(cur_iter, total_iters)

            logger.progress_bar = progress_bar
        try:
            self._call(
                "flash write",
                self._commands.write_flash,
                self._stub,
                [(offset, data)],
                flash_freq="keep",
                flash_mode="keep",
                flash_size=flash_size_name,
                compress=True,
                no_progress=not report_progress,
                force=False,
                encrypt=False,
            )
        finally:
            if report_progress:
                logger.progress_bar = original_progress
        if progress is not None:
            progress(len(data), len(data))

    def hard_reset(self) -> None:
        self._call("hard reset", self._stub.hard_reset)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for candidate in (self._stub, self._rom):
            port = getattr(candidate, "_port", None)
            if port is None:
                continue
            try:
                port.close()
            except Exception:
                pass


class EsptoolRomFactory:
    """Open one ESP32-S3 ROM session and retain it through erase and restore."""

    def open(
        self,
        port: str,
        *,
        baud: int,
        hint: RomPortHint | None = None,
    ) -> EsptoolRomSession:
        try:
            import esptool
            from esptool import cmds
            from esptool.util import flash_size_bytes
        except ImportError:
            raise RomOperationError("esptool is required for full-flash installation") from None

        deadline = time.monotonic() + ROM_PORT_WAIT_SECONDS
        saw_candidate = False
        while True:
            for candidate in rom_port_candidates(port, hint):
                saw_candidate = True
                rom = None
                stub = None
                try:
                    rom = esptool.get_default_connected_device(
                        [candidate],
                        candidate,
                        connect_attempts=1,
                        initial_baud=baud,
                        chip="esp32s3",
                        trace=False,
                        before="no-reset",
                    )
                    if rom is None:
                        raise RuntimeError("no ROM device")
                    raw_security = rom.get_security_info(cache=False)
                    flags = raw_security.get("parsed_flags", {})
                    flash_encryption = bool(rom.get_flash_encryption_enabled())
                    mac = rom.read_mac()
                    description = str(rom.get_chip_description())
                    stub = rom.run_stub()
                    detected_size = cmds.detect_flash_size(stub)
                    if not isinstance(detected_size, str):
                        raise RuntimeError("flash size unavailable")
                    flash_size = flash_size_bytes(detected_size)
                    if not isinstance(mac, tuple) or len(mac) != 6:
                        raise RuntimeError("device identity unavailable")
                    identity = hashlib.sha256(bytes(mac)).hexdigest()
                    chip = (
                        "ESP32-S3"
                        if "ESP32-S3" in description.upper()
                        else description
                    )
                    security = RomSecurityInfo(
                        chip=chip,
                        flash_size=flash_size,
                        device_identity_sha256=identity,
                        secure_boot=bool(flags.get("SECURE_BOOT_EN")),
                        secure_download=bool(flags.get("SECURE_DOWNLOAD_ENABLE")),
                        flash_encryption=flash_encryption,
                    )
                    connected_hint = capture_rom_port_hint(candidate)
                    if connected_hint.usb_location is None and hint is not None:
                        connected_hint = RomPortHint(
                            device=candidate,
                            usb_location=hint.usb_location,
                            serial_number=hint.serial_number,
                        )
                    return EsptoolRomSession(
                        rom,
                        stub,
                        security,
                        cmds,
                        connected_hint,
                    )
                except Exception:
                    for loader in (stub, rom):
                        if loader is None:
                            continue
                        try:
                            loader._port.close()
                        except Exception:
                            pass
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        if saw_candidate:
            raise RomOperationError(
                "ESP32-S3 serial port was found, but the ROM handshake failed; "
                "hold BOOT, press and release RESET, then release BOOT and retry"
            )
        raise RomOperationError(
            "could not find the re-enumerated ESP32-S3 ROM serial port; "
            "keep USB connected, use BOOT/RESET, and retry"
        )
