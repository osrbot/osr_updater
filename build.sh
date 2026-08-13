#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
host_os="$(uname -s)"
host_arch="$(uname -m)"

if [[ "$host_os" != "Linux" || "$host_arch" != "aarch64" ]]; then
  echo "OSR Updater release builds require Linux aarch64." >&2
  exit 2
fi

if [[ -n "$(git -C "$project_root" status --porcelain)" ]]; then
  echo "OSR Updater release builds require a clean Git worktree." >&2
  exit 2
fi

release_python="${OSR_UPDATER_PYTHON:-python3}"

"$release_python" - <<'PY'
import sys
import tkinter
assert sys.version_info[:2] == (3, 10)
assert tkinter.TkVersion >= 8.6
PY

build_root="$(mktemp -d "${TMPDIR:-/tmp}/osr-updater.XXXXXX")"

cleanup() {
  rm -rf -- "$build_root"
}
trap cleanup EXIT

"$release_python" -m venv "$build_root/venv"
"$build_root/venv/bin/python" -m pip install --disable-pip-version-check \
  --requirement "$project_root/requirements-build.txt"

rm -rf -- "$project_root/dist/osr-updater" "$build_root/pyinstaller" \
  "$build_root/pyinstaller-dist" "$build_root/deb"
mkdir -p "$project_root/dist/osr-updater" "$build_root/pyinstaller" \
  "$build_root/pyinstaller-dist" "$build_root/deb"

"$build_root/venv/bin/python" \
  "$project_root/generate_build_info.py" \
  --root "$project_root" \
  --output "$build_root/BUILD_INFO.json"

(
  cd "$project_root"
  OSR_UPDATER_BUILD_INFO="$build_root/BUILD_INFO.json" \
    "$build_root/venv/bin/python" -m PyInstaller \
    --clean \
    --noconfirm \
    --distpath "$build_root/pyinstaller-dist" \
    --workpath "$build_root/pyinstaller" \
    osr_updater.spec
)

artifact_root="$build_root/pyinstaller-dist/osr-updater"
artifact="$artifact_root/osr-updater"
test -x "$artifact"
OSR_UPDATER_SELF_TEST=1 "$artifact"

version="$(
  cd "$project_root"
  "$build_root/venv/bin/python" -c \
    'from osr_updater import __version__; print(__version__)'
)"
package_root="$build_root/deb/osr-updater_${version}_arm64"
mkdir -p "$package_root/DEBIAN" "$package_root/usr/bin" \
  "$package_root/usr/lib/osr-updater" \
  "$package_root/usr/share/applications" \
  "$package_root/usr/share/icons/hicolor/scalable/apps" \
  "$package_root/usr/share/doc/osr-updater"
cp -a "$artifact_root/." "$package_root/usr/lib/osr-updater/"
ln -s ../lib/osr-updater/osr-updater "$package_root/usr/bin/osr-updater"
install -m 0644 "$project_root/packaging/osr-updater.desktop" \
  "$package_root/usr/share/applications/osr-updater.desktop"
install -m 0644 "$project_root/packaging/osr-updater.svg" \
  "$package_root/usr/share/icons/hicolor/scalable/apps/osr-updater.svg"
install -m 0644 "$project_root/LICENSE" \
  "$package_root/usr/share/doc/osr-updater/SOURCE_LICENSE"
install -m 0644 "$project_root/THIRD_PARTY_NOTICES.txt" \
  "$package_root/usr/share/doc/osr-updater/THIRD_PARTY_NOTICES.txt"
install -m 0644 "$build_root/BUILD_INFO.json" \
  "$package_root/usr/share/doc/osr-updater/BUILD_INFO.json"
test -r /usr/share/common-licenses/GPL-2
install -m 0644 /usr/share/common-licenses/GPL-2 \
  "$package_root/usr/share/doc/osr-updater/COPYING"
licenses_root="$package_root/usr/share/doc/osr-updater/licenses"
mkdir -p "$licenses_root"
install -m 0644 "$project_root/licenses/pyserial-LICENSE.txt" \
  "$licenses_root/pyserial-LICENSE.txt"
source_root="$package_root/usr/share/doc/osr-updater/source"
mkdir -p "$source_root"
git -C "$project_root" archive --format=tar.gz \
  --prefix="osr-updater-${version}/" \
  --output="$source_root/osr-updater-${version}.tar.gz" HEAD
"$release_python" - "$source_root" <<'PY'
from pathlib import Path
from urllib.request import urlopen
import sys

destination = Path(sys.argv[1])
sources = {
    "esptool-5.3.1.tar.gz": (
        "https://files.pythonhosted.org/packages/76/ac/"
        "d2016cf6b3709d0e0166f45f84bc6e2d717757b5f59020ccb34de08d1b9b/"
        "esptool-5.3.1.tar.gz"
    ),
    "pyinstaller-6.22.0.tar.gz": (
        "https://files.pythonhosted.org/packages/05/03/"
        "669d06735cf57d7e2e5dfc3c2e1643554b34c559eff21449c58172ca0335/"
        "pyinstaller-6.22.0.tar.gz"
    ),
}
for filename, url in sources.items():
    with urlopen(url, timeout=60) as response:
        data = response.read()
    (destination / filename).write_bytes(data)
PY
(
  cd "$source_root"
  test "$(sha256sum esptool-5.3.1.tar.gz | cut -d' ' -f1)" = \
    125781f36e6a2d08c484524a45f340694675368b5eeead9d0cb21b2034a91d98
  test "$(sha256sum pyinstaller-6.22.0.tar.gz | cut -d' ' -f1)" = \
    8b0166fff4583b374bbe7fa044bf03ddc33cab0f792a665807d96048e63f060e
  sha256sum ./*.tar.gz > SHA256SUMS
)
"$build_root/venv/bin/python" - "$licenses_root" <<'PY'
from importlib.metadata import distributions
from pathlib import Path
import shutil
import sys

destination = Path(sys.argv[1])
copied = 0
for distribution in sorted(
    distributions(), key=lambda item: (item.metadata.get("Name") or "").lower()
):
    name = distribution.metadata.get("Name") or "unknown"
    version = distribution.version
    target = destination / f"{name}-{version}"
    for item in distribution.files or ():
        basename = Path(str(item)).name.lower()
        if not basename.startswith(("license", "copying", "notice")):
            continue
        source = Path(distribution.locate_file(item))
        if not source.is_file():
            continue
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target / Path(str(item)).name)
        copied += 1
if copied == 0:
    raise SystemExit("no dependency license files were collected")
PY
sed "s/@VERSION@/$version/g" "$project_root/packaging/debian-control.in" \
  > "$package_root/DEBIAN/control"

deb="$project_root/dist/osr-updater/osr-updater_${version}_arm64.deb"
dpkg-deb --build --root-owner-group "$package_root" "$deb"
(
  cd "$project_root/dist/osr-updater"
  sha256sum "$(basename "$deb")" > "$(basename "$deb").sha256"
)

echo "Built: $deb"
cat "$deb.sha256"
