from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from osr_updater import core
from osr_updater import __version__
from osr_updater.gui import failure_guidance_key
from osr_updater.storage import (
    default_state_directory,
    list_raw_nvs_backups,
    load_raw_nvs_backup,
    read_private_file,
    write_raw_nvs_backup,
    write_private_file,
)
from osr_updater.ui_text import TEXT, event_text, text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "osr_updater"


class ProductBoundaryTest(unittest.TestCase):
    def test_one_native_bilingual_interface_has_no_web_or_user_cli(self):
        self.assertEqual(set(TEXT), {"en", "zh"})
        self.assertEqual(text("en", "product_name"), "OSR Updater")
        self.assertEqual(text("zh", "product_name"), "OSR Updater")
        self.assertNotEqual(text("en", "tagline"), text("zh", "tagline"))
        self.assertNotEqual(
            event_text("en", "backup", "completed", "fallback"),
            event_text("zh", "backup", "completed", "fallback"),
        )
        self.assertFalse((PACKAGE_ROOT / "cli.py").exists())
        self.assertFalse((PACKAGE_ROOT / "web.py").exists())
        self.assertFalse((PACKAGE_ROOT / "resources").exists())

        gui_source = (PACKAGE_ROOT / "gui.py").read_text(encoding="utf-8")
        tree = ast.parse(gui_source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertNotIn("argparse", imports)
        self.assertNotIn("http.server", imports)
        self.assertIn("tkinter", gui_source)
        self.assertIn("self.open_backup_button = ttk.Button", gui_source)
        self.assertIn("self.open_audit_button = ttk.Button", gui_source)
        self.assertNotIn("self.audit_var", gui_source)
        self.assertNotIn("self.backup_var", gui_source)

    def test_repository_contains_no_firmware_payload(self):
        firmware_files = [
            path
            for path in PROJECT_ROOT.rglob("*")
            if path.is_file() and path.suffix.lower() in {".bin", ".hex"}
        ]
        self.assertEqual(firmware_files, [])

    def test_private_files_are_atomic_mode_0600_and_read_back(self):
        with tempfile.TemporaryDirectory() as directory:
            stored = write_private_file(
                Path(directory) / "private",
                prefix="vehicle-settings",
                suffix=".json",
                data=b"{}\n",
            )
            self.assertEqual(stored.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(read_private_file(stored.path), b"{}\n")

    def test_portable_input_may_be_read_only_but_never_group_writable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portable.json"
            path.write_bytes(b"{}\n")
            path.chmod(0o644)
            self.assertEqual(read_private_file(path, portable_read=True), b"{}\n")
            with self.assertRaises(core.AuditError):
                read_private_file(path)
            path.chmod(0o664)
            with self.assertRaises(core.AuditError):
                read_private_file(path, portable_read=True)

    def test_default_state_directory_uses_the_standalone_product_name(self):
        self.assertEqual(default_state_directory().name, "osr-updater")
        self.assertNotEqual(default_state_directory().parent.name, "osracer")

    def test_complete_settings_snapshot_metadata_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = "a" * 64
            selected = "b" * 64
            table = "c" * 64
            snapshot = write_raw_nvs_backup(
                b"current",
                directory=root / "settings",
                selected_firmware_sha256=selected,
                device_identity_sha256=identity,
                source_project_version="current-version",
                partition_table_sha256=table,
                offset=0x9000,
                size=7,
            )
            self.assertEqual(snapshot.data.path.read_bytes(), b"current")
            self.assertEqual(load_raw_nvs_backup(snapshot.metadata.path), snapshot)
            self.assertEqual(snapshot.data.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(snapshot.metadata.path.stat().st_mode & 0o777, 0o600)
            safety = write_raw_nvs_backup(
                b"previous",
                directory=root / "settings",
                selected_firmware_sha256=selected,
                device_identity_sha256=identity,
                source_project_version=None,
                partition_table_sha256=table,
                offset=0x9000,
                size=8,
                purpose="restore_safety_snapshot",
            )
            self.assertEqual(load_raw_nvs_backup(safety.metadata.path), safety)

    def test_identical_complete_settings_reuse_one_verified_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "settings"
            values = {
                "selected_firmware_sha256": "b" * 64,
                "device_identity_sha256": "a" * 64,
                "source_project_version": "source-a",
                "partition_table_sha256": "c" * 64,
                "offset": 0x9000,
                "size": 7,
            }
            first = write_raw_nvs_backup(b"current", directory=root, **values)
            second = write_raw_nvs_backup(
                b"current",
                directory=root,
                **{
                    **values,
                    "selected_firmware_sha256": "d" * 64,
                    "source_project_version": "source-b",
                },
            )
            third = write_raw_nvs_backup(
                b"current",
                directory=root,
                **{
                    **values,
                    "selected_firmware_sha256": "d" * 64,
                    "source_project_version": "source-b",
                },
            )

            self.assertNotEqual(first.metadata.path, second.metadata.path)
            self.assertEqual(first.data, second.data)
            self.assertEqual(second, third)
            self.assertEqual(len(list_raw_nvs_backups(root)), 1)
            self.assertEqual(len(tuple(root.glob("*.bin"))), 1)
            self.assertEqual(len(tuple(root.glob("*.json"))), 2)

    def test_version_is_release_shaped(self):
        self.assertRegex(__version__, r"^[1-9][0-9]*\.[0-9]+\.[0-9]+$")

    def test_source_tree_self_test_validates_version_and_notices(self):
        environment = dict(os.environ)
        environment["OSR_UPDATER_SELF_TEST"] = "1"
        completed = subprocess.run(
            [sys.executable, "-B", str(PROJECT_ROOT / "entry.py")],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_failure_guidance_uses_safe_operation_facts(self):
        error = RuntimeError("failed")
        self.assertEqual(failure_guidance_key(error, {}), "guidance_contact_support")
        self.assertEqual(
            failure_guidance_key(error, {"retry_app_update": True}),
            "guidance_retry_allowed",
        )
        error.no_app_reflash = True
        self.assertEqual(
            failure_guidance_key(error, {"retry_app_update": True}),
            "guidance_do_not_retry",
        )
        error.physical_recovery_required = True
        self.assertEqual(
            failure_guidance_key(error, {"retry_app_update": False}),
            "guidance_recovery_required",
        )
        error = RuntimeError("unsupported App OTA")
        error.full_flash_required = True
        self.assertEqual(
            failure_guidance_key(error, {}),
            "guidance_full_flash_required",
        )

    def test_public_workflow_has_one_firmware_picker_and_three_write_actions(self):
        gui_source = (PACKAGE_ROOT / "gui.py").read_text(encoding="utf-8")
        operations_source = (PACKAGE_ROOT / "operations.py").read_text(encoding="utf-8")
        self.assertEqual(gui_source.count("filedialog.askopenfilename("), 1)
        self.assertIn("def _choose_file", gui_source)
        self.assertNotIn("establish_baseline", gui_source)
        self.assertNotIn("restore_factory", gui_source)
        self.assertIn("ttk.Scrollbar", gui_source)
        self.assertIn("self.full_flash_button = ttk.Button", gui_source)
        self.assertIn("self.restore_settings_button = ttk.Button", gui_source)
        self.assertIn("self.backup_combo = ttk.Combobox", gui_source)
        self.assertIn("nvs_offset", gui_source)
        self.assertIn("nvs_size", gui_source)
        self.assertIn('style="Secondary.TButton"', gui_source)
        self.assertIn('uniform="action-buttons"', gui_source)
        self.assertIn('details.get("rom_port")', gui_source)
        self.assertIn("for delay in (300, 1500, 3000)", gui_source)
        self.assertIn('self._clear_device_display("device_reinspect")', gui_source)
        self.assertIn("self.root.iconphoto", gui_source)
        self.assertIn('self._clear_device_display("device_unavailable")', gui_source)
        self.assertIn('phase == "manual_reset"', gui_source)
        inspect_source = gui_source.split("def _inspect_device", 1)[1].split(
            "def _install_application", 1
        )[0]
        self.assertNotIn("_refresh_ports", inspect_source)
        self.assertEqual(gui_source.count('textvariable=self._w("full_flash")'), 1)
        self.assertIn("def _full_flash_installation", gui_source)
        self.assertIn("def _restore_controller", gui_source)
        self.assertIn("self.root.after_idle(self._execute_recovery)", gui_source)
        self.assertNotIn("simpledialog", gui_source)
        self.assertNotIn("askstring", gui_source)
        self.assertNotIn("ERASE AND INSTALL", gui_source)
        self.assertNotIn("Release BOOT", operations_source)
        self.assertIn('"restore_settings": "Factory Restore"', (PACKAGE_ROOT / "ui_text.py").read_text(encoding="utf-8"))
        self.assertIn('"restore_settings": "恢复出厂"', (PACKAGE_ROOT / "ui_text.py").read_text(encoding="utf-8"))
        self.assertNotIn("self.prepare_button", gui_source)
        self.assertNotIn("self.execute_button", gui_source)
        self.assertNotIn("official", gui_source.lower())
        self.assertNotIn("customer", gui_source.lower())
        self.assertIn('"application_update"', operations_source)
        self.assertIn('"full_flash_installation"', operations_source)
        self.assertIn('"vehicle_settings_restore"', operations_source)
        self.assertNotIn('"custom_app"', operations_source)
        self.assertNotIn('"official_update"', operations_source)

    def test_every_user_visible_text_key_exists_in_both_languages(self):
        english = set(TEXT["en"])
        chinese = set(TEXT["zh"])
        self.assertEqual(english, chinese)
        for language, table in TEXT.items():
            for key, value in table.items():
                with self.subTest(language=language, key=key):
                    self.assertTrue(value.strip())


if __name__ == "__main__":
    unittest.main()
