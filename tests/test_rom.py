from __future__ import annotations

import unittest
import sys
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

from osr_updater.rom import (
    EsptoolRomSession,
    EsptoolRomFactory,
    RomPortHint,
    RomSecurityInfo,
    capture_rom_port_hint,
    preferred_controller_port,
    rom_port_candidates,
)


def serial_port(
    device: str,
    *,
    location: str,
    serial_number: str,
    pid: int,
):
    return SimpleNamespace(
        device=device,
        vid=0x303A,
        pid=pid,
        location=location,
        serial_number=serial_number,
    )


class RomPortDiscoveryTest(unittest.TestCase):
    def test_preferred_port_uses_alias_or_unique_esp32s3(self):
        ports = (
            serial_port(
                "/dev/ttyACM2",
                location="1-2.2.4:1.0",
                serial_number="controller",
                pid=0x1001,
            ),
        )
        with patch("osr_updater.rom.os.path.exists", return_value=False):
            self.assertEqual(
                preferred_controller_port("/dev/osrbot_base", ports),
                "/dev/ttyACM2",
            )
        with patch(
            "osr_updater.rom.os.path.exists",
            side_effect=lambda value: value == "/dev/osrbot_base",
        ):
            self.assertEqual(
                preferred_controller_port("/dev/ttyACM2", ports),
                "/dev/ttyACM2",
            )

    def test_write_flash_reports_esptool_progress_and_restores_logger(self):
        calls = []
        original_progress = lambda **_details: None
        logger = SimpleNamespace(progress_bar=original_progress)

        def write_flash(_stub, _items, **kwargs):
            calls.append(kwargs)
            logger.progress_bar(cur_iter=25, total_iters=100)
            logger.progress_bar(cur_iter=100, total_iters=100)

        commands = SimpleNamespace(log=logger, write_flash=write_flash)
        session = EsptoolRomSession(
            SimpleNamespace(),
            SimpleNamespace(),
            RomSecurityInfo("ESP32-S3", 1024, "a" * 64, False, False, False),
            commands,
            RomPortHint(),
        )
        progress = []
        session.write_flash(
            0,
            b"x" * 100,
            flash_size=1024,
            progress=lambda written, total: progress.append((written, total)),
        )
        self.assertEqual(progress, [(0, 100), (25, 100), (100, 100), (100, 100)])
        self.assertFalse(calls[0]["no_progress"])
        self.assertIs(logger.progress_bar, original_progress)

    def test_manual_boot_connection_never_resets_before_rom_handshake(self):
        calls = []

        class Port:
            def close(self):
                pass

        class Loader:
            _port = Port()

            def get_security_info(self, *, cache):
                return {"parsed_flags": {}}

            def get_flash_encryption_enabled(self):
                return False

            def read_mac(self):
                return (0x3C, 0x0F, 0x02, 0xC9, 0x95, 0xDC)

            def get_chip_description(self):
                return "ESP32-S3"

            def run_stub(self):
                return stub

        stub = SimpleNamespace(_port=Port())

        esptool = ModuleType("esptool")

        def connect(serial_list, port, **kwargs):
            calls.append((serial_list, port, kwargs))
            return Loader()

        esptool.get_default_connected_device = connect
        commands = ModuleType("esptool.cmds")
        commands.detect_flash_size = lambda loader: (
            "16MB" if loader is stub else None
        )
        esptool.cmds = commands
        util = ModuleType("esptool.util")
        util.flash_size_bytes = lambda _value: 16 * 1024 * 1024

        with (
            patch.dict(
                sys.modules,
                {
                    "esptool": esptool,
                    "esptool.cmds": commands,
                    "esptool.util": util,
                },
            ),
            patch(
                "osr_updater.rom.rom_port_candidates",
                return_value=("/dev/ttyACM7",),
            ),
            patch(
                "osr_updater.rom.capture_rom_port_hint",
                return_value=RomPortHint(
                    device="/dev/ttyACM7",
                    usb_location="1-2.2.4",
                    serial_number="controller",
                ),
            ),
        ):
            session = EsptoolRomFactory().open(
                "/dev/osrbot_base",
                baud=460800,
                hint=RomPortHint(usb_location="1-2.2.4"),
            )
        self.assertEqual(calls[0][1], "/dev/ttyACM7")
        self.assertEqual(calls[0][2]["before"], "no-reset")
        session.close()

    def test_reenumerated_port_is_selected_by_physical_usb_location(self):
        ports = (
            serial_port(
                "/dev/ttyACM0",
                location="1-2.1:1.2",
                serial_number="other-device",
                pid=0x4001,
            ),
            serial_port(
                "/dev/ttyACM7",
                location="1-2.2.4:1.0",
                serial_number="controller",
                pid=0x1001,
            ),
        )
        hint = RomPortHint(
            device="/dev/ttyACM2",
            usb_location="1-2.2.4",
            serial_number="controller",
        )
        with (
            patch("osr_updater.rom._serial_ports", return_value=ports),
            patch("osr_updater.rom.os.path.exists", return_value=True),
        ):
            candidates = rom_port_candidates("/dev/osrbot_base", hint)
        self.assertEqual(candidates, ("/dev/ttyACM7",))

    def test_unique_rom_port_is_used_when_alias_is_already_gone(self):
        ports = (
            serial_port(
                "/dev/ttyACM0",
                location="1-2.1:1.2",
                serial_number="other-device",
                pid=0x4001,
            ),
            serial_port(
                "/dev/ttyACM7",
                location="1-2.2.4:1.0",
                serial_number="controller",
                pid=0x1001,
            ),
        )
        with (
            patch("osr_updater.rom._serial_ports", return_value=ports),
            patch(
                "osr_updater.rom.os.path.exists",
                side_effect=lambda value: value == "/dev/ttyACM7",
            ),
        ):
            candidates = rom_port_candidates(
                "/dev/osrbot_base",
                RomPortHint(),
            )
        self.assertEqual(candidates, ("/dev/ttyACM7",))

    def test_multiple_unbound_rom_ports_are_not_guessed(self):
        ports = (
            serial_port(
                "/dev/ttyACM4",
                location="1-2.2:1.0",
                serial_number="controller-a",
                pid=0x1001,
            ),
            serial_port(
                "/dev/ttyACM7",
                location="1-2.4:1.0",
                serial_number="controller-b",
                pid=0x1001,
            ),
        )
        with (
            patch("osr_updater.rom._serial_ports", return_value=ports),
            patch("osr_updater.rom.os.path.exists", return_value=False),
        ):
            candidates = rom_port_candidates(
                "/dev/osrbot_base",
                RomPortHint(),
            )
        self.assertEqual(candidates, ())

    def test_running_port_hint_keeps_usb_topology_not_tty_number(self):
        ports = (
            serial_port(
                "/dev/ttyACM2",
                location="1-2.2.4:1.0",
                serial_number="controller",
                pid=0x1001,
            ),
        )
        with (
            patch("osr_updater.rom._serial_ports", return_value=ports),
            patch(
                "osr_updater.rom.os.path.realpath",
                side_effect=lambda value: (
                    "/dev/ttyACM2" if value == "/dev/osrbot_base" else value
                ),
            ),
        ):
            hint = capture_rom_port_hint("/dev/osrbot_base")
        self.assertEqual(hint.device, "/dev/ttyACM2")
        self.assertEqual(hint.usb_location, "1-2.2.4")
        self.assertEqual(hint.serial_number, "controller")


if __name__ == "__main__":
    unittest.main()
