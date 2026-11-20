"""Turn a downloaded InstallAssistant.pkg (or an existing Install app) into the
content slickstick writes onto the USB.

A createinstallmedia stick carries, at the volume root:
  - Install macOS <name>.app         (comes straight from the pkg)
  - .IAPhysicalMedia                 (a tiny plist createinstallmedia *generates*)
  - restore/                         (an empty directory)
  - .VolumeIcon.icns                 (cosmetic)

When the source is a reference stick we copy those markers verbatim. When the
source is a pkg we *synthesize* .IAPhysicalMedia + restore/ from the app's own
version info (see docs/how-it-works.md); both are boot-verified on Apple Silicon.
"""
from __future__ import annotations

import os
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from . import ui


def extract_pkg(pkg: Path, dest: Path) -> Path:
    """Extract the Install app from an InstallAssistant.pkg into ``dest``.

    The pkg is itself a "pkgdmg": a xar package (members ``Payload`` — the app
    skeleton — and ``SharedSupport.dmg``) with a UDIF/koly trailer appended. We
    decode the ``Payload`` skeleton with our stdlib decoder (no external pbzx
    tool), then place the **whole pkg** as the app's
    ``Contents/SharedSupport/SharedSupport.dmg`` — that is what createinstallmedia
    does and what startosinstall expects (see the note in the body). Returns the
    assembled app dir.
    """
    dest.mkdir(parents=True, exist_ok=True)
    _extract_skeleton(pkg, dest)
    app = locate_install_app(dest)
    if app is None:
        raise RuntimeError(f"Could not find 'Install macOS *.app' after unpacking {pkg.name}.")
    shared = app / "Contents" / "SharedSupport"
    shared.mkdir(parents=True, exist_ok=True)
    dmg = shared / "SharedSupport.dmg"
    # The on-stick SharedSupport.dmg must be the WHOLE InstallAssistant.pkg — it
    # is itself a "pkgdmg" (a xar package with a UDIF/koly trailer appended), not
    # the inner SharedSupport.dmg member. startosinstall opens this file expecting
    # the pkg and reads the real dmg's offset *inside* it ("Getting offset for dmg
    # in pkg"); handed the bare inner member it rejects the media with "pkgdmg is
    # missing a footer" (proven on real hardware 2026-11-17).
    size = pkg.stat().st_size
    ui.info(f"placing SharedSupport.dmg (full pkgdmg) — {ui.human(size)}")
    cmd = ["bash", "-c", f"cat {shlex.quote(str(pkg))} > {shlex.quote(str(dmg))}"]
    rc = ui.run_with_progress(cmd, size, ui.measure_file(str(dmg)), label="dmg")
    if rc:
        raise RuntimeError(f"copying SharedSupport.dmg (pkgdmg) from {pkg.name} failed (rc={rc})")
    return app


def _extract_skeleton(pkg: Path, dest: Path) -> None:
    """bsdtar(Payload) | pbzx-decode | bsdtar -x  — writes the app skeleton."""
    repo_root = Path(__file__).resolve().parent.parent
    p1 = subprocess.Popen(["bsdtar", "-xO", "-f", str(pkg), "Payload"], stdout=subprocess.PIPE)
    p2 = subprocess.Popen([sys.executable, "-m", "lib.pbzx"],
                          stdin=p1.stdout, stdout=subprocess.PIPE, cwd=str(repo_root))
    p3 = subprocess.Popen(["bsdtar", "-x", "-C", str(dest)], stdin=p2.stdout)
    p1.stdout.close()
    p2.stdout.close()
    p3.communicate()
    p1.wait()
    p2.wait()
    if p1.returncode or p2.returncode or p3.returncode:
        raise RuntimeError(f"skeleton extraction failed "
                           f"(bsdtar={p1.returncode}, pbzx={p2.returncode}, bsdtar={p3.returncode})")


def locate_install_app(root: Path) -> Path | None:
    """Find an 'Install macOS *.app' bundle directory under root."""
    for cand in root.rglob("Install macOS *.app"):
        if cand.is_dir() and (cand / "Contents").is_dir():
            return cand
    return None



def app_version_info(app: Path) -> dict[str, str]:
    """Best-effort read of ProductVersion / build / name for .IAPhysicalMedia.

    SharedSupport holds the authoritative version; without mounting its dmg we
    fall back to the app's Info.plist and bundle name. Good enough to synthesize
    the marker; the exact build string may be approximate on the pkg path.
    """
    info: dict[str, str] = {"AppName": app.name}
    plist = app / "Contents" / "Info.plist"
    try:
        with open(plist, "rb") as fh:
            data = plistlib.load(fh)
        # DTPlatformVersion is like "27.2"; ProductVersion on media is "27.2.1".
        if "DTPlatformVersion" in data:
            info["ProductVersion"] = str(data["DTPlatformVersion"])
    except (OSError, plistlib.InvalidFileException):
        pass
    return info


# The destination volume is mounted by root, so all writes to it go through sudo
# (same as the installer-app copy) — a plain Python write would be EACCES.
def pkg_build_version(pkg: Path) -> str | None:
    """Decode the macOS build from the pkg's PackageInfo version.

    The full-installer pkg encodes the build as a dotted version, e.g.
    ``26.1.131`` -> ``26B131`` (minor 1 -> letter 'B'). Returns None if absent.
    """
    out = subprocess.run(["bsdtar", "-xO", "-f", str(pkg), "PackageInfo"],
                         capture_output=True, text=True).stdout
    m = re.search(r'version="(\d+)\.(\d+)\.(\d+)"', out)
    if not m:
        return None
    major, minor, patch = m.groups()
    return f"{major}{chr(ord('A') + int(minor))}{patch}"


