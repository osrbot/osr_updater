"""Native bilingual desktop interface for OSR Updater."""

from __future__ import annotations

import queue
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - exercised by packaging prerequisite checks
    tk = None
    filedialog = messagebox = ttk = None

from . import __version__
from .build_info import load_build_info, load_third_party_notices
from .operations import (
    BackupChoice,
    ErasePreparation,
    OperationResult,
    UpdaterService,
    UpdaterSettings,
)
from .rom import preferred_controller_port
from .selection import FirmwareSelection, inspect_firmware_file
from .ui_text import event_text, text


NAVY = "#0B172A"
NAVY_LIGHT = "#13243D"
CYAN = "#21D4C2"
BLUE = "#4F8CFF"
SURFACE = "#F3F7FA"
CARD = "#FFFFFF"
TEXT = "#172033"
MUTED = "#687386"
SUCCESS = "#148568"
ERROR = "#C23B52"


def failure_guidance_key(error: BaseException, details: dict[str, Any]) -> str:
    """Select one safe, localized next step from operation failure facts."""

    if bool(details.get("full_flash_required")) or bool(
        getattr(error, "full_flash_required", False)
    ):
        return "guidance_full_flash_required"
    if bool(details.get("physical_recovery_required")) or bool(
        getattr(error, "physical_recovery_required", False)
    ):
        return "guidance_recovery_required"
    if bool(getattr(error, "no_app_reflash", False)) or details.get(
        "retry_app_update"
    ) is False:
        return "guidance_do_not_retry"
    if details.get("retry_app_update") is True:
        return "guidance_retry_allowed"
    return "guidance_contact_support"


