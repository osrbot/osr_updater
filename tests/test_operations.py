from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from osr_updater import core
from osr_updater.operations import ConfirmationError, UpdaterService, UpdaterSettings
from osr_updater.storage import write_raw_nvs_backup
from tests.support import (
    NVS_OFFSET,
    NVS_SIZE,
    SOURCE_VERSION,
    TARGET_VERSION,
    FakeClock,
    FakeRomFactory,
    FakeRomSession,
    FakeSerial,
    SequenceFactory,
    application_image,
    managed_handler,
    recovery_image,
)


class UpdaterOperationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_dir = self.root / "state"
        self.app_path = self.root / "firmware-a.bin"
        self.recovery_path = self.root / "firmware-b.bin"
        self.app_path.write_bytes(application_image())
        self.recovery_bytes = recovery_image()
        self.recovery_path.write_bytes(self.recovery_bytes)
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _service(self, connections, *, rom_factory=None, events=None) -> UpdaterService:
        if rom_factory is None:
            settings = b"\x42" * NVS_SIZE
            rom_factory = FakeRomFactory(
                [
                    FakeRomSession(self.recovery_bytes, settings),
                ]
            )
        return UpdaterService(
            settings=UpdaterSettings(
                port="/dev/test-controller",
                response_timeout=0.05,
                reconnect_timeout=0.5,
                state_dir=self.state_dir,
            ),
            serial_factory=SequenceFactory(connections),
            rom_factory=rom_factory,
            event_sink=events.append if events is not None else (lambda _event: None),
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )

    def test_application_update_backs_up_then_installs_and_verifies(self):
        writes: list[str] = []
        events: list[dict] = []
        pre = FakeSerial(managed_handler(SOURCE_VERSION), writes)
        post = FakeSerial(managed_handler(TARGET_VERSION), writes)
        class RomMustNotOpen:
            def open(self, *_args, **_kwargs):
                raise AssertionError("App OTA must not open ESP32-S3 ROM mode")

        service = self._service(
            [pre, post],
            rom_factory=RomMustNotOpen(),
            events=events,
        )

        result = service.install_application(
            self.app_path,
            confirmation="INSTALL FIRMWARE",
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.operation, "application_update")
        self.assertEqual(result.post_project_version, TARGET_VERSION)
        self.assertIsNotNone(result.logical_backup)
        self.assertIsNone(result.raw_nvs_backup)
        assert result.logical_backup is not None
        self.assertTrue(result.logical_backup.path.is_file())
        self.assertEqual(stat.S_IMODE(result.logical_backup.path.stat().st_mode), 0o600)
        self.assertLess(writes.index("config export"), next(i for i, value in enumerate(writes) if value.startswith("fw begin ")))
        self.assertTrue(any(value.startswith("fw data ") for value in writes))
        self.assertIn("fw end", writes)
        self.assertTrue(any(event.get("phase") == "flash_app" for event in events))

    def test_application_confirmation_refusal_never_starts_transfer(self):
        writes: list[str] = []
        service = self._service([FakeSerial(managed_handler(SOURCE_VERSION), writes)])

        with self.assertRaises(ConfirmationError):
            service.install_application(self.app_path, confirmation="NO")

        self.assertFalse(any(value.startswith("fw begin ") for value in writes))
        backups = list((self.state_dir / "backups").glob("*.json"))
        self.assertEqual(len(backups), 0)

    def test_explicit_begin_rejection_is_safe_to_retry(self):
        writes: list[str] = []
        settings = b"\x42" * NVS_SIZE
        service = self._service(
            [
                FakeSerial(managed_handler(SOURCE_VERSION, begin_error="low_voltage"), writes),
            ],
            rom_factory=FakeRomFactory([FakeRomSession(self.recovery_bytes, settings)]),
        )

        with self.assertRaises(core.DeviceRejectedError) as caught:
            service.install_application(
                self.app_path,
                confirmation="INSTALL FIRMWARE",
            )

        self.assertFalse(getattr(caught.exception, "no_app_reflash", False))
        self.assertFalse(any(value.startswith("fw data ") for value in writes))
        audit = caught.exception.audit_path.read_text(encoding="utf-8")
        self.assertIn('"retry_app_update":true', audit)

    def test_data_ack_uncertainty_forbids_another_application_write(self):
        writes: list[str] = []
        base_handler = managed_handler(SOURCE_VERSION)

        def uncertain_data(command: str):
            if command.startswith("fw data "):
                return []
            if command == "fw status" and any(
                value.startswith("fw data ") for value in writes
            ):
                return "FW: active=No written=0 size=0 next_seq=0 running=ota_0 next=ota_1"
            return base_handler(command)

        settings = b"\x42" * NVS_SIZE
        service = self._service(
            [FakeSerial(uncertain_data, writes)],
            rom_factory=FakeRomFactory([FakeRomSession(self.recovery_bytes, settings)]),
        )
        with self.assertRaises(core.ProtocolError) as caught:
            service.install_application(
                self.app_path,
                confirmation="INSTALL FIRMWARE",
            )

        self.assertTrue(getattr(caught.exception, "no_app_reflash", False))
        self.assertIn("fw abort", writes)
        self.assertFalse(any(value == "fw end" for value in writes))
        audit = caught.exception.audit_path.read_text(encoding="utf-8")
        self.assertIn('"retry_app_update":false', audit)

    def test_end_ack_loss_without_reconnect_is_completed_but_unverified(self):
        writes: list[str] = []
        base_handler = managed_handler(SOURCE_VERSION)

        def missing_end_ack(command: str):
            if command == "fw end":
                return []
            return base_handler(command)

        settings = b"\x42" * NVS_SIZE
        service = self._service(
            [
                FakeSerial(missing_end_ack, writes),
            ],
            rom_factory=FakeRomFactory([FakeRomSession(self.recovery_bytes, settings)]),
        )
        result = service.install_application(
            self.app_path,
            confirmation="INSTALL FIRMWARE",
        )

        self.assertEqual(result.status, "completed_unverified")
        self.assertFalse(result.retry_app_update)
        self.assertIn("were not verified", result.message)
        self.assertIn("fw end", writes)
        self.assertNotIn("fw abort", writes)
        audit = result.audit_path.read_text(encoding="utf-8")
        self.assertIn('"completed_unverified"', audit)

    def test_unknown_source_requires_full_flash_without_sending_app_data(self):
        writes: list[str] = []

        def unsupported(command: str):
            if command in {"v 0.00 0.00", "stream off"}:
                return []
            if command == "fw version":
                return "FW_VERSION: ProjectVer=CUSTOM, Proto=unknown"
            if command == "profile get":
                return "ERROR unknown_command"
            if command == "fw status":
                return "FW: active=No written=0 size=0 next_seq=0 running=ota_0 next=ota_1"
            if command == "b":
                return "b 12.1"
            return "ERROR unsupported"

        service = self._service([FakeSerial(unsupported, writes)])
        with self.assertRaises(core.FirmwareUpdateError) as caught:
            service.install_application(
                self.app_path,
                confirmation="INSTALL FIRMWARE",
            )
        self.assertFalse(any(value.startswith("fw data ") for value in writes))
        self.assertTrue(getattr(caught.exception, "full_flash_required", False))

    def test_application_logical_export_failure_still_uses_app_ota_without_rom(self):
        writes: list[str] = []

        class RomMustNotOpen:
            def open(self, *_args, **_kwargs):
                raise AssertionError("App OTA must not open ESP32-S3 ROM mode")

        service = self._service(
            [
                FakeSerial(managed_handler(SOURCE_VERSION, export_error=True), writes),
                FakeSerial(managed_handler(TARGET_VERSION, export_error=True), writes),
            ],
            rom_factory=RomMustNotOpen(),
        )

        result = service.install_application(
            self.app_path,
            confirmation="INSTALL FIRMWARE",
        )

        self.assertEqual(result.status, "success")
        self.assertIsNone(result.logical_backup)
        self.assertIsNone(result.raw_nvs_backup)

    def test_custom_application_parameter_change_is_diagnostic_not_a_write_failure(self):
        writes: list[str] = []
        settings = b"\x37" * NVS_SIZE
        changed_items = list(core.VEHICLE_CONFIG_FIELDS)
        original_export = managed_handler(TARGET_VERSION)
        target_calls = {"export": 0}

        def changed_export(command: str):
            response = original_export(command)
            if command != "config export" or not isinstance(response, list):
                return response
            target_calls["export"] += 1
            lines = list(response)
            index = next(
                i
                for i, line in enumerate(lines)
                if line.startswith("CONFIG_ITEM: Name=pid_params.kp,")
            )
            lines[index] = (
                "CONFIG_ITEM: Name=pid_params.kp, State=SET, Type=BLOB, "
                "Value=0000803f"
            )
            source = core.ConfigIdentity(TARGET_VERSION, "test", 1)
            items = []
            for name, value_type, _size in changed_items:
                line = next(line for line in lines if f"Name={name}," in line)
                state = line.split("State=", 1)[1].split(",", 1)[0]
                value = line.split("Value=", 1)[1]
                items.append(core.VehicleConfigItem(name, state, value_type, value))
            digest = core.calculate_vehicle_config_sha256(
                source.project_version,
                source.profile_id,
                source.nvs_schema,
                tuple(items),
            )
            lines[3] = f"CONFIG_EXPORT_HASH: BackupSHA={digest}"
            lines[-1] = (
                f"CONFIG_EXPORT_END: Result=OK, Items=24, BackupSHA={digest}, Reason=ok"
            )
            return lines

        service = self._service(
            [
                FakeSerial(managed_handler(SOURCE_VERSION), writes),
                FakeSerial(changed_export, writes),
            ],
            rom_factory=FakeRomFactory(
                [FakeRomSession(self.recovery_bytes, settings)]
            ),
        )

        result = service.install_application(
            self.app_path,
            confirmation="INSTALL FIRMWARE",
        )

        self.assertEqual(result.status, "completed_unverified")
        self.assertIn("available_but_differs", result.post_verification)
        self.assertFalse(result.retry_app_update)

    def test_full_flash_has_two_gates_and_restores_complete_settings(self):
        writes: list[str] = []
        events: list[dict] = []
        settings = bytes(range(256)) * (NVS_SIZE // 256)
        prepare_rom = FakeRomSession(self.recovery_bytes, settings)
        execute_rom = FakeRomSession(self.recovery_bytes, settings)
        pre = FakeSerial(managed_handler(SOURCE_VERSION), writes)
        post = FakeSerial(managed_handler(TARGET_VERSION), writes)
        rom_factory = FakeRomFactory([prepare_rom, execute_rom])
        service = self._service(
            [pre, post],
            rom_factory=rom_factory,
            events=events,
        )

        preparation = service.prepare_recovery(
            self.recovery_path,
            confirmation="PREPARE RECOVERY",
        )
        self.assertFalse(prepare_rom.erased)
        self.assertFalse(prepare_rom.reset)
        assert preparation.raw_nvs_backup is not None
        self.assertEqual(stat.S_IMODE(preparation.raw_nvs_backup.data.path.stat().st_mode), 0o600)
        self.assertEqual(preparation.raw_nvs_backup.data.size, NVS_SIZE)
        self.assertIn("settings-snapshot-", preparation.raw_nvs_backup.data.path.name)

        with self.assertRaises(ConfirmationError):
            service.execute_recovery(
                preparation.preparation_id,
                acknowledge_other_data_loss=False,
            )
        self.assertFalse(execute_rom.erased)

        result = service.execute_recovery(
            preparation.preparation_id,
            acknowledge_other_data_loss=True,
        )
        self.assertEqual(result.status, "success")
        self.assertTrue(execute_rom.erased)
        self.assertFalse(execute_rom.reset)
        self.assertEqual([item[0] for item in execute_rom.writes], [0, NVS_OFFSET])
        self.assertEqual(execute_rom.settings, settings)
        self.assertIsNone(result.post_project_version)
        self.assertIn("restart_required", result.post_verification)
        self.assertEqual(len(service.serial_factory.calls), 1)
        self.assertEqual(rom_factory.calls[1][2], prepare_rom.port_hint)
        progress = [event["progress"] for event in events if "progress" in event]
        self.assertTrue(any(0.45 < value < 0.85 for value in progress))
        self.assertEqual(progress[-1], 1.0)
        self.assertTrue(
            any(
                event.get("phase") == "manual_reset"
                and event.get("status") == "required"
                and "RESET" in event.get("message", "")
                and "BOOT" not in event.get("message", "")
                for event in events
            )
        )
        self.assertFalse(any(event.get("phase") == "reconnect" for event in events))
        self.assertTrue(
            any(
                event.get("details", {}).get("rom_port") == "/dev/ttyACM-test"
                for event in events
            )
        )

    def test_controller_recovery_combines_selected_firmware_and_saved_settings(self):
        current_settings = b"\x72" * NVS_SIZE
        selected_settings = b"\x31" * NVS_SIZE
        selected = write_raw_nvs_backup(
            selected_settings,
            directory=self.state_dir / "recovery-settings",
            selected_firmware_sha256="b" * 64,
            device_identity_sha256="a" * 64,
            source_project_version="OSR-PRODUCTION",
            partition_table_sha256=hashlib.sha256(
                self.recovery_bytes[0x8000:0x8C00]
            ).hexdigest(),
            offset=NVS_OFFSET,
            size=NVS_SIZE,
        )
        prepare_rom = FakeRomSession(self.recovery_bytes, current_settings)
        execute_rom = FakeRomSession(self.recovery_bytes, current_settings)
        service = self._service(
            [
                FakeSerial(managed_handler(SOURCE_VERSION), []),
                FakeSerial(managed_handler(TARGET_VERSION), []),
            ],
            rom_factory=FakeRomFactory([prepare_rom, execute_rom]),
        )

        preparation = service.prepare_recovery(
            self.recovery_path,
            confirmation="PREPARE RECOVERY",
            restore_metadata_path=selected.metadata.path,
        )

        self.assertEqual(preparation.restore_source, "selected_backup")
        self.assertEqual(preparation.restore_backup, selected)
        self.assertNotEqual(preparation.raw_nvs_backup.data.sha256, selected.data.sha256)
        result = service.execute_recovery(
            preparation.preparation_id,
            acknowledge_other_data_loss=True,
        )
        self.assertEqual(result.status, "success")
        self.assertEqual(execute_rom.settings, selected_settings)
        self.assertEqual([item[0] for item in execute_rom.writes], [0, NVS_OFFSET])

    def test_backup_list_keeps_fixed_earliest_factory_baseline(self):
        directory = self.state_dir / "recovery-settings"
        factory = write_raw_nvs_backup(
            b"\x21" * NVS_SIZE,
            directory=directory,
            selected_firmware_sha256="b" * 64,
            device_identity_sha256="a" * 64,
            source_project_version="OSR-KNOWN",
            partition_table_sha256="c" * 64,
            offset=NVS_OFFSET,
            size=NVS_SIZE,
        )
        factory_document = json.loads(factory.metadata.path.read_text(encoding="utf-8"))
        factory_document["captured_at"] = "2026-08-01T00:00:00+00:00"
        factory.metadata.path.write_text(
            json.dumps(factory_document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        later = write_raw_nvs_backup(
            b"\x23" * NVS_SIZE,
            directory=directory,
            selected_firmware_sha256="e" * 64,
            device_identity_sha256="a" * 64,
            source_project_version="OSR-LATER",
            partition_table_sha256="c" * 64,
            offset=NVS_OFFSET,
            size=NVS_SIZE,
        )
        later_document = json.loads(later.metadata.path.read_text(encoding="utf-8"))
        later_document["captured_at"] = "2026-08-02T00:00:00+00:00"
        later.metadata.path.write_text(
            json.dumps(later_document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        write_raw_nvs_backup(
            b"\x22" * NVS_SIZE,
            directory=directory,
            selected_firmware_sha256="d" * 64,
            device_identity_sha256="a" * 64,
            source_project_version=None,
            partition_table_sha256="c" * 64,
            offset=NVS_OFFSET,
            size=NVS_SIZE,
            purpose="restore_safety_snapshot",
        )

        choices = self._service([]).list_vehicle_settings_backups()

        self.assertEqual(len(choices), 1)
        recommended = [choice for choice in choices if choice.recommended]
        self.assertEqual(len(recommended), 1)
        self.assertEqual(recommended[0].source_project_version, "OSR-KNOWN")
        self.assertEqual(
            recommended[0].metadata_path.resolve(), factory.metadata.path.resolve()
        )
        filtered = self._service([]).list_vehicle_settings_backups(
            offset=NVS_OFFSET,
            size=NVS_SIZE,
        )
        self.assertEqual(filtered, choices)

    def test_recovery_refuses_different_device_before_erase(self):
        writes: list[str] = []
        settings = b"\x42" * NVS_SIZE
        prepare_rom = FakeRomSession(self.recovery_bytes, settings, identity="a" * 64)
        execute_rom = FakeRomSession(self.recovery_bytes, settings, identity="b" * 64)
        service = self._service(
            [FakeSerial(managed_handler(SOURCE_VERSION), writes)],
            rom_factory=FakeRomFactory([prepare_rom, execute_rom]),
        )
        preparation = service.prepare_recovery(
            self.recovery_path,
            confirmation="PREPARE RECOVERY",
        )

        with self.assertRaises(core.DevicePreflightError):
            service.execute_recovery(
                preparation.preparation_id,
                acknowledge_other_data_loss=True,
            )
        self.assertFalse(execute_rom.erased)

    def test_full_flash_refuses_partition_layout_drift_before_erase(self):
        settings = b"\x42" * NVS_SIZE
        prepare_rom = FakeRomSession(self.recovery_bytes, settings)
        changed_layout = bytearray(self.recovery_bytes)
        changed_layout[0x8000:0x8020] = b"\xff" * 32
        execute_rom = FakeRomSession(bytes(changed_layout), settings)
        service = self._service(
            [FakeSerial(managed_handler(SOURCE_VERSION), [])],
            rom_factory=FakeRomFactory([prepare_rom, execute_rom]),
        )
        preparation = service.prepare_recovery(
            self.recovery_path,
            confirmation="PREPARE RECOVERY",
        )

        with self.assertRaises(core.DevicePreflightError):
            service.execute_recovery(
                preparation.preparation_id,
                acknowledge_other_data_loss=True,
            )

        self.assertFalse(execute_rom.erased)

    def test_recovery_refuses_tampered_backup_before_rom_reconnect(self):
        writes: list[str] = []
        settings = b"\x42" * NVS_SIZE
        prepare_rom = FakeRomSession(self.recovery_bytes, settings)
        execute_factory = FakeRomFactory([prepare_rom])
        service = self._service(
            [FakeSerial(managed_handler(SOURCE_VERSION), writes)],
            rom_factory=execute_factory,
        )
        preparation = service.prepare_recovery(
            self.recovery_path,
            confirmation="PREPARE RECOVERY",
        )
        assert preparation.raw_nvs_backup is not None
        os.chmod(preparation.raw_nvs_backup.data.path, 0o600)
        preparation.raw_nvs_backup.data.path.write_bytes(b"\x00" * NVS_SIZE)

        with self.assertRaises(core.AuditError):
            service.execute_recovery(
                preparation.preparation_id,
                acknowledge_other_data_loss=True,
            )
        self.assertEqual(len(execute_factory.sessions), 0)

    def test_recovery_readback_failure_retains_backup_and_requires_physical_recovery(self):
        writes: list[str] = []
        settings = bytes(range(256)) * (NVS_SIZE // 256)
        prepare_rom = FakeRomSession(self.recovery_bytes, settings)
        execute_rom = FakeRomSession(
            self.recovery_bytes,
            settings,
            corrupt_readback=True,
        )
        service = self._service(
            [FakeSerial(managed_handler(SOURCE_VERSION), writes)],
            rom_factory=FakeRomFactory([prepare_rom, execute_rom]),
        )
        preparation = service.prepare_recovery(
            self.recovery_path,
            confirmation="PREPARE RECOVERY",
        )
        assert preparation.raw_nvs_backup is not None
        backup_path = preparation.raw_nvs_backup.data.path
        backup_sha = preparation.raw_nvs_backup.data.sha256

        with self.assertRaises(core.PostInstallError) as caught:
            service.execute_recovery(
                preparation.preparation_id,
                acknowledge_other_data_loss=True,
            )

        self.assertTrue(execute_rom.erased)
        self.assertFalse(execute_rom.reset)
        self.assertTrue(getattr(caught.exception, "physical_recovery_required", False))
        self.assertTrue(backup_path.is_file())
        self.assertEqual(
            hashlib.sha256(backup_path.read_bytes()).hexdigest(),
            backup_sha,
        )
        audit = caught.exception.audit_path.read_text(encoding="utf-8")
        self.assertIn('"physical_recovery_required":true', audit)
        self.assertIn(str(backup_path), audit)
        self.assertIn(str(preparation.raw_nvs_backup.metadata.path), audit)

    def test_custom_full_flash_layout_keeps_backup_without_automatic_restore(self):
        custom_bytes = bytearray(self.recovery_bytes)
        custom_bytes[0x8000:0x8020] = b"\xff" * 32
        custom_path = self.root / "custom-full.bin"
        custom_path.write_bytes(custom_bytes)
        writes: list[str] = []
        settings = b"\x53" * NVS_SIZE
        prepare_rom = FakeRomSession(self.recovery_bytes, settings)
        execute_rom = FakeRomSession(self.recovery_bytes, settings)
        service = self._service(
            [FakeSerial(managed_handler(SOURCE_VERSION), writes)],
            rom_factory=FakeRomFactory([prepare_rom, execute_rom]),
        )

        preparation = service.prepare_recovery(
            custom_path,
            confirmation="PREPARE RECOVERY",
        )
        self.assertIsNone(preparation.recovery_image.nvs_offset)
        self.assertIsNone(preparation.restore_backup)
        result = service.execute_recovery(
            preparation.preparation_id,
            acknowledge_other_data_loss=True,
        )

        self.assertEqual(result.status, "success")
        self.assertEqual([item[0] for item in execute_rom.writes], [0])
        self.assertIn("settings_backup_retained_only", result.post_verification)
        self.assertIn("restart_required", result.post_verification)

    def test_full_flash_restores_when_nvs_layout_matches_even_if_other_partitions_change(self):
        target_bytes = bytearray(self.recovery_bytes)
        target_bytes[0x802C:0x803C] = b"custom\0" + b"\0" * 9
        target_path = self.root / "changed-app-layout.bin"
        target_path.write_bytes(target_bytes)
        settings = b"\x67" * NVS_SIZE
        prepare_rom = FakeRomSession(self.recovery_bytes, settings)
        execute_rom = FakeRomSession(self.recovery_bytes, settings)
        service = self._service(
            [FakeSerial(managed_handler(SOURCE_VERSION), [])],
            rom_factory=FakeRomFactory([prepare_rom, execute_rom]),
        )

        preparation = service.prepare_recovery(
            target_path,
            confirmation="PREPARE RECOVERY",
        )

        self.assertIsNotNone(preparation.restore_backup)
        service.execute_recovery(
            preparation.preparation_id,
            acknowledge_other_data_loss=True,
        )
        self.assertEqual([item[0] for item in execute_rom.writes], [0, NVS_OFFSET])
        self.assertEqual(execute_rom.settings, settings)

    def test_unknown_current_nvs_layout_refuses_full_flash_before_erase(self):
        custom_current = bytearray(self.recovery_bytes)
        custom_current[0x8000:0x8020] = b"\xff" * 32
        prepare_rom = FakeRomSession(bytes(custom_current), b"\xff" * NVS_SIZE)
        service = self._service(
            [FakeSerial(managed_handler(SOURCE_VERSION), [])],
            rom_factory=FakeRomFactory([prepare_rom]),
        )

        with self.assertRaises(core.DevicePreflightError):
            service.prepare_recovery(
                self.recovery_path,
                confirmation="PREPARE RECOVERY",
            )
        self.assertFalse(prepare_rom.erased)
        self.assertTrue(prepare_rom.reset)

    def test_vehicle_settings_restore_saves_current_state_and_verifies_write(self):
        selected_settings = b"\x31" * NVS_SIZE
        current_settings = b"\x72" * NVS_SIZE
        selected = write_raw_nvs_backup(
            selected_settings,
            directory=self.state_dir / "portable-backup",
            selected_firmware_sha256="b" * 64,
            device_identity_sha256="a" * 64,
            source_project_version=SOURCE_VERSION,
            partition_table_sha256=hashlib.sha256(
                self.recovery_bytes[0x8000:0x8C00]
            ).hexdigest(),
            offset=NVS_OFFSET,
            size=NVS_SIZE,
        )
        rom = FakeRomSession(self.recovery_bytes, current_settings)
        events: list[dict] = []
        service = self._service(
            [],
            rom_factory=FakeRomFactory([rom]),
            events=events,
        )

        result = service.restore_vehicle_settings(
            selected.metadata.path,
            confirmation="RESTORE VEHICLE SETTINGS",
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.operation, "vehicle_settings_restore")
        self.assertEqual([item[0] for item in rom.writes], [NVS_OFFSET])
        self.assertEqual(rom.settings, selected_settings)
        self.assertTrue(rom.reset)
        self.assertIsNotNone(result.raw_nvs_backup)
        assert result.raw_nvs_backup is not None
        self.assertEqual(result.raw_nvs_backup.purpose, "restore_safety_snapshot")
        self.assertEqual(result.raw_nvs_backup.data.path.read_bytes(), current_settings)
        progress = [event["progress"] for event in events if "progress" in event]
        self.assertTrue(any(0.32 < value < 0.80 for value in progress))
        self.assertEqual(progress[-1], 1.0)

    def test_vehicle_settings_restore_rejects_wrong_controller_and_layout(self):
        selected_settings = b"\x31" * NVS_SIZE
        selected = write_raw_nvs_backup(
            selected_settings,
            directory=self.state_dir / "portable-backup",
            selected_firmware_sha256="b" * 64,
            device_identity_sha256="a" * 64,
            source_project_version=SOURCE_VERSION,
            partition_table_sha256="c" * 64,
            offset=NVS_OFFSET,
            size=NVS_SIZE,
        )
        wrong_controller = FakeRomSession(
            self.recovery_bytes,
            b"\x72" * NVS_SIZE,
            identity="d" * 64,
        )
        wrong_controller_service = self._service(
            [],
            rom_factory=FakeRomFactory([wrong_controller]),
        )
        with self.assertRaises(core.DevicePreflightError):
            wrong_controller_service.restore_vehicle_settings(
                selected.metadata.path,
                confirmation="RESTORE VEHICLE SETTINGS",
            )
        self.assertEqual(wrong_controller.writes, [])

        different_layout = write_raw_nvs_backup(
            selected_settings,
            directory=self.state_dir / "other-layout",
            selected_firmware_sha256="b" * 64,
            device_identity_sha256="a" * 64,
            source_project_version=SOURCE_VERSION,
            partition_table_sha256="c" * 64,
            offset=NVS_OFFSET + 0x1000,
            size=NVS_SIZE,
        )
        layout_rom = FakeRomSession(self.recovery_bytes, b"\x72" * NVS_SIZE)
        layout_service = self._service(
            [],
            rom_factory=FakeRomFactory([layout_rom]),
        )
        with self.assertRaises(core.DevicePreflightError):
            layout_service.restore_vehicle_settings(
                different_layout.metadata.path,
                confirmation="RESTORE VEHICLE SETTINGS",
            )
        self.assertEqual(layout_rom.writes, [])

    def test_vehicle_settings_restore_readback_failure_keeps_both_backups(self):
        selected_settings = b"\x31" * NVS_SIZE
        current_settings = b"\x72" * NVS_SIZE
        selected = write_raw_nvs_backup(
            selected_settings,
            directory=self.state_dir / "portable-backup",
            selected_firmware_sha256="b" * 64,
            device_identity_sha256="a" * 64,
            source_project_version=SOURCE_VERSION,
            partition_table_sha256="c" * 64,
            offset=NVS_OFFSET,
            size=NVS_SIZE,
        )
        rom = FakeRomSession(
            self.recovery_bytes,
            current_settings,
            corrupt_readback=True,
        )
        service = self._service([], rom_factory=FakeRomFactory([rom]))

        with self.assertRaises(core.PostInstallError) as caught:
            service.restore_vehicle_settings(
                selected.metadata.path,
                confirmation="RESTORE VEHICLE SETTINGS",
            )

        self.assertTrue(getattr(caught.exception, "physical_recovery_required", False))
        self.assertTrue(selected.metadata.path.is_file())
        safety_metadata = list(
            (self.state_dir / "recovery-settings").glob(
                "settings-snapshot-metadata-*.json"
            )
        )
        self.assertEqual(len(safety_metadata), 1)
        self.assertFalse(rom.reset)

    def test_audit_omits_vehicle_parameter_values(self):
        writes: list[str] = []
        service = self._service(
            [
                FakeSerial(managed_handler(SOURCE_VERSION), writes),
                FakeSerial(managed_handler(TARGET_VERSION), writes),
            ],
            rom_factory=FakeRomFactory(
                [FakeRomSession(self.recovery_bytes, b"\x42" * NVS_SIZE)]
            ),
        )
        result = service.install_application(
            self.app_path,
            confirmation="INSTALL FIRMWARE",
        )
        audit = result.audit_path.read_text(encoding="utf-8")
        self.assertNotIn("CONFIG_ITEM", audit)
        for line in audit.splitlines():
            json.loads(line)


if __name__ == "__main__":
    unittest.main()
