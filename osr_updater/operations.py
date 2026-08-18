"""UI-independent business operations for OSR Updater."""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from . import core
from .images import ApplicationImage, validate_application_file
from .recovery import (
    PARTITION_TABLE_OFFSET,
    PARTITION_TABLE_SIZE,
    RecoveryImage,
    find_nvs_partition,
    parse_current_partition_table,
    validate_recovery_file,
)
from .rom import (
    EsptoolRomFactory,
    RomFactory,
    RomPortHint,
    RomSecurityInfo,
    capture_rom_port_hint,
)
from .storage import (
    RawNvsBackup,
    default_state_directory,
    list_raw_nvs_backups,
    load_raw_nvs_backup,
    read_private_file,
    write_raw_nvs_backup,
)


EventSink = Callable[[dict[str, Any]], None]


class OperationBusyError(core.FirmwareUpdateError):
    exit_code = 3


class ConfirmationError(core.UserCancelledError):
    pass


@dataclass(frozen=True)
class UpdaterSettings:
    port: str = core.DEFAULT_PORT
    baud: int = core.DEFAULT_BAUD
    chunk_size: int = core.DEFAULT_CHUNK_SIZE
    response_timeout: float = core.DEFAULT_RESPONSE_TIMEOUT
    reconnect_timeout: float = core.DEFAULT_RECONNECT_TIMEOUT
    state_dir: Path = field(default_factory=default_state_directory)

    def update_config(self) -> core.UpdateConfig:
        config = core.UpdateConfig(
            port=self.port,
            baud=self.baud,
            chunk_size=self.chunk_size,
            response_timeout=self.response_timeout,
            reconnect_timeout=self.reconnect_timeout,
            log_dir=self.audit_dir,
            snapshot_dir=self.backup_dir,
        )
        config.validate()
        return config

    @property
    def audit_dir(self) -> Path:
        return self.state_dir.expanduser() / "audit"

    @property
    def backup_dir(self) -> Path:
        return self.state_dir.expanduser() / "backups"

    @property
    def raw_nvs_dir(self) -> Path:
        return self.state_dir.expanduser() / "recovery-settings"

@dataclass(frozen=True)
class DeviceInspection:
    project_version: str
    protocol: str | None
    profile_id: str | None
    nvs_schema: int | None
    profile_state: str | None
    motion_ok: bool | None
    writes_ok: bool | None
    voltage: float
    firmware_status: core.FirmwareStatus
    backup_capability: str

    def safe_summary(self) -> dict[str, Any]:
        return {
            "project_version": self.project_version,
            "protocol": self.protocol,
            "profile_id": self.profile_id,
            "nvs_schema": self.nvs_schema,
            "profile_state": self.profile_state,
            "motion_ok": self.motion_ok,
            "writes_ok": self.writes_ok,
            "battery_voltage": self.voltage,
            "ota_session_active": self.firmware_status.active,
            "backup_capability": self.backup_capability,
        }


@dataclass(frozen=True)
class LogicalBackup:
    kind: str
    path: Path
    file_sha256: str
    reference: core.VehicleConfigExport | dict[str, Any]

    def safe_summary(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": str(self.path),
            "file_sha256": self.file_sha256,
        }


@dataclass(frozen=True)
class OperationResult:
    status: str
    operation: str
    audit_path: Path
    message: str
    firmware_file: str | None = None
    firmware_sha256: str | None = None
    app_sha256: str | None = None
    logical_backup: LogicalBackup | None = None
    raw_nvs_backup: RawNvsBackup | None = None
    post_project_version: str | None = None
    post_verification: str = "not_run"
    retry_app_update: bool | None = None

    def safe_summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation": self.operation,
            "message": self.message,
            "firmware_file": self.firmware_file,
            "firmware_sha256": self.firmware_sha256,
            "app_sha256": self.app_sha256,
            "audit_path": str(self.audit_path),
            "logical_backup": (
                None if self.logical_backup is None else self.logical_backup.safe_summary()
            ),
            "raw_nvs_backup": (
                None
                if self.raw_nvs_backup is None
                else {
                    "path": str(self.raw_nvs_backup.data.path),
                    "sha256": self.raw_nvs_backup.data.sha256,
                    "offset": self.raw_nvs_backup.offset,
                    "size": self.raw_nvs_backup.size,
                    "metadata_path": str(self.raw_nvs_backup.metadata.path),
                    "purpose": self.raw_nvs_backup.purpose,
                }
            ),
            "post_project_version": self.post_project_version,
            "post_verification": self.post_verification,
            "retry_app_update": self.retry_app_update,
        }


@dataclass(frozen=True)
class ErasePreparation:
    preparation_id: str
    recovery_image: RecoveryImage
    created_monotonic: float
    raw_nvs_backup: RawNvsBackup
    restore_backup: RawNvsBackup | None
    restore_source: str
    logical_backup: LogicalBackup | None
    source_project_version: str | None
    security: RomSecurityInfo
    rom_port_hint: RomPortHint
    prepared_partition_table_sha256: str
    audit_path: Path

    def safe_summary(self) -> dict[str, Any]:
        return {
            "preparation_id": self.preparation_id,
            "firmware_file": self.recovery_image.path.name,
            "firmware_sha256": self.recovery_image.sha256,
            "logical_backup": (
                None if self.logical_backup is None else self.logical_backup.safe_summary()
            ),
            "raw_nvs_backup": {
                "path": str(self.raw_nvs_backup.data.path),
                "sha256": self.raw_nvs_backup.data.sha256,
                "offset": self.raw_nvs_backup.offset,
                "size": self.raw_nvs_backup.size,
                "metadata_path": str(self.raw_nvs_backup.metadata.path),
            },
            "settings_restore": self.restore_source,
            "current_settings_snapshot": "stored_and_verified",
            "non_settings_data": "will_be_erased",
            "audit_path": str(self.audit_path),
        }


@dataclass(frozen=True)
class CapturedSettings:
    operation_snapshot: RawNvsBackup
    security: RomSecurityInfo
    rom_port_hint: RomPortHint
    partition_table_sha256: str


@dataclass(frozen=True)
class BackupChoice:
    metadata_path: Path
    captured_at: str
    source_project_version: str | None
    purpose: str
    size: int
    controller_id: str
    recommended: bool

    def safe_summary(self) -> dict[str, Any]:
        return {
            "metadata_path": str(self.metadata_path),
            "captured_at": self.captured_at,
            "source_project_version": self.source_project_version,
            "purpose": self.purpose,
            "size": self.size,
            "controller_id": self.controller_id,
            "recommended": self.recommended,
        }