def pkg_app_name(pkg: Path) -> str | None:
    """Installer name (without .app) from the pkg's PackageInfo bundle path, e.g.
    'Install macOS 27 Golden Gate'. Used as the volume label when streaming
    (the app isn't extracted to disk, so we can't read its name that way)."""
    out = subprocess.run(["bsdtar", "-xO", "-f", str(pkg), "PackageInfo"],
                         capture_output=True, text=True).stdout
    m = re.search(r'bundle path="\./Applications/(.+?)\.app"', out)
    return m.group(1) if m else None


def short_os_name(name: str | None) -> str:
    """'Install macOS Sonoma' / None -> 'Sonoma' (falls back to 'macOS')."""
    return (name or "macOS").replace("Install macOS ", "").strip() or "macOS"


def firmware_check_script(app_name: str | None) -> str:
    """Return a self-contained shell script for the Intel installer stick that
    reports whether the target Mac is a T2 (whose firmware updates during install
    can require the network) and gives honest, actionable advice. The user runs it
    in the macOS Recovery Terminal before installing.

    It deliberately does NOT try to predict offline-safety from version numbers:
    real data showed that gives wrong answers — a Sonoma install failed offline at
    a bridgeOS the version math called "new enough," because a same-era *point*
    bump was required (Apple decides at install time; see docs/intel.md). So it
    shows the firmware for reference only and gives the one always-correct rule:
    have the network available; it's used only if an update is actually needed.
    ``app_name`` (e.g. "Install macOS Sonoma") is used only for the header.
    """
    name = short_os_name(app_name)
    return f'''#!/bin/bash
# SlickStick firmware-check: will installing {name} on THIS Mac need the internet?
# Run it in the macOS Recovery Terminal before installing.
set -u
echo "== SlickStick firmware check =="
echo "This installer: {name}"
echo
RC=/usr/libexec/remotectl
if [ ! -x "$RC" ]; then
  echo "remotectl not present -- almost certainly a PRE-T2 Intel Mac."
  echo "Pre-T2 Macs have no T2 firmware and install FULLY OFFLINE. You are good."
  exit 0
fi
BV="$("$RC" dumpstate 2>/dev/null | grep -i BridgeVersion)"
if [ -z "$BV" ]; then
  echo "No T2 bridge detected -- looks like a pre-T2 Mac. Installs fully offline."
  exit 0
fi
echo "This Mac has a T2 chip. Firmware (bridgeOS) versions it reports:"
echo "$BV" | sed 's/^/   /'
echo "(The T2 reports more than one firmware-related version; shown for reference.)"
echo
echo "What this means:"
echo " - A T2 updates its firmware (bridgeOS) during a macOS install, and that"
echo "   update is fetched from Apple -- it cannot be bundled on the USB."
echo " - Whether THIS install needs an update is decided by Apple at install time"
echo "   and cannot be reliably predicted here: the version numbers do not tell the"
echo "   whole story (a same-version point update can still be required)."
echo
echo "Bottom line: have Ethernet or Wi-Fi available before you install."
echo "It is used ONLY if a firmware update is actually needed, harmless otherwise."
echo "If the install stalls near 50% at \\"An error occurred installing macOS\\","
echo "connect to the internet and retry -- that is the firmware step."
'''


def write_markers_from_app(app_on_volume: Path, volume_root: Path,
                           build_version: str | None = None) -> None:
    """Synthesize the root markers for a pkg-sourced build (written as root).

    Apple Silicon's startup picker lists a volume only if its root
    ``.IAPhysicalMedia`` carries all three of ``AppName``, ``ProductVersion`` and
    ``ProductBuildVersion`` (see docs/how-it-works.md) — so the build version is
    required, not optional. The icon is cosmetic but copied so the volume shows
    the macOS logo in the picker.
    """
    info = app_version_info(app_on_volume)          # {AppName, ProductVersion}
    if build_version:
        info["ProductBuildVersion"] = build_version
    else:
        ui.warn("no build version available — the stick may not be recognized "
                "(synthesized markers need ProductBuildVersion; use a pkg source)")

    fd, tmp = tempfile.mkstemp(suffix=".IAPhysicalMedia")
    try:
        with os.fdopen(fd, "wb") as fh:
            plistlib.dump(info, fh)
        subprocess.run(["sudo", "cp", tmp, str(volume_root / ".IAPhysicalMedia")], check=True)
    finally:
        os.unlink(tmp)
    subprocess.run(["sudo", "mkdir", "-p", str(volume_root / "restore")], check=True)

    # Cosmetic: give the volume the installer's icon in the boot picker. Remove any
    # existing entry first — a recovery BaseSystem ships .VolumeIcon.icns as a
    # *dangling* symlink, and cp would refuse to "write through" it.
    icns = app_on_volume / "Contents" / "Resources" / "InstallAssistant.icns"
    if icns.exists():
        subprocess.run(["sudo", "rm", "-f", str(volume_root / ".VolumeIcon.icns")])
        subprocess.run(["sudo", "cp", str(icns), str(volume_root / ".VolumeIcon.icns")])


def copy_markers_from_stick(src_root: Path, volume_root: Path) -> list[str]:
    """Copy the real root markers from a createinstallmedia stick (written as root)."""
    copied = []
    for name in (".IAPhysicalMedia", ".VolumeIcon.icns", "restore"):
        src = src_root / name
        if src.exists():
            subprocess.run(["sudo", "cp", "-a", str(src), str(volume_root) + "/"], check=True)
            copied.append(name)
    return copied
