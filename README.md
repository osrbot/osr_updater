# OSR Updater

<!-- markdownlint-disable MD013 MD033 -->

<p align="center">
  <a href="./README.md">English</a> ·
  <a href="./README_zh.md">简体中文</a>
</p>

<p align="center">
  <img src="packaging/osr-updater.svg" alt="OSR Updater" width="112">
</p>

<p align="center">
  <strong>Native firmware installation and controller recovery for OSRBOT vehicles.</strong>
</p>

OSR Updater is a bilingual graphical application for installing firmware files
supplied by OSRBOT. It validates the selected file, backs up vehicle settings,
shows installation progress, and reports a clear final result. Firmware is not
embedded in the application.

## Features

- Native English and Simplified Chinese interface
- Automatic identification of ESP32-S3 application and full-flash images
- Optional readable settings export before App OTA
- Complete controller-bound vehicle-settings backup before every full-flash installation
- Application-only updates that do not erase the settings partition
- Guarded full-flash installation for standard and independently developed layouts
- Automatic settings restoration only when controller identity and partition
  layout match exactly
- Verified manual restoration from a selected vehicle-settings backup
- Direct access to backup and audit folders, SHA-256 information, progress, and
  final status
- Private audit and backup files with restrictive filesystem permissions
- Single Linux arm64 desktop package for Jetson systems

## Install

Download the current `arm64.deb` package and its checksum from
[GitHub Releases](https://github.com/osrbot/osr_updater/releases). Verify the
checksum, then install the package with the Ubuntu package installer or:

```bash
sudo apt install ./osr-updater_<version>_arm64.deb
```

Open **OSR Updater** from the application menu. The application does not expose
a web interface or a user command-line workflow.

## Standard Update

1. Stop ROS nodes and other software using the controller serial device.
2. Keep the vehicle stationary and connect stable power.
3. Open OSR Updater and inspect the controller.
4. Select the application firmware file supplied for the vehicle.
5. Review the file name, type, size, and SHA-256 value.
6. Choose **Install firmware** and confirm the operation.
7. Keep the connection and power unchanged until the final result is displayed.

App OTA never enters ROM recovery mode and never erases or rewrites the settings
partition. When the running firmware exposes the settings export interface, the
updater also stores a readable diagnostic backup and compares it after restart.
Use **Open backups** in the interface to view stored backups.

Application update requires the currently running controller firmware to expose
the OSR App OTA commands. If independently developed firmware no longer exposes
that interface, use a full-flash image instead.

## Full-Flash Installation

Full-flash installation is used for controller recovery or an independently
developed merged image that does not use App OTA. One guided action performs
the following steps:

1. validates the selected ESP32-S3 full-flash image;
2. enters ROM recovery mode, follows USB serial re-enumeration automatically,
   and stores the complete current settings partition before any erase;
3. binds the backup to the connected controller;
4. requires a separate final destructive confirmation;
5. erases and installs the selected image;
6. automatically restores the saved settings only when device identity and partition
   offset and size match, then verifies its byte-for-byte readback;
7. retains the backup without writing it when the target layout is incompatible;
8. reports the verified write result and asks the operator to restart and
   inspect the controller.

After a full-flash write, press RESET once when prompted so the installed
firmware can start. Full-flash completion does not wait for an OSR protocol
response because an independently developed image may not implement one.

The selected full-flash image is written exactly, including any data it carries
in its own partition layout. All previous flash data is replaced. Open the
backup folder and confirm that the current settings snapshot is present before
accepting the final confirmation.

## Factory Restore

Select the required full-flash firmware. The updater filters saved vehicle
settings by the selected firmware's NVS layout and presents the fixed factory
baseline for the matching controller and layout. **Factory Restore** verifies
the firmware, backup, controller identity, and target layout before it erases
anything. It saves the current settings as a safety backup, installs the selected
firmware, restores the factory baseline, and verifies the result by byte-for-byte
readback.

Identical raw settings from the same controller and partition layout reuse one
verified data file. The earliest verified operation snapshot with known source
firmware remains the factory baseline for that controller and settings layout;
later safety records remain available only as audit evidence.

See the [User Guide](docs/usage.md) for the complete workflow and recovery
instructions.

## Firmware Files

OSR Updater does not include firmware. Use only a file supplied or explicitly
approved by OSRBOT for the controller being serviced. The application validates
file structure and transfer integrity; it cannot certify the functional
behavior of independently modified firmware.

## Troubleshooting

| Symptom | Likely cause | Recommended action |
| --- | --- | --- |
| Controller cannot be opened | ROS or another utility owns the serial device | Stop the other process, reconnect USB, and inspect again. |
| App update requests full-flash installation | The running firmware does not expose OSR App OTA | Select a full-flash image, enter recovery mode with BOOT/RESET, and use full-flash installation. |
| Readable settings export is unavailable | The running firmware does not expose the OSR settings interface | App OTA still preserves NVS; post-update parameter comparison is reported as unavailable. |
| Installation is refused before transfer | Power, motion, image, or device preflight failed | Correct the reported condition and restart the operation. |
| Result says not to retry | Application delivery or recovery state is uncertain | Preserve the audit and backup paths and contact OSRBOT before another write. |
| Controller is unavailable after full-flash | The controller still needs a manual restart, or the installed firmware does not implement the OSR interface | Press RESET once, select the detected runtime port if needed, and inspect again. |

## Development

The source requires Python 3.10 or later, Tk 8.6, pySerial, and esptool.

```bash
python3 -m unittest discover -s tests -v
```

Release packages are built on Linux arm64:

```bash
./build.sh
```

The build produces one Debian package and a matching SHA-256 file. No firmware
payload is added to the package.

## Releases and Support

- [Release notes](CHANGELOG.md)
- [User Guide](docs/usage.md)
- [GitHub Issues](https://github.com/osrbot/osr_updater/issues)
- Technical support: [winter@osrbot.com](mailto:winter@osrbot.com)

## Authors

- Zhihao ZHANG
- Kit So
- Jintai WANG
- dajianli

## License

The OSR Updater source is available under the [MIT License](LICENSE). The
self-contained executable includes esptool and is distributed under
GPL-2.0-or-later. The Debian package includes the applicable dependency license
files, exact build information, and corresponding source archives. See
[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
