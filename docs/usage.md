# OSR Updater User Guide

## Before You Begin

- Use a firmware file supplied for the controller or a file you intentionally
  developed for an ESP32-S3 controller.
- Keep the vehicle stationary with stable power throughout the operation.
- Stop ROS and every other program that may open the controller serial device.
- Do not disconnect USB or power while an installation is active.

## Select the Firmware

OSR Updater uses one firmware-file selector and classifies the file by content:

- **Application update** installs an ESP-IDF application through the running
  controller firmware. It requires the OSR App OTA commands and does not erase
  the settings partition.
- **Full-flash image** erases the controller and writes a merged image at flash
  offset zero. The image may use an independently developed partition layout.

The updater does not contain firmware files. Verify the displayed SHA-256 value
against the value supplied with the selected file.

## Protect Vehicle Settings

The complete NVS partition contains factory-test and vehicle-calibration data
that can be expensive to reproduce. Before every full-flash installation, OSR
Updater reads the current partition in ROM recovery mode and stores a private,
controller-bound snapshot. No user judgement about a "factory state" is needed.

## Install an Application

1. Inspect the controller.
2. Select the application file and verify its type and SHA-256.
3. Select **Install firmware** and confirm the operation.
4. Keep the vehicle stationary until the final result appears.

App OTA does not enter ROM recovery mode and does not erase or rewrite NVS. The
readable 24-item settings export is an additional diagnostic backup when the
running firmware supports it. If the current firmware does not expose OSR App
OTA, select a full-flash image and use BOOT/RESET recovery mode instead.

## Install a Full-Flash Image

1. Select the full-flash file and verify its type and SHA-256.
2. Select **Full-flash installation**.
3. Follow the BOOT/RESET prompt.
   The controller re-enumerates on a different serial device; OSR Updater
   follows the same physical USB connection automatically and displays the ROM
   serial device in the controller field.
4. Select **Open backups** and verify that the current complete settings backup
   is present.
5. Accept the final destructive confirmation displayed automatically after the
   backup.
6. Keep power and USB connected until the write result appears.
7. When prompted after the write, press RESET once. The serial-device list
   refreshes automatically; then select **Inspect**.

Backup and installation are one guided action. After the backup succeeds, leave
the controller in recovery mode and do not press RESET or disconnect USB while
reviewing the final confirmation. Cancelling that confirmation keeps the backup
and does not erase the controller; select **Full-flash installation** again to
continue with the existing preparation.

The updater accepts a structurally valid ESP32-S3 merged image even when it has
a custom target layout. It automatically restores the just-captured settings
only when all of the following match:

- connected controller identity;
- settings partition offset and size;
- backup ownership, permissions, size, and SHA-256.

The restored bytes are read back and compared byte for byte before completion.
When the target layout differs, the backup is retained and is not written into
the unknown layout. The updater does not wait for an OSR protocol response after
full-flash writing; this avoids delaying independently developed firmware that
does not implement that interface.

## Factory Restore

1. Select the required full-flash firmware file.
2. Review the factory vehicle-settings baseline. The selected firmware's NVS
   layout filters the list automatically.
3. Select **Factory Restore**.
4. Follow the BOOT/RESET prompt and confirm the combined firmware and settings
   restoration.
5. Keep power and USB connected until the readback result appears.

The updater verifies metadata and data hashes, filesystem permissions, the
ESP32-S3 device identity, and the current NVS offset and size before writing.
It then stores the current NVS as a separate safety snapshot, installs the selected
full-flash firmware, writes the selected backup, reads the complete settings
partition back, and compares every byte. A backup from another controller or an
incompatible target layout is refused before erase.

The updater reads and verifies settings before every destructive operation. It
does not store duplicate raw data when the controller, NVS layout, and content
SHA-256 match. The earliest verified operation snapshot with known source
firmware remains the fixed factory baseline for each controller and NVS layout;
later snapshots remain only as audit evidence.

## Results

- **Operation completed successfully** means the requested write and readback
  checks completed. After full-flash installation, press RESET and select
  **Inspect** to check the running firmware separately.
- **Write completed; controller startup requires verification** means the write
  completed but custom startup, identity, or optional settings checks could not
  be confirmed. Preserve the backup and do not repeat an App write automatically.
- **Operation requires attention** before a write means the reported condition
  can be corrected and the full-flash action repeated.
- If firmware inspection is unavailable, the displayed firmware and interface
  values are cleared. The selected serial port is retained; ROM download mode or
  independently developed firmware may not implement the OSR inspection commands.
- A failure after full-flash erase begins requires the protected backup and
  recovery guidance shown by the application.

## Backup and Audit Files

Files are stored with owner-only permissions under:

```text
~/.local/state/osr-updater/
```

Use **Open backups** and **Open** beside Audit log to open the fixed state
folders. The state directory contains readable settings exports, complete NVS
snapshots, and append-only audit records. Keep each `.bin` snapshot with its
adjacent `.json` metadata file and the controller's service record.

## Support Information

When requesting assistance, provide the firmware filename and SHA-256, controller
identity shown by Inspect, audit path, backup paths, exact final message, and
whether power or USB changed during the operation.

Contact: [winter@osrbot.com](mailto:winter@osrbot.com)