class UpdaterApp:
    """Own one Tk root and keep all controller I/O off the UI thread."""

    def __init__(self, root: Any):
        self.root = root
        self.language = "en"
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.selection: FirmwareSelection | None = None
        self.service: UpdaterService | None = None
        self.preparation: ErasePreparation | None = None
        self.backup_choices: tuple[BackupChoice, ...] = ()
        self.busy = False
        self.last_backup_path = ""
        self.last_audit_path = ""
        self.last_failure_details: dict[str, Any] = {}

        self.words: dict[str, Any] = {}
        self.device_values: dict[str, Any] = {}
        self.file_name_var = tk.StringVar()
        self.file_type_var = tk.StringVar()
        self.file_size_var = tk.StringVar()
        self.file_sha_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.port_var = tk.StringVar(value="/dev/osrbot_base")

        self._configure_window()
        self._build_interface()
        self._apply_language()
        self.root.after(10, self._refresh_ports)
        self.root.after(50, self._refresh_backup_choices)
        self.root.after(100, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _w(self, key: str) -> Any:
        value = tk.StringVar()
        self.words[key] = value
        return value

    def _configure_window(self) -> None:
        self.root.title("OSR Updater")
        self.window_icon = tk.PhotoImage(width=64, height=64)
        self.window_icon.put(NAVY, to=(0, 0, 64, 64))
        self.window_icon.put(BLUE, to=(27, 10, 37, 39))
        self.window_icon.put(BLUE, to=(17, 29, 47, 39))
        self.window_icon.put(CYAN, to=(22, 39, 42, 45))
        self.window_icon.put("#E7F6F4", to=(14, 49, 50, 55))
        self.root.iconphoto(True, self.window_icon)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(1220, max(820, screen_width - 40))
        height = min(920, max(640, screen_height - 80))
        left = max(0, (screen_width - width) // 2)
        top = max(0, (screen_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        self.root.minsize(820, 640)
        self.root.configure(background=SURFACE)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=SURFACE)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=SURFACE, foreground=TEXT, font=("Sans", 10))
        style.configure("Card.TLabel", background=CARD, foreground=TEXT, font=("Sans", 10))
        style.configure("Muted.Card.TLabel", background=CARD, foreground=MUTED, font=("Sans", 9))
        style.configure("Title.Card.TLabel", background=CARD, foreground=TEXT, font=("Sans", 13, "bold"))
        style.configure("Accent.TButton", font=("Sans", 10, "bold"), padding=(14, 9))
        style.map(
            "Accent.TButton",
            background=[("!disabled", BLUE), ("active", "#3976E8")],
            foreground=[("!disabled", "white")],
        )
        style.configure("Danger.TButton", font=("Sans", 10, "bold"), padding=(14, 9))
        style.map(
            "Danger.TButton",
            background=[("!disabled", ERROR), ("active", "#A92F45")],
            foreground=[("!disabled", "white")],
        )
        style.configure("Secondary.TButton", font=("Sans", 10, "bold"), padding=(14, 9))
        style.map(
            "Secondary.TButton",
            background=[("!disabled", NAVY_LIGHT), ("active", "#1C3557")],
            foreground=[("!disabled", "white")],
        )
        style.configure(
            "Updater.Horizontal.TProgressbar",
            troughcolor="#DCE6ED",
            background=CYAN,
            bordercolor="#DCE6ED",
            lightcolor=CYAN,
            darkcolor=CYAN,
        )

    def _build_interface(self) -> None:
        header = tk.Frame(self.root, background=NAVY, height=104)
        header.pack(fill="x")
        header.pack_propagate(False)
        title_group = tk.Frame(header, background=NAVY)
        title_group.pack(side="left", padx=30, pady=17)
        tk.Label(
            title_group,
            textvariable=self._w("product_name"),
            background=NAVY,
            foreground="white",
            font=("Sans", 25, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_group,
            textvariable=self._w("tagline"),
            background=NAVY,
            foreground="#A9BDD6",
            font=("Sans", 11),
        ).pack(anchor="w", pady=(5, 0))
        language_button = tk.Button(
            header,
            textvariable=self._w("language"),
            command=self._toggle_language,
            background=NAVY_LIGHT,
            activebackground="#1C3557",
            foreground=CYAN,
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=18,
            pady=8,
            font=("Sans", 10, "bold"),
            cursor="hand2",
        )
        language_button.pack(side="right", padx=34)

        body = ttk.Frame(self.root, padding=(20, 16, 20, 10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(2, weight=1)

        connection = self._card(body, "connection", 0, 0)
        firmware = self._card(body, "firmware", 0, 1)
        actions = self._card(body, "actions", 1, 0, columnspan=2)

        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, textvariable=self._w("serial_port"), style="Muted.Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=(13, 5)
        )
        self.port_combo = ttk.Combobox(connection, textvariable=self.port_var, width=30)
        self.port_combo.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(0, 8))
        self.refresh_button = ttk.Button(
            connection, textvariable=self._w("refresh"), command=self._refresh_ports
        )
        self.refresh_button.grid(row=2, column=2, padx=(0, 8))
        self.inspect_button = ttk.Button(
            connection,
            textvariable=self._w("inspect"),
            command=self._inspect_device,
            style="Accent.TButton",
        )
        self.inspect_button.grid(row=2, column=3)

        device_grid = ttk.Frame(connection, style="Card.TFrame")
        device_grid.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(16, 2))
        device_grid.columnconfigure(1, weight=1)
        for row, key in enumerate(("device_version", "device_protocol", "device_voltage", "device_state")):
            ttk.Label(device_grid, textvariable=self._w(key), style="Muted.Card.TLabel").grid(
                row=row, column=0, sticky="w", pady=3
            )
            value = tk.StringVar()
            self.device_values[key] = value
            ttk.Label(device_grid, textvariable=value, style="Card.TLabel").grid(
                row=row, column=1, sticky="e", pady=3
            )

        firmware.columnconfigure(0, weight=1)
        self.file_name_label = ttk.Label(
            firmware,
            textvariable=self.file_name_var,
            style="Card.TLabel",
            wraplength=420,
        )
        self.file_name_label.grid(row=1, column=0, sticky="w", pady=(13, 9))
        self.select_button = ttk.Button(
            firmware,
            textvariable=self._w("select_file"),
            command=self._choose_file,
            style="Accent.TButton",
        )
        self.select_button.grid(row=1, column=1, sticky="e", padx=(10, 0))
        file_grid = ttk.Frame(firmware, style="Card.TFrame")
        file_grid.grid(row=2, column=0, columnspan=2, sticky="ew")
        file_grid.columnconfigure(1, weight=1)
        for row, (label_key, value) in enumerate(
            (("file_type", self.file_type_var), ("file_size", self.file_size_var), ("file_sha", self.file_sha_var))
        ):
            ttk.Label(file_grid, textvariable=self._w(label_key), style="Muted.Card.TLabel").grid(
                row=row, column=0, sticky="w", pady=3
            )
            ttk.Label(
                file_grid,
                textvariable=value,
                style="Card.TLabel",
                wraplength=330,
                justify="right",
            ).grid(row=row, column=1, sticky="e", pady=3)

        actions.columnconfigure(0, weight=1)
        button_row = ttk.Frame(actions, style="Card.TFrame")
        button_row.grid(row=1, column=0, sticky="ew", pady=(13, 8))
        for column in range(3):
            button_row.columnconfigure(column, weight=1, uniform="action-buttons")
        self.install_button = ttk.Button(
            button_row,
            textvariable=self._w("normal_update"),
            command=self._install_application,
            style="Accent.TButton",
        )
        self.install_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.full_flash_button = ttk.Button(
            button_row,
            textvariable=self._w("full_flash"),
            command=self._full_flash_installation,
            style="Danger.TButton",
        )
        self.full_flash_button.grid(row=0, column=1, sticky="ew", padx=6)
        self.restore_settings_button = ttk.Button(
            button_row,
            textvariable=self._w("restore_settings"),
            command=self._restore_controller,
            style="Secondary.TButton",
        )
        self.restore_settings_button.grid(row=0, column=2, sticky="ew", padx=(6, 0))

        self.backup_choice_var = tk.StringVar()
        self.backup_choice_label = ttk.Label(
            actions,
            textvariable=self._w("restore_from"),
            style="Muted.Card.TLabel",
        )
        self.backup_choice_label.grid(row=2, column=0, sticky="w", pady=(7, 3))
        self.backup_combo = ttk.Combobox(
            actions,
            textvariable=self.backup_choice_var,
            state="readonly",
        )
        self.backup_combo.grid(row=3, column=0, sticky="ew")
        self.backup_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_control_states())

        ttk.Label(actions, textvariable=self._w("progress"), style="Muted.Card.TLabel").grid(
            row=4, column=0, sticky="w", pady=(9, 3)
        )
        self.progress = ttk.Progressbar(
            actions,
            mode="determinate",
            maximum=100,
            style="Updater.Horizontal.TProgressbar",
        )
        self.progress.grid(row=5, column=0, sticky="ew")
        backup_row = ttk.Frame(actions, style="Card.TFrame")
        backup_row.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        backup_row.columnconfigure(1, weight=1)
        ttk.Label(backup_row, textvariable=self._w("backup_path"), style="Muted.Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10)
        )
        self.open_backup_button = ttk.Button(
            backup_row,
            textvariable=self._w("open_backups"),
            command=self._open_backup_directory,
        )
        self.open_backup_button.grid(row=0, column=1, sticky="w")
        ttk.Label(
            backup_row,
            textvariable=self._w("audit_path"),
            style="Muted.Card.TLabel",
        ).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(5, 0))
        self.open_audit_button = ttk.Button(
            backup_row,
            textvariable=self._w("open_audit"),
            command=self._open_audit_directory,
        )
        self.open_audit_button.grid(row=1, column=1, sticky="w", pady=(5, 0))

        activity = ttk.Frame(body, style="Card.TFrame", padding=18)
        activity.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(14, 0))
        activity.columnconfigure(0, weight=1)
        activity.rowconfigure(1, weight=1)
        ttk.Label(activity, textvariable=self._w("activity"), style="Title.Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        self.log = tk.Text(
            activity,
            height=10,
            background="#0E1B2D",
            foreground="#D6E2F0",
            insertbackground="white",
            selectbackground="#31547C",
            relief="flat",
            borderwidth=0,
            font=("Monospace", 9),
            padx=12,
            pady=10,
            state="disabled",
            wrap="word",
        )
        self.log.grid(row=1, column=0, sticky="nsew")
        log_scrollbar = ttk.Scrollbar(activity, orient="vertical", command=self.log.yview)
        log_scrollbar.grid(row=1, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scrollbar.set)

        footer = ttk.Frame(self.root, padding=(26, 0, 26, 12))
        footer.pack(fill="x")
        self.status_label = ttk.Label(footer, textvariable=self.status_var)
        self.status_label.pack(side="left")
        ttk.Label(footer, textvariable=self._w("about"), foreground=MUTED).pack(side="right")

    def _card(
        self,
        parent: Any,
        title_key: str,
        row: int,
        column: int,
        *,
        columnspan: int = 1,
    ) -> Any:
        card = ttk.Frame(parent, style="Card.TFrame", padding=18)
        card.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            sticky="nsew",
            padx=(0, 7) if column == 0 and columnspan == 1 else (7, 0) if column == 1 else 0,
            pady=(0, 14) if row == 0 else 0,
        )
        ttk.Label(card, textvariable=self._w(title_key), style="Title.Card.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        return card

    def _apply_language(self) -> None:
        for key, variable in self.words.items():
            variable.set(text(self.language, key, version=__version__))
        self.root.title(text(self.language, "window_title"))
        if self.selection is None:
            self.file_name_var.set(text(self.language, "no_file"))
            self.file_type_var.set("—")
            self.file_size_var.set("—")
            self.file_sha_var.set("—")
        else:
            self._display_selection(self.selection)
        if not any(value.get() for value in self.device_values.values()):
            for value in self.device_values.values():
                value.set("—")
        if not self.status_var.get():
            self.status_var.set(text(self.language, "ready"))
        self._display_backup_choices()
        self._update_control_states()

    def _toggle_language(self) -> None:
        self.language = "zh" if self.language == "en" else "en"
        self._apply_language()

    def _refresh_ports(self) -> None:
        values = ["/dev/osrbot_base"]
        ports: tuple[Any, ...] = ()
        try:
            from serial.tools import list_ports

            ports = tuple(list_ports.comports())
            values.extend(port.device for port in ports)
        except Exception:
            pass
        unique = tuple(dict.fromkeys(values))
        self.port_combo.configure(values=unique)
        self.port_var.set(preferred_controller_port(self.port_var.get().strip(), ports))

    def _refresh_backup_choices(self) -> None:
        """Verify saved backups away from the Tk event loop."""

        selection = self.selection
        if selection is None or selection.kind != "recovery":
            self.events.put({"_kind": "backup_choices", "result": ()})
            return
        offset = getattr(selection.image, "nvs_offset", None)
        size = getattr(selection.image, "nvs_size", None)
        if not isinstance(offset, int) or not isinstance(size, int):
            self.events.put({"_kind": "backup_choices", "result": ()})
            return

        def load() -> None:
            try:
                choices = UpdaterService().list_vehicle_settings_backups(
                    offset=offset,
                    size=size,
                )
            except BaseException:
                choices = ()
            self.events.put({"_kind": "backup_choices", "result": choices})

        threading.Thread(target=load, name="osr-updater-backups", daemon=True).start()

    def _display_backup_choices(self) -> None:
        labels = tuple(self._backup_choice_text(item) for item in self.backup_choices)
        self.backup_combo.configure(values=labels)
        current = self.backup_combo.current()
        if labels and current < 0:
            recommended = next(
                (index for index, item in enumerate(self.backup_choices) if item.recommended),
                0,
            )
            self.backup_combo.current(recommended)
        elif not labels:
            key = (
                "select_full_flash_for_backups"
                if self.selection is None or self.selection.kind != "recovery"
                else "no_compatible_settings_backups"
            )
            self.backup_choice_var.set(text(self.language, key))

    def _backup_choice_text(self, choice: BackupChoice) -> str:
        try:
            timestamp = datetime.fromisoformat(choice.captured_at).astimezone().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            timestamp = choice.captured_at
        source = choice.source_project_version or text(self.language, "unknown_firmware")
        kind = text(self.language, choice.purpose)
        recommended = (
            f"{text(self.language, 'factory_baseline')} · "
            if choice.recommended
            else ""
        )
        return (
            f"{recommended}{timestamp} · {source} · {kind} · "
            f"ID {choice.controller_id} · {choice.size // 1024} KiB"
        )

    def _selected_backup_choice(self) -> BackupChoice | None:
        index = self.backup_combo.current()
        if index < 0 or index >= len(self.backup_choices):
            return None
        return self.backup_choices[index]

    def _choose_file(self) -> None:
        if self.busy or self.preparation is not None:
            self._show_busy()
            return
        selected = filedialog.askopenfilename(
            title=text(self.language, "select_dialog"),
            filetypes=(
                (text(self.language, "firmware_file_filter"), "*.bin"),
                (text(self.language, "all_files_filter"), "*"),
            ),
        )
        if not selected:
            return
        path = Path(selected).expanduser().absolute()
        self.selection = None
        self.preparation = None
        self.service = None
        self.file_name_var.set(path.name)
        self.file_type_var.set("—")
        self.file_size_var.set("—")
        self.file_sha_var.set("—")
        self._start_worker("select", lambda: inspect_firmware_file(path))

    def _inspect_device(self) -> None:
        if self.busy or self.preparation is not None:
            self._show_busy()
            return
        self._clear_device_display("device_inspecting")
        service = self._new_service()
        self._start_worker("inspect", service.inspect)

    def _install_application(self) -> None:
        if self.selection is None:
            messagebox.showerror("OSR Updater", text(self.language, "error_no_file"))
            return
        if self.selection.kind != "application":
            messagebox.showerror("OSR Updater", text(self.language, "error_application_only"))
            return
        if not messagebox.askyesno(
            text(self.language, "confirm_install_title"),
            text(self.language, "confirm_install"),
            icon="warning",
        ):
            self._append_log(text(self.language, "confirmation_cancelled"))
            return
        self._refresh_ports()
        service = self._new_service()
        selected_path = self.selection.path
        self._start_worker(
            "application",
            lambda: service.install_application(
                selected_path,
                confirmation="INSTALL FIRMWARE",
            ),
        )

    def _full_flash_installation(self) -> None:
        if self.preparation is not None:
            self._execute_recovery()
            return
        self._prepare_recovery()

    def _prepare_recovery(self, restore_metadata_path: Path | None = None) -> None:
        if self.selection is None:
            messagebox.showerror("OSR Updater", text(self.language, "error_no_file"))
            return
        if self.selection.kind != "recovery":
            messagebox.showerror("OSR Updater", text(self.language, "error_recovery_only"))
            return
        self._refresh_ports()
        restore_selected = restore_metadata_path is not None
        if not messagebox.askyesno(
            text(
                self.language,
                "confirm_controller_restore_title"
                if restore_selected
                else "confirm_prepare_title",
            ),
            text(
                self.language,
                "confirm_controller_restore"
                if restore_selected
                else "confirm_prepare",
            ),
            icon="warning",
        ):
            self._append_log(text(self.language, "confirmation_cancelled"))
            return
        self.service = self._new_service()
        selected_path = self.selection.path
        self._start_worker(
            "prepare_recovery",
            lambda: self.service.prepare_recovery(
                selected_path,
                confirmation="PREPARE RECOVERY",
                restore_metadata_path=restore_metadata_path,
            ),
        )

    def _execute_recovery(self) -> None:
        if self.preparation is None or self.service is None:
            messagebox.showerror("OSR Updater", text(self.language, "error_recovery_only"))
            return
        if not messagebox.askyesno(
            text(self.language, "confirm_erase_title"),
            text(
                self.language,
                "confirm_erase_restore"
                if self.preparation.restore_source == "selected_backup"
                else "confirm_erase",
            ),
            icon="warning",
        ):
            self._append_log(text(self.language, "confirmation_cancelled"))
            return
        preparation_id = self.preparation.preparation_id
        self._start_worker(
            "execute_recovery",
            lambda: self.service.execute_recovery(
                preparation_id,
                acknowledge_other_data_loss=True,
            ),
            reset_progress=False,
        )

    def _restore_controller(self) -> None:
        if self.busy or self.preparation is not None:
            self._show_busy()
            return
        if self.selection is None or self.selection.kind != "recovery":
            messagebox.showerror(
                "OSR Updater",
                text(self.language, "error_restore_requires_full_image"),
            )
            return
        backup = self._selected_backup_choice()
        if backup is None:
            messagebox.showerror(
                "OSR Updater",
                text(self.language, "error_restore_requires_backup"),
            )
            return
        self._prepare_recovery(backup.metadata_path)

    def _new_service(self) -> UpdaterService:
        settings = UpdaterSettings(port=self.port_var.get().strip() or "/dev/osrbot_base")
        return UpdaterService(settings=settings, event_sink=self.events.put)

    def _start_worker(
        self,
        name: str,
        operation: Callable[[], Any],
        *,
        reset_progress: bool = True,
    ) -> None:
        if self.busy:
            self._show_busy()
            return
        self._set_busy(True)
        if reset_progress:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
        self.last_failure_details = {}

        def run() -> None:
            try:
                result = operation()
            except BaseException as error:
                self.events.put({"_kind": "worker_done", "name": name, "error": error})
            else:
                self.events.put({"_kind": "worker_done", "name": name, "result": result})

        threading.Thread(target=run, name=f"osr-updater-{name}", daemon=True).start()

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event.get("_kind") == "backup_choices":
                    result = event.get("result")
                    if isinstance(result, tuple):
                        self.backup_choices = result
                        self._display_backup_choices()
                        self._update_control_states()
                elif event.get("_kind") == "worker_done":
                    self._handle_worker_done(event)
                else:
                    self._handle_operation_event(event)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _handle_operation_event(self, event: dict[str, Any]) -> None:
        phase = str(event.get("phase", "operation"))
        status = str(event.get("status", "info"))
        fallback = str(event.get("message", ""))
        line = event_text(self.language, phase, status, fallback)
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        rom_port = details.get("rom_port")
        if isinstance(rom_port, str) and rom_port:
            current_values = self.port_combo.cget("values")
            values = (
                tuple(self.root.tk.splitlist(current_values))
                if isinstance(current_values, str)
                else tuple(current_values)
            )
            if rom_port not in values:
                self.port_combo.configure(values=(*values, rom_port))
            self.port_var.set(rom_port)
        audit_path = details.get("audit_path")
        if isinstance(audit_path, str) and audit_path:
            self._set_audit_path(audit_path)
        progress = event.get("progress")
        if bool(details.get("indeterminate")):
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        elif isinstance(progress, (float, int)):
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress.configure(value=max(0, min(100, float(progress) * 100)))
        displayed_paths: list[str] = []
        operation_path = details.get("operation_snapshot_path") or details.get(
            "raw_nvs_path"
        )
        if isinstance(operation_path, str) and operation_path:
            displayed_paths.append(
                f"{text(self.language, 'complete_settings_backup')}: {operation_path}"
            )
        operation_metadata_path = details.get("operation_snapshot_metadata_path")
        if isinstance(operation_metadata_path, str) and operation_metadata_path:
            displayed_paths.append(
                f"{text(self.language, 'complete_settings_metadata')}: "
                f"{operation_metadata_path}"
            )
        if not displayed_paths:
            for path_key in ("path", "backup_path"):
                candidate = details.get(path_key)
                if isinstance(candidate, str) and candidate:
                    displayed_paths.append(candidate)
        if displayed_paths:
            self._set_backup_path("\n".join(displayed_paths))
        raw_backup = details.get("raw_nvs_backup")
        if isinstance(raw_backup, dict) and isinstance(raw_backup.get("path"), str):
            self._set_backup_path(raw_backup["path"])
        result = details.get("result")
        if isinstance(result, dict):
            self._capture_result_backup(result)
        if phase == "result" and status == "failed":
            self.last_failure_details = dict(details)
        if phase == "result" and status == "failed" and fallback:
            line = f"{line}: {fallback}"
        self._append_log(line)
        self.status_var.set(line)
        if phase == "manual_reset" and status == "required":
            messagebox.showinfo(
                text(self.language, "manual_reset_title"),
                text(self.language, "manual_reset_required"),
            )
            for delay in (300, 1500, 3000):
                self.root.after(delay, self._refresh_ports)

    def _handle_worker_done(self, event: dict[str, Any]) -> None:
        name = str(event.get("name", "operation"))
        error = event.get("error")
        continue_full_flash = False
        self._set_busy(False)
        self.progress.stop()
        self.progress.configure(mode="determinate")
        if name == "execute_recovery":
            self.preparation = None
            self.service = None
        if error is not None:
            audit_path = getattr(error, "audit_path", None) or self.last_failure_details.get(
                "audit_path"
            )
            if audit_path:
                self._set_audit_path(str(audit_path))
            if name == "inspect":
                self._clear_device_display("device_unavailable")
                detail = text(self.language, "inspect_unavailable_detail")
            else:
                detail = str(error)
                detail += (
                    f"\n\n{text(self.language, failure_guidance_key(error, self.last_failure_details))}"
                )
            current_failure_has_backup = any(
                self.last_failure_details.get(key)
                for key in (
                    "backup_path",
                    "raw_nvs_path",
                    "operation_snapshot_path",
                    "operation_snapshot_metadata_path",
                )
            )
            if self.last_backup_path and current_failure_has_backup:
                detail += f"\n\n{text(self.language, 'backup_available')}"
            if audit_path:
                detail += f"\n{text(self.language, 'audit_available')}"
            self.status_var.set(text(self.language, "failed"))
            self._append_log(detail)
            messagebox.showerror("OSR Updater", detail)
            return
        result = event.get("result")
        if name == "select" and isinstance(result, FirmwareSelection):
            self.selection = result
            self.preparation = None
            self.service = None
            self._display_selection(result)
            self.status_var.set(text(self.language, "event_select"))
            self._append_log(
                f"{text(self.language, 'event_select')}: {result.path.name} · {result.sha256}"
            )
            self._refresh_backup_choices()
        elif name == "inspect":
            self._display_device(result)
            self.status_var.set(text(self.language, "inspect_ok"))
        elif name == "prepare_recovery" and isinstance(result, ErasePreparation):
            self._set_audit_path(str(result.audit_path))
            self.preparation = result
            self._capture_preparation_backup(result)
            self.progress.configure(value=25)
            self.status_var.set(text(self.language, "event_confirm_erase_ready"))
            continue_full_flash = True
        elif isinstance(result, OperationResult):
            self._set_audit_path(str(result.audit_path))
            self._capture_result_backup(result.safe_summary())
            self._display_result_device(result)
            self.progress.configure(value=100)
            completed = result.status == "success"
            self.status_var.set(
                text(self.language, "success" if completed else "completed_unverified")
            )
            result_message = self._localized_result_message(result)
            if completed:
                messagebox.showinfo("OSR Updater", result_message)
            else:
                messagebox.showwarning("OSR Updater", result_message)
            self._refresh_backup_choices()
        self._update_control_states()
        if continue_full_flash:
            self.root.after_idle(self._execute_recovery)

    def _display_selection(self, selection: FirmwareSelection) -> None:
        self.file_name_var.set(selection.path.name)
        self.file_type_var.set(text(self.language, selection.kind))
        self.file_size_var.set(self._format_bytes(selection.size))
        self.file_sha_var.set(selection.sha256)
        self._update_control_states()

    def _display_device(self, inspection: Any) -> None:
        summary = inspection.safe_summary()
        self.device_values["device_version"].set(summary.get("project_version") or "—")
        self.device_values["device_protocol"].set(summary.get("protocol") or text(self.language, "unknown"))
        voltage = summary.get("battery_voltage")
        self.device_values["device_voltage"].set(
            f"{voltage:.2f} V" if isinstance(voltage, (float, int)) else text(self.language, "unknown")
        )
        ready = summary.get("profile_state") in {None, "READY"} and not summary.get("ota_session_active")
        self.device_values["device_state"].set(
            text(self.language, "device_ready" if ready else "device_attention")
        )

    def _clear_device_display(self, state_key: str) -> None:
        self.device_values["device_version"].set("—")
        self.device_values["device_protocol"].set("—")
        self.device_values["device_voltage"].set("—")
        self.device_values["device_state"].set(text(self.language, state_key))

    def _display_result_device(self, result: OperationResult) -> None:
        if result.post_project_version:
            self.device_values["device_version"].set(result.post_project_version)
        if result.operation == "full_flash_installation":
            self._clear_device_display("device_reinspect")
            return
        if result.operation == "vehicle_settings_restore":
            self.device_values["device_state"].set(
                text(self.language, "device_reinspect")
            )
            return
        self.device_values["device_state"].set(
            text(
                self.language,
                "device_ready" if result.status == "success" else "device_attention",
            )
        )

    def _capture_preparation_backup(self, preparation: ErasePreparation) -> None:
        paths = [
            f"{text(self.language, 'complete_settings_backup')}: "
            f"{preparation.raw_nvs_backup.data.path}",
            f"{text(self.language, 'complete_settings_metadata')}: "
            f"{preparation.raw_nvs_backup.metadata.path}",
        ]
        self._set_backup_path("\n".join(paths))

    def _capture_result_backup(self, result: dict[str, Any]) -> None:
        logical = result.get("logical_backup")
        raw = result.get("raw_nvs_backup")
        if isinstance(raw, dict) and isinstance(raw.get("path"), str):
            paths = [
                f"{text(self.language, 'complete_settings_backup')}: {raw['path']}"
            ]
            if isinstance(raw.get("metadata_path"), str):
                paths.append(
                    f"{text(self.language, 'complete_settings_metadata')}: "
                    f"{raw['metadata_path']}"
                )
            self._set_backup_path("\n".join(paths))
        elif isinstance(logical, dict) and isinstance(logical.get("path"), str):
            self._set_backup_path(logical["path"])

    def _set_backup_path(self, path: str) -> None:
        self.last_backup_path = path
        self._update_control_states()

    def _set_audit_path(self, path: str) -> None:
        self.last_audit_path = path
        self._update_control_states()

    def _localized_result_message(self, result: OperationResult) -> str:
        if result.operation == "application_update":
            key = "result_app_success" if result.status == "success" else "result_app_unverified"
            return text(self.language, key)
        if result.operation == "vehicle_settings_restore":
            return text(self.language, "result_settings_restored")
        restored = "vehicle_settings_readback_verified" in result.post_verification
        if restored:
            key = (
                "result_full_restored"
                if result.status == "success"
                else "result_full_restored_unverified"
            )
        else:
            key = "result_full_success" if result.status == "success" else "result_full_unverified"
        return text(self.language, key)

    def _open_backup_directory(self) -> None:
        self._open_directory(UpdaterSettings().state_dir, "backup_directory_opened")

    def _open_audit_directory(self) -> None:
        self._open_directory(UpdaterSettings().audit_dir, "audit_directory_opened")

    def _open_directory(self, directory: Path, status_key: str) -> None:
        try:
            directory = directory.expanduser().absolute()
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            subprocess.Popen(
                ["xdg-open", str(directory)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError):
            messagebox.showerror(
                "OSR Updater",
                text(self.language, "error_open_directory"),
            )
            return
        self.status_var.set(text(self.language, status_key))

    def _set_busy(self, value: bool) -> None:
        self.busy = value
        self.status_var.set(text(self.language, "working" if value else "ready"))
        self._update_control_states()

    def _update_control_states(self) -> None:
        locked = self.busy or self.preparation is not None
        self.port_combo.configure(state="disabled" if locked else "normal")
        self.refresh_button.configure(state="disabled" if locked else "normal")
        self.inspect_button.configure(state="disabled" if locked else "normal")
        self.select_button.configure(state="disabled" if locked else "normal")
        app_ready = self.selection is not None and self.selection.kind == "application"
        recovery_ready = self.selection is not None and self.selection.kind == "recovery"
        self.install_button.configure(state="normal" if app_ready and not locked else "disabled")
        self.full_flash_button.configure(
            state="normal" if recovery_ready and not self.busy else "disabled"
        )
        self.restore_settings_button.configure(
            state=(
                "normal"
                if recovery_ready
                and self._selected_backup_choice() is not None
                and not locked
                else "disabled"
            )
        )
        self.backup_combo.configure(state="disabled" if locked else "readonly")
        self.open_backup_button.configure(state="normal")
        self.open_audit_button.configure(state="normal")

    def _show_busy(self) -> None:
        messagebox.showwarning("OSR Updater", text(self.language, "error_busy"))

    def _append_log(self, message: str) -> None:
        if not message:
            return
        stamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{stamp}] {message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    @staticmethod
    def _format_bytes(value: int) -> str:
        if value >= 1024 * 1024:
            return f"{value / (1024 * 1024):.2f} MiB ({value:,} bytes)"
        return f"{value / 1024:.1f} KiB ({value:,} bytes)"

    def _on_close(self) -> None:
        if self.busy:
            self._show_busy()
            return
        self.root.destroy()


def main() -> int:
    if os.environ.get("OSR_UPDATER_SELF_TEST") == "1":
        build_info = load_build_info()
        notices = load_third_party_notices()
        runtime_ready = True
        if bool(getattr(sys, "frozen", False)):
            try:
                from esptool import cmds
                from esptool.targets.esp32s3 import ESP32S3ROM

                runtime_ready = callable(cmds.write_flash) and ESP32S3ROM is not None
            except ImportError:
                runtime_ready = False
        return (
            0
            if runtime_ready
            and build_info.get("version") == __version__
            and "esptool" in notices
            else 2
        )
    if tk is None:
        sys.stderr.write("OSR Updater requires Python Tk support.\n")
        return 2
    root = tk.Tk()
    UpdaterApp(root)
    root.mainloop()
    return 0
