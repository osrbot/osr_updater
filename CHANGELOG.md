# Changelog

All notable user-visible changes to OSR Updater are documented in this file.

## [Unreleased]

## [1.0.1] - 2026-08-18

### Fixed

- Reports when ROS or another program is using the selected controller port
  before a firmware operation starts.
- Does not report a managed application update as successful unless the target
  controller reaches its `READY` runtime state.

## [1.0.0] - 2026-08-13

### Changed

- Application updates now remain entirely on the running-firmware App OTA path
  and never request ESP32-S3 ROM recovery mode.
- Full-flash installation now always captures the current complete NVS partition
  and automatically restores it when the source and target layouts match.
- Full-flash recovery now follows ESP32-S3 USB serial re-enumeration instead of
  assuming the running-firmware device path remains available in ROM mode.
- Removed the operator-facing factory-state choice.
- Combined full-flash backup, final confirmation, erase, installation, and
  compatible settings restoration into one guided user action.
- Removed typed confirmation phrases, added automatic controller-port selection,
  and added continuous progress for full-flash erase, write, restore, and verify.
- Increased the default window size and added a persistent activity-log scrollbar.
- Replaced long backup and audit paths in the main window with direct folder-open
  actions.
- Combined controller recovery into one firmware-and-settings workflow with a
  verified, descriptive backup list and automatic recommendation.
- Moved controller-port and backup discovery away from the initial window path
  and switched the packaged application to a faster directory-based runtime.
- Reused identical controller-bound settings data and retained one verified
  baseline per controller and settings layout.
- Filtered recovery snapshots automatically by the selected full-flash
  firmware's NVS layout.
- Preserved an explicitly selected serial device during inspection and cleared
  stale firmware information when inspection is unavailable.
- Renamed controller recovery to **Factory Restore**, aligned all three action
  buttons, and fixed the earliest verified controller settings as its factory
  baseline.
- Displayed the actual ESP32-S3 ROM serial device as soon as it is discovered.
- Completed full-flash operations immediately after verified write/readback and
  replaced protocol reconnection waiting with a direct RESET-and-inspect prompt.
- Cleared stale runtime identity after full-flash and refreshed serial devices
  automatically after the manual RESET prompt.
- Identified ESP32-S3 support in the application title.

### Added

- Native English and Simplified Chinese desktop interface for Linux arm64.
- External ESP32-S3 application and full-flash image selection with structural
  validation, bootloader integrity checks, and SHA-256 display.
- Controller-bound complete NVS snapshot before every full-flash installation.
- Progress reporting, private audit records, backup-path display, and explicit
  final results.
- Guarded full-flash installation for standard and custom partition layouts,
  with automatic compatible settings restoration and readback verification.
- Verified controller restoration using a selected full-flash firmware and saved
  settings, with controller and layout binding, a pre-restore safety snapshot,
  and byte-for-byte readback.
- Optional post-install OSR settings comparison, allowing independently developed
  firmware that does not implement the OSR settings protocol.
- Debian package for application-menu installation on Jetson systems.
- Corresponding source archives, exact build information, and third-party
  license materials inside the Debian package.

### Security

- Firmware files are not embedded in the application or source repository.
- Backup and audit files are created outside the source tree with restrictive
  owner-only permissions.
- Uncertain write outcomes fail closed and preserve recovery evidence.

[1.0.1]: https://github.com/osrbot/osr_updater/releases/tag/v1.0.1
[1.0.0]: https://github.com/osrbot/osr_updater/releases/tag/v1.0.0