class UpdaterService:
    def __init__(
        self,
        *,
        settings: UpdaterSettings | None = None,
        serial_factory: core.SerialFactory = core.default_serial_factory,
        rom_factory: RomFactory | None = None,
        event_sink: EventSink | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings or UpdaterSettings()
        self.serial_factory = serial_factory
        self.rom_factory = rom_factory or EsptoolRomFactory()
        self.event_sink = event_sink or (lambda _event: None)
        self.monotonic = monotonic
        self.sleep = sleep
        self._lock = threading.Lock()
        self._preparations: dict[str, ErasePreparation] = {}
        self._last_progress_percent: int | None = None

    def list_vehicle_settings_backups(
        self,
        *,
        offset: int | None = None,
        size: int | None = None,
    ) -> tuple[BackupChoice, ...]:
        """List verified backups without opening a controller or writing flash."""

        backups = list_raw_nvs_backups(
            self.settings.raw_nvs_dir,
            offset=offset,
            size=size,
        )
        recommended = next(
            (
                item.metadata.path
                for item in backups
                if item.purpose == "operation_snapshot"
                and item.source_project_version is not None
            ),
            backups[0].metadata.path if backups else None,
        )
        return tuple(
            BackupChoice(
                metadata_path=backup.metadata.path,
                captured_at=backup.captured_at,
                source_project_version=backup.source_project_version,
                purpose=backup.purpose,
                size=backup.size,
                controller_id=backup.device_identity_sha256[:12],
                recommended=backup.metadata.path == recommended,
            )
            for backup in backups
        )

    def _capture_current_settings(
        self,
        *,
        selected_firmware_sha256: str,
        source_project_version: str | None,
        rom_port_hint: RomPortHint,
        audit: core.AuditLogger,
    ) -> CapturedSettings:
        self._event(
            "rom",
            "waiting",
            "Enter ESP32-S3 ROM download mode with BOOT/RESET if automatic reset does not connect",
            progress=0.05,
            indeterminate=True,
        )
        session = self.rom_factory.open(
            self.settings.port,
            baud=self.settings.baud,
            hint=rom_port_hint,
        )
        try:
            session.security.validate_supported()
            self._event(
                "rom",
                "started",
                "Connected to ESP32-S3 ROM download mode",
                progress=0.08,
                indeterminate=True,
                rom_port=session.port_hint.device,
            )
            current_table = session.read_flash(PARTITION_TABLE_OFFSET, PARTITION_TABLE_SIZE)
            partition_table_sha256 = hashlib.sha256(current_table).hexdigest()
            try:
                current_nvs = find_nvs_partition(parse_current_partition_table(current_table))
            except core.FirmwareUpdateError:
                raise core.DevicePreflightError(
                    "the current NVS partition could not be identified; full-flash installation was not started"
                ) from None
            self._event(
                "raw_settings",
                "started",
                "Reading the complete vehicle-settings partition",
                progress=0.10,
            )
            raw = session.read_flash(current_nvs.offset, current_nvs.size)
            snapshot = write_raw_nvs_backup(
                raw,
                directory=self.settings.raw_nvs_dir,
                selected_firmware_sha256=selected_firmware_sha256,
                device_identity_sha256=session.security.device_identity_sha256,
                source_project_version=source_project_version,
                partition_table_sha256=partition_table_sha256,
                offset=current_nvs.offset,
                size=current_nvs.size,
                purpose="operation_snapshot",
            )
            audit.event(
                "operation_snapshot",
                "stored",
                path=str(snapshot.data.path),
                metadata_path=str(snapshot.metadata.path),
                sha256=snapshot.data.sha256,
                offset=snapshot.offset,
                size=snapshot.size,
            )
            self._event(
                "raw_settings",
                "completed",
                "Complete vehicle-settings snapshot stored and verified",
                progress=0.20,
                operation_snapshot_path=str(snapshot.data.path),
                operation_snapshot_metadata_path=str(snapshot.metadata.path),
                sha256=snapshot.data.sha256,
            )
            captured = CapturedSettings(
                snapshot,
                session.security,
                session.port_hint,
                partition_table_sha256,
            )
        except BaseException:
            try:
                session.hard_reset()
            except Exception:
                pass
            raise
        else:
            return captured
        finally:
            session.close()

    def _event(
        self,
        phase: str,
        status: str,
        message: str,
        *,
        progress: float | None = None,
        **details: Any,
    ) -> None:
        event: dict[str, Any] = {
            "phase": phase,
            "status": status,
            "message": message,
            "timestamp": time.time(),
        }
        if progress is not None:
            event["progress"] = max(0.0, min(1.0, float(progress)))
        if details:
            event["details"] = details
        self.event_sink(event)

    @contextmanager
    def _exclusive_operation(self) -> Iterator[None]:
        if not self._lock.acquire(blocking=False):
            raise OperationBusyError("another firmware operation is already active")
        try:
            yield
        finally:
            self._lock.release()

    def _open_serial(self, config: core.UpdateConfig) -> core.SerialConnection:
        return core._open_exclusive(config, self.serial_factory)

    def _inspect_connection(
        self,
        connection: core.SerialConnection,
        config: core.UpdateConfig,
        audit: core.AuditLogger,
        *,
        phase: str,
        reconnect_deadline: float | None = None,
    ) -> DeviceInspection:
        transport = core.SerialTransport(connection, monotonic=self.monotonic, sleep=self.sleep)
        transport.send_line("v 0.00 0.00")
        transport.send_line("stream off")
        transport.sleep(0.05)
        core._drain_safe_stop(transport, audit, phase=phase)
        version = core._query_value(
            transport,
            config,
            command="fw version",
            label="fw version",
            prefix="FW_VERSION:",
            parser=core.parse_firmware_version,
        )
        profile: core.ProfileStatus | None = None
        try:
            profile = core._query_value(
                transport,
                config,
                command="profile get",
                label="profile",
                prefix="PROFILE:",
                parser=core.parse_profile_status,
            )
        except (core.ResponseTimeoutError, core.DeviceRejectedError):
            profile = None
        firmware_status = core._query_fw_status(transport, config)
        if phase == "post":
            voltage = core._query_post_battery(
                transport,
                config,
                audit,
                reconnect_deadline=reconnect_deadline,
            )
        else:
            transport.send_line("b")
            voltage = transport.wait_for(
                label="battery voltage",
                prefixes=("b ",),
                parser=core.parse_battery_voltage,
                timeout=config.response_timeout,
            )
        capability = self._backup_capability(version, profile)
        inspection = DeviceInspection(
            project_version=version.project_version,
            protocol=version.protocol,
            profile_id=None if profile is None else profile.profile_id,
            nvs_schema=None if profile is None else profile.nvs_schema,
            profile_state=None if profile is None else profile.state,
            motion_ok=None if profile is None else profile.motion_ok,
            writes_ok=None if profile is None else profile.writes_ok,
            voltage=voltage,
            firmware_status=firmware_status,
            backup_capability=capability,
        )
        audit.event("device_inspection", "ok", phase=phase, **inspection.safe_summary())
        return inspection

    @staticmethod
    def _backup_capability(
        version: core.FirmwareVersion,
        profile: core.ProfileStatus | None,
    ) -> str:
        if (
            version.protocol == core.SUPPORTED_PROTOCOL
            and profile is not None
            and profile.nvs_schema == 1
        ):
            return "managed"
        if version.protocol != core.SUPPORTED_PROTOCOL and profile is None:
            return "legacy"
        return "unavailable"

    def inspect(self) -> DeviceInspection:
        with self._exclusive_operation():
            config = self.settings.update_config()
            audit = core.AuditLogger(config.log_dir)
            connection = None
            try:
                self._event("inspect", "started", "Inspecting device")
                connection = self._open_serial(config)
                result = self._inspect_connection(connection, config, audit, phase="inspect")
                audit.event("result", "success", operation="inspect")
                self._event("inspect", "completed", "Device inspection completed")
                return result
            except core.FirmwareUpdateError as error:
                audit.event("result", "failed", operation="inspect", reason=str(error))
                error.audit_path = audit.path
                self._event("inspect", "failed", str(error))
                raise
            except Exception:
                error = core.ProtocolError("unexpected local updater error during device inspection")
                error.audit_path = audit.path
                audit.event("result", "failed", operation="inspect", reason=str(error))
                self._event("inspect", "failed", str(error))
                raise error from None
            finally:
                core._close_quietly(connection)
                audit.close()

    def _create_logical_backup(
        self,
        connection: core.SerialConnection,
        inspection: DeviceInspection,
        config: core.UpdateConfig,
        audit: core.AuditLogger,
        *,
        release: core.ReleasePackage | None,
    ) -> LogicalBackup | None:
        transport = core.SerialTransport(connection, monotonic=self.monotonic, sleep=self.sleep)
        if inspection.backup_capability == "managed":
            if inspection.profile_state == "READY":
                core._wait_for_level_calibration(
                    transport,
                    config,
                    audit,
                    reconnect_deadline=None,
                )
            exported = core._receive_vehicle_config_export_when_ready(transport, config)
            if (
                exported.source.project_version != inspection.project_version
                or exported.source.profile_id != inspection.profile_id
                or exported.source.nvs_schema != inspection.nvs_schema
            ):
                raise core.DevicePreflightError(
                    "configuration export source identity does not match the inspected device"
                )
            path, file_sha = core._write_vehicle_config_backup(
                exported,
                release,
                self.settings.backup_dir,
                audit_path=audit.path,
            )
            backup = LogicalBackup("managed", path, file_sha, exported)
        elif inspection.backup_capability == "legacy":
            try:
                configuration = core._query_configuration(transport, config)
            except core.FirmwareUpdateError as error:
                audit.event(
                    "logical_backup",
                    "unavailable",
                    reason=type(error).__name__,
                )
                self._event(
                    "backup",
                    "unavailable",
                    "A readable vehicle-settings backup is unavailable",
                )
                return None
            snapshot = core.DeviceSnapshot(
                version=core.FirmwareVersion(
                    inspection.project_version,
                    inspection.protocol,
                ),
                profile=None,
                voltage=inspection.voltage,
                firmware_status=inspection.firmware_status,
                configuration=configuration,
                unavailable_fields=("managed_config_export",),
            )
            file_sha, path = core._write_snapshot(snapshot, self.settings.backup_dir)
            backup = LogicalBackup("legacy", path, file_sha, configuration)
        else:
            audit.event("logical_backup", "unavailable")
            self._event(
                "backup",
                "unavailable",
                "A readable vehicle-settings backup is unavailable",
            )
            return None
        audit.event(
            "logical_backup",
            "stored",
            kind=backup.kind,
            path=str(backup.path),
            file_sha256=backup.file_sha256,
        )
        self._event(
            "backup",
            "completed",
            "Vehicle-settings backup stored",
            kind=backup.kind,
            path=str(backup.path),
            sha256=backup.file_sha256,
        )
        return backup

    def _verify_logical_backup(
        self,
        connection: core.SerialConnection,
        backup: LogicalBackup,
        config: core.UpdateConfig,
        audit: core.AuditLogger,
    ) -> str:
        transport = core.SerialTransport(connection, monotonic=self.monotonic, sleep=self.sleep)
        if backup.kind == "managed":
            expected = backup.reference
            if not isinstance(expected, core.VehicleConfigExport):
                raise core.ProtocolError("managed backup reference is invalid")
            current = core._receive_ready_vehicle_config_after_level_calibration(
                transport,
                config,
                audit,
                phase="post",
            )
            comparison = core.compare_vehicle_config_semantics(expected.items, current.items)
            core._audit_vehicle_config_comparison(audit, comparison, phase="post")
            if not comparison.matches:
                raise core.PostInstallError(
                    "post-update configuration differs from the persisted backup",
                    outcome="post_verification_pending",
                    stage="configuration_compare",
                )
            return (
                f"20 non-level items match; level init {comparison.level_init_status}; "
                f"level offsets {comparison.level_offset_status}"
            )
        expected_legacy = backup.reference
        if not isinstance(expected_legacy, dict):
            raise core.ProtocolError("legacy backup reference is invalid")
        current_legacy = core._query_configuration(transport, config)
        mismatches = core._configuration_mismatches(expected_legacy, current_legacy)
        audit.event(
            "legacy_config_compare",
            "ok" if not mismatches else "mismatch",
            mismatch_fields=mismatches,
            level_offsets_dynamic=core._level_offset_changed(expected_legacy, current_legacy),
        )
        if mismatches:
            raise core.PostInstallError(
                "post-update known vehicle parameters differ from the persisted backup",
                outcome="post_verification_pending",
                stage="legacy_configuration_compare",
            )
        return "known legacy parameters match; boot-time level offsets are dynamic"

    def _progress_callback(self, written: int, total: int) -> None:
        percent = 0 if total <= 0 else min(100, written * 100 // total)
        if percent == self._last_progress_percent:
            return
        self._last_progress_percent = percent
        self._event(
            "flash_app",
            "progress",
            "Flashing application",
            progress=0.0 if total <= 0 else written / total,
            written=written,
            total=total,
        )

    def _perform_app_transfer(
        self,
        connection: core.SerialConnection,
        release: core.ReleasePackage,
        config: core.UpdateConfig,
        audit: core.AuditLogger,
    ) -> core.OtaProgress:
        self._last_progress_percent = None
        progress = core.OtaProgress()
        abort_acknowledged = False
        try:
            core._perform_ota(
                connection,
                release,
                config,
                audit,
                progress,
                monotonic=self.monotonic,
                sleep=self.sleep,
                progress_func=self._progress_callback,
            )
            return progress
        except KeyboardInterrupt:
            if progress.session_active and not progress.end_may_have_been_sent:
                abort_acknowledged = core._best_effort_abort(
                    connection,
                    config,
                    audit,
                    monotonic=self.monotonic,
                    sleep=self.sleep,
                )
            error = core.UpdateInterruptedError("firmware operation interrupted by operator")
            error.no_app_reflash = not self._safe_app_retry(progress, abort_acknowledged)
            raise error from None
        except core.FirmwareUpdateError as error:
            if progress.session_active and not progress.end_may_have_been_sent:
                abort_acknowledged = core._best_effort_abort(
                    connection,
                    config,
                    audit,
                    monotonic=self.monotonic,
                    sleep=self.sleep,
                )
            error.no_app_reflash = not self._safe_app_retry(progress, abort_acknowledged)
            raise
        except Exception:
            if progress.session_active and not progress.end_may_have_been_sent:
                abort_acknowledged = core._best_effort_abort(
                    connection,
                    config,
                    audit,
                    monotonic=self.monotonic,
                    sleep=self.sleep,
                )
            error = core.ProtocolError("unexpected host error during App transfer")
            error.no_app_reflash = not self._safe_app_retry(progress, abort_acknowledged)
            raise error from None

    @staticmethod
    def _safe_app_retry(progress: core.OtaProgress, abort_acknowledged: bool) -> bool:
        if progress.begin_rejected:
            return True
        if not progress.begin_may_have_been_sent:
            return True
        if progress.end_may_have_been_sent or progress.data_delivery_unknown:
            return False
        return (
            progress.data_committed_bytes == 0
            and progress.session_active
            and abort_acknowledged
        )

    @staticmethod
    def _application_release(
        image: ApplicationImage,
        source: DeviceInspection,
    ) -> core.ReleasePackage:
        target = core.TargetProfile(
            profile_id=source.profile_id or "external",
            hardware="ESP32-S3",
            nvs_schema=source.nvs_schema or 1,
            project_version=image.version[:31],
            protocol=source.protocol or core.SUPPORTED_PROTOCOL,
        )
        return core.ReleasePackage(
            manifest_sha256=image.sha256,
            app_sha256=image.sha256,
            app_member="selected/application.bin",
            app_bytes=image.data,
            target=target,
            package_sha256=image.sha256,
            package_size=image.size,
        )

    def _wait_for_application_target(
        self,
        source_version: str,
        image: ApplicationImage,
        config: core.UpdateConfig,
        audit: core.AuditLogger,
    ) -> tuple[core.SerialConnection | None, DeviceInspection | None, str]:
        deadline = self.monotonic() + config.reconnect_timeout
        while self.monotonic() < deadline:
            connection = None
            try:
                connection = self._open_serial(config)
                inspection = self._inspect_connection(
                    connection,
                    config,
                    audit,
                    phase="post",
                    reconnect_deadline=deadline,
                )
                if inspection.project_version == image.version:
                    verification = "selected_identity_matched"
                elif inspection.project_version != source_version:
                    verification = "device_identity_changed"
                else:
                    verification = "source_identity_still_reported"
                return connection, inspection, verification
            except (
                core.SerialUnavailableError,
                core.ResponseTimeoutError,
                core.SerialCommunicationError,
            ):
                core._close_quietly(connection)
                remaining = deadline - self.monotonic()
                if remaining > 0:
                    self.sleep(min(config.reconnect_interval, remaining))
            except core.FirmwareUpdateError:
                core._close_quietly(connection)
                remaining = deadline - self.monotonic()
                if remaining > 0:
                    self.sleep(min(config.reconnect_interval, remaining))
        audit.event("application_post_probe", "unavailable")
        return None, None, "unavailable"

    def install_application(
        self,
        image_path: Path,
        *,
        confirmation: str,
    ) -> OperationResult:
        with self._exclusive_operation():
            config = self.settings.update_config()
            audit = core.AuditLogger(config.log_dir)
            connection = None
            backup: LogicalBackup | None = None
            app_delivery_completed = False
            try:
                self._event("validate", "started", "Validating selected ESP32-S3 application")
                image = validate_application_file(image_path.expanduser().absolute())
                if confirmation != "INSTALL FIRMWARE":
                    raise ConfirmationError(
                        "application update was not confirmed with INSTALL FIRMWARE"
                    )
                audit.event("application_image", "validated", **image.safe_summary())
                self._event(
                    "validate",
                    "completed",
                    "Selected application validated",
                    **image.safe_summary(),
                )
                connection = self._open_serial(config)
                try:
                    inspection = self._inspect_connection(
                        connection,
                        config,
                        audit,
                        phase="pre",
                    )
                except (core.DeviceRejectedError, core.ResponseTimeoutError) as error:
                    stage = getattr(error, "stage", "")
                    if stage in {"fw version", "fw status"} or any(
                        marker in str(error)
                        for marker in ("fw version", "fw status")
                    ):
                        error.full_flash_required = True
                    raise
                if inspection.firmware_status.active:
                    raise core.DevicePreflightError("an App OTA session is already active")
                try:
                    backup = self._create_logical_backup(
                        connection,
                        inspection,
                        config,
                        audit,
                        release=None,
                    )
                except core.FirmwareUpdateError as logical_error:
                    audit.event(
                        "logical_backup",
                        "unavailable",
                        reason=type(logical_error).__name__,
                    )
                    self._event(
                        "backup",
                        "unavailable",
                        "Readable settings export is unavailable; App OTA still preserves the NVS partition",
                    )
                    backup = None
                audit.event("confirmation", "ok", operation="application_update")
                self._event(
                    "flash_app",
                    "started",
                    "Starting App OTA; the NVS partition is not erased or rewritten",
                )
                try:
                    progress = self._perform_app_transfer(
                        connection,
                        self._application_release(image, inspection),
                        config,
                        audit,
                    )
                except core.DeviceRejectedError as error:
                    reason = error.device_reason.lower().replace("-", "_")
                    if error.stage == "fw begin" and any(
                        token in reason
                        for token in ("unknown", "unsupported", "not_supported", "unrecognized")
                    ):
                        error.full_flash_required = True
                    raise
                app_delivery_completed = True
                core._close_quietly(connection)
                connection = None
                connection, post, identity_verification = self._wait_for_application_target(
                    inspection.project_version,
                    image,
                    config,
                    audit,
                )
                post_version = None if post is None else post.project_version
                if connection is None:
                    identity_verification = "not_supported_by_running_firmware"
                runtime_verification = "not_available"
                runtime_ready = post is not None
                if post is not None and post.profile_state is not None:
                    runtime_ready = (
                        post.profile_state == "READY"
                        and post.motion_ok is True
                        and post.writes_ok is True
                    )
                    runtime_verification = (
                        "ready"
                        if runtime_ready
                        else f"profile_{post.profile_state.lower()}"
                    )
                elif post is not None:
                    runtime_verification = "profile_status_not_supported"
                settings_verification = "not_available"
                settings_match = True
                if connection is not None and backup is not None:
                    try:
                        settings_verification = self._verify_logical_backup(
                            connection,
                            backup,
                            config,
                            audit,
                        )
                    except core.FirmwareUpdateError as verification_error:
                        unsupported = isinstance(
                            verification_error,
                            (core.DeviceRejectedError, core.ResponseTimeoutError),
                        )
                        if not unsupported:
                            settings_verification = "available_but_differs"
                        else:
                            settings_verification = "not_supported_by_running_firmware"
                        settings_match = False
                        audit.event(
                            "logical_post_verification",
                            (
                                "diagnostic_mismatch"
                                if settings_verification == "available_but_differs"
                                else "unavailable"
                            ),
                            reason=type(verification_error).__name__,
                        )
                verification = (
                    f"{identity_verification}; {runtime_verification}; "
                    f"{settings_verification}"
                )
                verified = (
                    identity_verification == "selected_identity_matched"
                    and runtime_ready
                    and settings_match
                )
                if verified and backup is None:
                    success_message = (
                        "Application update completed; the NVS partition was preserved "
                        "and target startup was verified"
                    )
                else:
                    success_message = (
                        "Application update completed; NVS was preserved and available "
                        "settings checks passed"
                    )
                result = OperationResult(
                    "success" if verified else "completed_unverified",
                    "application_update",
                    audit.path,
                    (
                        success_message
                        if verified
                        else "Application transfer completed, but startup or optional settings checks were not verified; do not repeat the App write"
                    ),
                    firmware_file=image_path.name,
                    firmware_sha256=image.sha256,
                    app_sha256=image.sha256,
                    logical_backup=backup,
                    post_project_version=post_version,
                    post_verification=verification,
                    retry_app_update=False,
                )
                audit.event(
                    "result",
                    result.status,
                    end_acknowledged=progress.end_acknowledged,
                    result=result.safe_summary(),
                )
                self._event(
                    "result",
                    result.status,
                    result.message,
                    result=result.safe_summary(),
                )
                return result
            except core.FirmwareUpdateError as error:
                error.audit_path = audit.path
                if app_delivery_completed:
                    error.no_app_reflash = True
                retry = not bool(getattr(error, "no_app_reflash", False))
                audit.event(
                    "result",
                    "failed",
                    operation="application_update",
                    reason=str(error),
                    retry_app_update=retry,
                    backup_path=None if backup is None else str(backup.path),
                    full_flash_required=bool(getattr(error, "full_flash_required", False)),
                )
                self._event(
                    "result",
                    "failed",
                    str(error),
                    retry_app_update=retry,
                    backup_path=None if backup is None else str(backup.path),
                    full_flash_required=bool(getattr(error, "full_flash_required", False)),
                    audit_path=str(audit.path),
                )
                raise
            except Exception:
                error = core.ProtocolError("unexpected local updater error during application update")
                error.audit_path = audit.path
                error.no_app_reflash = app_delivery_completed
                audit.event(
                    "result",
                    "failed",
                    operation="application_update",
                    reason=str(error),
                    retry_app_update=not app_delivery_completed,
                )
                self._event(
                    "result",
                    "failed",
                    str(error),
                    audit_path=str(audit.path),
                )
                raise error from None
            finally:
                core._close_quietly(connection)
                audit.close()

    def prepare_recovery(
        self,
        image_path: Path,
        *,
        confirmation: str,
        restore_metadata_path: Path | None = None,
    ) -> ErasePreparation:
        with self._exclusive_operation():
            image = validate_recovery_file(image_path.expanduser().absolute())
            if confirmation != "PREPARE RECOVERY":
                raise ConfirmationError(
                    "full-flash preparation requires PREPARE RECOVERY"
                )
            config = self.settings.update_config()
            audit = core.AuditLogger(config.log_dir)
            connection = None
            logical: LogicalBackup | None = None
            source_version: str | None = None
            selected_restore: RawNvsBackup | None = None
            rom_port_hint = capture_rom_port_hint(self.settings.port)
            try:
                if restore_metadata_path is not None:
                    selected_restore = load_raw_nvs_backup(
                        restore_metadata_path.expanduser().absolute()
                    )
                audit.event(
                    "full_flash_prepare",
                    "started",
                    firmware_sha256=image.sha256,
                    firmware_bytes=image.size,
                )
                self._event(
                    "backup",
                    "started",
                    "Backing up vehicle settings",
                    progress=0.02,
                )
                try:
                    connection = self._open_serial(config)
                    inspection = self._inspect_connection(connection, config, audit, phase="pre")
                    source_version = inspection.project_version
                    logical = self._create_logical_backup(
                        connection,
                        inspection,
                        config,
                        audit,
                        release=None,
                    )
                except core.FirmwareUpdateError as logical_error:
                    audit.event(
                        "logical_backup",
                        "unavailable",
                        reason=type(logical_error).__name__,
                    )
                    self._event(
                        "backup",
                        "unavailable",
                        "Readable settings backup unavailable; complete settings backup remains mandatory",
                    )
                finally:
                    core._close_quietly(connection)
                    connection = None

                captured = self._capture_current_settings(
                    selected_firmware_sha256=image.sha256,
                    source_project_version=source_version,
                    rom_port_hint=rom_port_hint,
                    audit=audit,
                )
                snapshot = captured.operation_snapshot
                if selected_restore is not None:
                    if (
                        selected_restore.device_identity_sha256
                        != captured.security.device_identity_sha256
                    ):
                        raise core.DevicePreflightError(
                            "selected vehicle-settings backup belongs to a different controller"
                        )
                    if (
                        image.nvs_offset is None
                        or image.nvs_size is None
                        or (image.nvs_offset, image.nvs_size)
                        != (selected_restore.offset, selected_restore.size)
                    ):
                        raise core.DevicePreflightError(
                            "selected firmware and vehicle-settings backup use different settings layouts"
                        )
                    restore_backup = selected_restore
                    restore_plan = "selected_backup"
                else:
                    restore_compatible = (
                        image.nvs_offset is not None
                        and image.nvs_size is not None
                        and (image.nvs_offset, image.nvs_size)
                        == (snapshot.offset, snapshot.size)
                    )
                    restore_backup = snapshot if restore_compatible else None
                    restore_plan = (
                        "automatic_current_backup"
                        if restore_compatible
                        else "backup_only"
                    )
                audit.event(
                    "settings_restore_plan",
                    restore_plan,
                    selected_metadata=(
                        None
                        if selected_restore is None
                        else str(selected_restore.metadata.path)
                    ),
                    reason=(
                        "incompatible_partition_layout"
                        if restore_backup is None
                        else None
                    ),
                )
                preparation = ErasePreparation(
                    preparation_id=uuid.uuid4().hex,
                    recovery_image=image,
                    created_monotonic=self.monotonic(),
                    raw_nvs_backup=snapshot,
                    restore_backup=restore_backup,
                    restore_source=restore_plan,
                    logical_backup=logical,
                    source_project_version=source_version,
                    security=captured.security,
                    rom_port_hint=captured.rom_port_hint,
                    prepared_partition_table_sha256=captured.partition_table_sha256,
                    audit_path=audit.path,
                )
                self._preparations[preparation.preparation_id] = preparation
                audit.event("erase_prepare", "ready", **preparation.safe_summary())
                self._event(
                    "confirm_erase",
                    "ready",
                    "Complete vehicle settings are stored and verified; keep USB connected and remain in recovery mode for final confirmation",
                    progress=0.25,
                    **preparation.safe_summary(),
                )
                return preparation
            except core.FirmwareUpdateError as error:
                error.audit_path = audit.path
                operation_snapshot_path = getattr(error, "operation_snapshot_path", None)
                operation_snapshot_metadata_path = getattr(
                    error,
                    "operation_snapshot_metadata_path",
                    None,
                )
                audit.event(
                    "result",
                    "failed",
                    operation="full_flash_prepare",
                    reason=str(error),
                    operation_snapshot_path=operation_snapshot_path,
                    operation_snapshot_metadata_path=operation_snapshot_metadata_path,
                )
                self._event(
                    "result",
                    "failed",
                    str(error),
                    operation_snapshot_path=operation_snapshot_path,
                    operation_snapshot_metadata_path=operation_snapshot_metadata_path,
                    audit_path=str(audit.path),
                )
                raise
            except Exception:
                error = core.ProtocolError("unexpected local updater error during recovery preparation")
                error.audit_path = audit.path
                audit.event(
                    "result",
                    "failed",
                    operation="full_flash_prepare",
                    reason=str(error),
                )
                self._event("result", "failed", str(error), audit_path=str(audit.path))
                raise error from None
            finally:
                core._close_quietly(connection)
                audit.close()

    def execute_recovery(
        self,
        preparation_id: str,
        *,
        acknowledge_other_data_loss: bool,
    ) -> OperationResult:
        with self._exclusive_operation():
            preparation = self._preparations.get(preparation_id)
            if preparation is None:
                raise core.PackageValidationError(
                    "recovery preparation is unknown or already consumed"
                )
            if self.monotonic() - preparation.created_monotonic > 15 * 60:
                del self._preparations[preparation_id]
                raise core.PackageValidationError(
                    "recovery preparation expired; create a new vehicle-settings backup"
                )
            if not acknowledge_other_data_loss:
                raise ConfirmationError(
                    "full-flash installation requires data-loss acknowledgement"
                )
            del self._preparations[preparation_id]
            image = validate_recovery_file(preparation.recovery_image.path)
            if image.sha256 != preparation.recovery_image.sha256:
                raise core.PackageValidationError(
                    "selected full-flash image changed after settings backup"
                )
            config = self.settings.update_config()
            audit = core.AuditLogger(config.log_dir)
            session = None
            destructive_started = False
            try:
                raw_snapshot = preparation.raw_nvs_backup
                restore_backup = preparation.restore_backup
                verified_snapshot = load_raw_nvs_backup(raw_snapshot.metadata.path)
                if verified_snapshot != raw_snapshot:
                    raise core.AuditError(
                        "vehicle-settings snapshot changed after preparation"
                    )
                raw_snapshot = verified_snapshot
                audit.event(
                    "recovery_execute",
                    "started",
                    preparation_audit=str(preparation.audit_path),
                    firmware_sha256=image.sha256,
                    raw_nvs_path=str(raw_snapshot.data.path),
                    raw_nvs_sha256=raw_snapshot.data.sha256,
                    settings_restore=restore_backup is not None,
                )
                raw = None
                if restore_backup is not None:
                    verified_restore = load_raw_nvs_backup(restore_backup.metadata.path)
                    if verified_restore != restore_backup:
                        raise core.AuditError(
                            "vehicle-settings snapshot changed after preparation"
                        )
                    restore_backup = verified_restore
                    raw = read_private_file(
                        restore_backup.data.path,
                        expected_size=restore_backup.size,
                        expected_sha256=restore_backup.data.sha256,
                    )
                self._event(
                    "rom",
                    "started",
                    "Reconnecting to ESP32-S3 ROM download mode",
                    progress=0.28,
                    indeterminate=True,
                )
                session = self.rom_factory.open(
                    self.settings.port,
                    baud=self.settings.baud,
                    hint=preparation.rom_port_hint,
                )
                session.security.validate_supported(minimum_flash_size=image.minimum_flash_size)
                if (
                    session.security.device_identity_sha256
                    != preparation.security.device_identity_sha256
                ):
                    raise core.DevicePreflightError(
                        "connected controller is not the controller bound to the settings backup"
                    )
                current_table = session.read_flash(
                    PARTITION_TABLE_OFFSET,
                    PARTITION_TABLE_SIZE,
                )
                if (
                    hashlib.sha256(current_table).hexdigest()
                    != preparation.prepared_partition_table_sha256
                ):
                    raise core.DevicePreflightError(
                        "controller partition layout changed after preparation"
                    )
                if (
                    preparation.prepared_partition_table_sha256
                    != raw_snapshot.partition_table_sha256
                ):
                    raise core.DevicePreflightError(
                        "prepared settings snapshot is not bound to the current partition layout"
                    )
                current_raw = session.read_flash(raw_snapshot.offset, raw_snapshot.size)
                if hashlib.sha256(current_raw).hexdigest() != raw_snapshot.data.sha256:
                    raise core.DevicePreflightError(
                        "vehicle settings changed after preparation; create a new backup"
                    )
                self._event(
                    "rom",
                    "completed",
                    "Recovery controller and settings backup verified",
                    progress=0.34,
                    rom_port=session.port_hint.device,
                )
                self._event(
                    "erase",
                    "started",
                    "Erasing complete flash; data outside vehicle settings will be replaced",
                    progress=0.35,
                    indeterminate=True,
                )
                destructive_started = True
                session.erase_flash()
                audit.event("erase_flash", "completed")
                self._event(
                    "erase",
                    "completed",
                    "Complete flash erase finished",
                    progress=0.45,
                )
                self._event(
                    "recovery_flash",
                    "started",
                    "Writing selected full-flash image",
                    progress=0.45,
                )
                last_image_percent = -1

                def image_progress(written: int, total: int) -> None:
                    nonlocal last_image_percent
                    ratio = 0.0 if total <= 0 else min(1.0, written / total)
                    overall = 0.45 + ratio * 0.40
                    percent = int(overall * 100)
                    if percent == last_image_percent:
                        return
                    last_image_percent = percent
                    self._event(
                        "recovery_flash",
                        "progress",
                        "Writing selected full-flash image",
                        progress=overall,
                        written=written,
                        total=total,
                    )

                session.write_flash(
                    0,
                    image.data,
                    flash_size=session.security.flash_size,
                    progress=image_progress,
                )
                audit.event(
                    "recovery_image",
                    "written",
                    sha256=image.sha256,
                    bytes=image.size,
                )
                self._event(
                    "recovery_flash",
                    "completed",
                    "Selected full-flash image written",
                    progress=0.85,
                )
                settings_verification = "settings_backup_retained_only"
                if restore_backup is not None and raw is not None:
                    self._event(
                        "settings_restore",
                        "started",
                        "Restoring the saved vehicle settings",
                        progress=0.86,
                    )

                    def settings_progress(written: int, total: int) -> None:
                        ratio = 0.0 if total <= 0 else min(1.0, written / total)
                        self._event(
                            "settings_restore",
                            "progress",
                            "Restoring the saved vehicle settings",
                            progress=0.86 + ratio * 0.06,
                            written=written,
                            total=total,
                        )

                    session.write_flash(
                        restore_backup.offset,
                        raw,
                        flash_size=session.security.flash_size,
                        progress=settings_progress,
                    )
                    readback = session.read_flash(restore_backup.offset, restore_backup.size)
                    if readback != raw:
                        raise core.PostInstallError(
                            "vehicle-settings readback does not match the saved snapshot",
                            outcome="physical_recovery_required",
                            stage="raw_nvs_readback",
                        )
                    audit.event(
                        "raw_nvs_restore",
                        "verified",
                        path=str(restore_backup.data.path),
                        sha256=restore_backup.data.sha256,
                        offset=restore_backup.offset,
                        size=restore_backup.size,
                    )
                    settings_verification = "vehicle_settings_readback_verified"
                    self._event(
                        "settings_restore",
                        "completed",
                        "Saved vehicle settings restored and verified",
                        progress=0.94,
                    )
                session.close()
                session = None
                self._event(
                    "manual_reset",
                    "required",
                    "Press RESET once to restart the controller, then inspect it again",
                    progress=0.98,
                )
                result = OperationResult(
                    "success",
                    "full_flash_installation",
                    audit.path,
                    (
                        (
                            "Full-flash installation and vehicle-settings restoration completed; press RESET to restart the controller"
                        )
                        if restore_backup is not None
                        else (
                            "Full-flash installation completed; the vehicle-settings backup was retained because the target layout differs; press RESET to restart the controller"
                        )
                    ),
                    firmware_file=image.path.name,
                    firmware_sha256=image.sha256,
                    logical_backup=preparation.logical_backup,
                    raw_nvs_backup=raw_snapshot,
                    post_project_version=None,
                    post_verification=f"{settings_verification}; restart_required",
                    retry_app_update=False,
                )
                audit.event("result", result.status, result=result.safe_summary())
                self._event(
                    "result",
                    result.status,
                    result.message,
                    progress=1.0,
                    result=result.safe_summary(),
                )
                return result
            except core.FirmwareUpdateError as error:
                error.audit_path = audit.path
                if destructive_started:
                    error.physical_recovery_required = True
                audit.event(
                    "result",
                    "failed",
                    operation="full_flash_installation",
                    reason=str(error),
                    destructive_started=destructive_started,
                    physical_recovery_required=destructive_started,
                    raw_nvs_path=str(raw_snapshot.data.path),
                    raw_nvs_sha256=raw_snapshot.data.sha256,
                    operation_snapshot_metadata_path=str(raw_snapshot.metadata.path),
                )
                self._event(
                    "result",
                    "failed",
                    str(error),
                    destructive_started=destructive_started,
                    physical_recovery_required=destructive_started,
                    raw_nvs_path=str(raw_snapshot.data.path),
                    operation_snapshot_metadata_path=str(raw_snapshot.metadata.path),
                    audit_path=str(audit.path),
                )
                raise
            except Exception:
                error = core.ProtocolError(
                    "unexpected local updater error during full-flash installation"
                )
                error.audit_path = audit.path
                error.physical_recovery_required = destructive_started
                audit.event(
                    "result",
                    "failed",
                    operation="full_flash_installation",
                    reason=str(error),
                    destructive_started=destructive_started,
                    physical_recovery_required=destructive_started,
                    raw_nvs_path=str(raw_snapshot.data.path),
                    operation_snapshot_metadata_path=str(raw_snapshot.metadata.path),
                )
                self._event(
                    "result",
                    "failed",
                    str(error),
                    destructive_started=destructive_started,
                    physical_recovery_required=destructive_started,
                    raw_nvs_path=str(raw_snapshot.data.path),
                    operation_snapshot_metadata_path=str(raw_snapshot.metadata.path),
                    audit_path=str(audit.path),
                )
                raise error from None
            finally:
                if session is not None:
                    session.close()
                audit.close()

    def restore_vehicle_settings(
        self,
        metadata_path: Path,
        *,
        confirmation: str,
    ) -> OperationResult:
        """Restore one verified complete NVS snapshot in ESP32-S3 ROM mode."""

        with self._exclusive_operation():
            if confirmation != "RESTORE VEHICLE SETTINGS":
                raise ConfirmationError(
                    "vehicle-settings restoration requires explicit confirmation"
                )
            config = self.settings.update_config()
            audit = core.AuditLogger(config.log_dir)
            session = None
            write_started = False
            selected: RawNvsBackup | None = None
            safety_snapshot: RawNvsBackup | None = None
            try:
                selected = load_raw_nvs_backup(
                    metadata_path.expanduser().absolute(),
                    portable_read=True,
                )
                selected_data = read_private_file(
                    selected.data.path,
                    expected_size=selected.size,
                    expected_sha256=selected.data.sha256,
                    portable_read=True,
                )
                audit.event(
                    "settings_restore",
                    "started",
                    selected_metadata=str(selected.metadata.path),
                    selected_sha256=selected.data.sha256,
                    selected_offset=selected.offset,
                    selected_size=selected.size,
                )
                self._event(
                    "rom",
                    "waiting",
                    "Enter ESP32-S3 ROM download mode with BOOT/RESET if automatic reset does not connect",
                    progress=0.03,
                    indeterminate=True,
                )
                port_hint = capture_rom_port_hint(self.settings.port)
                session = self.rom_factory.open(
                    self.settings.port,
                    baud=self.settings.baud,
                    hint=port_hint,
                )
                session.security.validate_supported()
                if (
                    session.security.device_identity_sha256
                    != selected.device_identity_sha256
                ):
                    raise core.DevicePreflightError(
                        "selected vehicle-settings backup belongs to a different controller"
                    )
                current_table = session.read_flash(
                    PARTITION_TABLE_OFFSET,
                    PARTITION_TABLE_SIZE,
                )
                current_table_sha256 = hashlib.sha256(current_table).hexdigest()
                try:
                    current_nvs = find_nvs_partition(
                        parse_current_partition_table(current_table)
                    )
                except core.FirmwareUpdateError:
                    raise core.DevicePreflightError(
                        "the current NVS partition could not be identified; vehicle settings were not written"
                    ) from None
                if (current_nvs.offset, current_nvs.size) != (
                    selected.offset,
                    selected.size,
                ):
                    raise core.DevicePreflightError(
                        "selected vehicle-settings backup does not match the current NVS layout"
                    )
                self._event(
                    "restore_backup",
                    "started",
                    "Saving the current vehicle settings before restoration",
                    progress=0.18,
                )
                current_data = session.read_flash(current_nvs.offset, current_nvs.size)
                safety_snapshot = write_raw_nvs_backup(
                    current_data,
                    directory=self.settings.raw_nvs_dir,
                    selected_firmware_sha256=selected.selected_firmware_sha256,
                    device_identity_sha256=session.security.device_identity_sha256,
                    source_project_version=None,
                    partition_table_sha256=current_table_sha256,
                    offset=current_nvs.offset,
                    size=current_nvs.size,
                    purpose="restore_safety_snapshot",
                )
                audit.event(
                    "restore_safety_snapshot",
                    "stored",
                    path=str(safety_snapshot.data.path),
                    metadata_path=str(safety_snapshot.metadata.path),
                    sha256=safety_snapshot.data.sha256,
                    offset=safety_snapshot.offset,
                    size=safety_snapshot.size,
                )
                self._event(
                    "restore_backup",
                    "completed",
                    "Current vehicle settings stored and verified",
                    progress=0.30,
                    operation_snapshot_path=str(safety_snapshot.data.path),
                    operation_snapshot_metadata_path=str(
                        safety_snapshot.metadata.path
                    ),
                )
                self._event(
                    "settings_restore",
                    "started",
                    "Restoring the selected vehicle settings",
                    progress=0.32,
                )

                def settings_progress(written: int, total: int) -> None:
                    ratio = 0.0 if total <= 0 else min(1.0, written / total)
                    self._event(
                        "settings_restore",
                        "progress",
                        "Restoring the selected vehicle settings",
                        progress=0.32 + ratio * 0.48,
                        written=written,
                        total=total,
                    )

                write_started = True
                session.write_flash(
                    selected.offset,
                    selected_data,
                    flash_size=session.security.flash_size,
                    progress=settings_progress,
                )
                readback = session.read_flash(selected.offset, selected.size)
                if readback != selected_data:
                    raise core.PostInstallError(
                        "vehicle-settings readback does not match the selected backup",
                        outcome="physical_recovery_required",
                        stage="raw_nvs_readback",
                    )
                audit.event(
                    "settings_restore",
                    "verified",
                    selected_metadata=str(selected.metadata.path),
                    selected_sha256=selected.data.sha256,
                    safety_metadata=str(safety_snapshot.metadata.path),
                )
                self._event(
                    "settings_restore",
                    "completed",
                    "Selected vehicle settings restored and verified",
                    progress=0.92,
                )
                session.hard_reset()
                result = OperationResult(
                    "success",
                    "vehicle_settings_restore",
                    audit.path,
                    "Vehicle settings were restored and verified; the previous settings were retained as a safety backup",
                    raw_nvs_backup=safety_snapshot,
                    post_verification="vehicle_settings_readback_verified",
                    retry_app_update=None,
                )
                audit.event("result", result.status, result=result.safe_summary())
                self._event(
                    "result",
                    result.status,
                    result.message,
                    progress=1.0,
                    result=result.safe_summary(),
                )
                return result
            except core.FirmwareUpdateError as error:
                error.audit_path = audit.path
                if write_started:
                    error.physical_recovery_required = True
                audit.event(
                    "result",
                    "failed",
                    operation="vehicle_settings_restore",
                    reason=str(error),
                    write_started=write_started,
                    physical_recovery_required=write_started,
                    selected_metadata=(
                        None if selected is None else str(selected.metadata.path)
                    ),
                    safety_metadata=(
                        None
                        if safety_snapshot is None
                        else str(safety_snapshot.metadata.path)
                    ),
                )
                self._event(
                    "result",
                    "failed",
                    str(error),
                    physical_recovery_required=write_started,
                    operation_snapshot_metadata_path=(
                        None
                        if safety_snapshot is None
                        else str(safety_snapshot.metadata.path)
                    ),
                    audit_path=str(audit.path),
                )
                raise
            except Exception:
                error = core.ProtocolError(
                    "unexpected local updater error during vehicle-settings restoration"
                )
                error.audit_path = audit.path
                error.physical_recovery_required = write_started
                audit.event(
                    "result",
                    "failed",
                    operation="vehicle_settings_restore",
                    reason=str(error),
                    write_started=write_started,
                    physical_recovery_required=write_started,
                )
                self._event(
                    "result",
                    "failed",
                    str(error),
                    physical_recovery_required=write_started,
                    audit_path=str(audit.path),
                )
                raise error from None
            finally:
                if session is not None:
                    session.close()
                audit.close()
