from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from osr_updater import core
from osr_updater.images import ImageValidationError, validate_application_bytes
from osr_updater.recovery import RecoveryImageError, validate_recovery_bytes
from osr_updater.selection import inspect_firmware_file

from tests.support import NVS_OFFSET, NVS_SIZE, application_image, recovery_image


class FirmwareImageValidationTest(unittest.TestCase):
    def test_application_and_recovery_images_are_classified_without_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application_path = root / "firmware-a.bin"
            recovery_path = root / "firmware-b.bin"
            application_path.write_bytes(application_image())
            recovery_path.write_bytes(recovery_image())

            application = inspect_firmware_file(application_path)
            recovery = inspect_firmware_file(recovery_path)

            self.assertEqual(application.kind, "application")
            self.assertEqual(recovery.kind, "recovery")
            self.assertEqual(application.path, application_path)
            self.assertEqual(recovery.path, recovery_path)
            self.assertEqual(len(application.sha256), 64)
            self.assertEqual(len(recovery.sha256), 64)

    def test_application_parser_checks_chip_checksum_and_digest(self):
        valid = application_image()
        self.assertEqual(validate_application_bytes(valid).version, "OSR-TEST-TARGET")

        wrong_chip = bytearray(valid)
        wrong_chip[12:14] = (1).to_bytes(2, "little")
        bad_digest = bytearray(valid)
        bad_digest[-1] ^= 0xFF
        for candidate in (bytes(wrong_chip), bytes(bad_digest)):
            with self.subTest(candidate=len(candidate)):
                with self.assertRaises(ImageValidationError):
                    validate_application_bytes(candidate)

    def test_full_flash_parser_accepts_custom_layout_and_reports_standard_settings(self):
        valid = validate_recovery_bytes(recovery_image())
        self.assertEqual((valid.nvs_offset, valid.nvs_size), (NVS_OFFSET, NVS_SIZE))
        self.assertGreater(valid.minimum_flash_size, valid.size)

        missing_table = bytearray(recovery_image())
        missing_table[0x8000:0x8020] = b"\xff" * 32
        wrong_chip = bytearray(recovery_image())
        wrong_chip[12:14] = (2).to_bytes(2, "little")
        bad_bootloader = bytearray(recovery_image())
        bad_bootloader[32] ^= 0xFF
        custom = validate_recovery_bytes(bytes(missing_table))
        self.assertEqual(custom.partitions, ())
        self.assertIsNone(custom.nvs_offset)
        self.assertIsNone(custom.nvs_size)
        with self.assertRaises(RecoveryImageError):
            validate_recovery_bytes(bytes(wrong_chip))
        with self.assertRaises(RecoveryImageError):
            validate_recovery_bytes(bytes(bad_bootloader))

    def test_unknown_and_symlink_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unknown = root / "unknown.bin"
            unknown.write_bytes(b"not firmware")
            with self.assertRaises(core.PackageValidationError):
                inspect_firmware_file(unknown)

            target = root / "target.bin"
            target.write_bytes(application_image())
            link = root / "link.bin"
            link.symlink_to(target)
            with self.assertRaises(core.PackageValidationError):
                inspect_firmware_file(link)


if __name__ == "__main__":
    unittest.main()
