"""Turn a downloaded InstallAssistant.pkg (or an existing Install app) into the
content slickstick writes onto the USB.

A createinstallmedia stick carries, at the volume root:
  - Install macOS <name>.app         (comes straight from the pkg)
  - .IAPhysicalMedia                 (a tiny plist createinstallmedia *generates*)
  - restore/                         (an empty directory)
  - .VolumeIcon.icns                 (cosmetic)

When the source is a reference stick we copy those markers verbatim (proven).
When the source is a pkg we *synthesize* .IAPhysicalMedia + restore/ from the
app's own version info (see docs/how-it-works.md — this path is not yet
boot-verified end to end).
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
from pathlib import Path


def extract_pkg(pkg: Path, dest: Path) -> Path:
    """Extract InstallAssistant.pkg on Linux and return the Install macOS app dir.

    InstallAssistant.pkg is a flat (xar) package whose Payload is a gzip cpio
    containing ./Applications/Install macOS <name>.app. We extract the outer
    archive, then any inner Payload, then locate the app.
    """
    dest.mkdir(parents=True, exist_ok=True)
    _bsdtar(pkg, dest)

    # Extract any nested Payload archives (the full-installer app lives inside).
    for payload in list(dest.rglob("Payload")) + list(dest.rglob("*.pax.gz")):
        try:
            _bsdtar(payload, dest)
        except subprocess.CalledProcessError:
            pass

    app = locate_install_app(dest)
    if app is None:
        raise RuntimeError(
            f"Could not find 'Install macOS *.app' after extracting {pkg.name}. "
            "The pkg layout may differ; try the --local source with an app you already have."
        )
    return app


def locate_install_app(root: Path) -> Path | None:
    """Find an 'Install macOS *.app' bundle directory under root."""
    for cand in root.rglob("Install macOS *.app"):
        if cand.is_dir() and (cand / "Contents").is_dir():
            return cand
    return None


def _bsdtar(archive: Path, dest: Path) -> None:
    subprocess.run(["bsdtar", "-x", "-f", str(archive), "-C", str(dest)], check=True)


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
def write_markers_from_app(app_on_volume: Path, volume_root: Path) -> None:
    """Synthesize the root markers for a pkg-sourced build (written as root)."""
    info = app_version_info(app_on_volume)
    fd, tmp = tempfile.mkstemp(suffix=".IAPhysicalMedia")
    try:
        with os.fdopen(fd, "wb") as fh:
            plistlib.dump(info, fh)
        subprocess.run(["sudo", "cp", tmp, str(volume_root / ".IAPhysicalMedia")], check=True)
    finally:
        os.unlink(tmp)
    subprocess.run(["sudo", "mkdir", "-p", str(volume_root / "restore")], check=True)


def copy_markers_from_stick(src_root: Path, volume_root: Path) -> list[str]:
    """Copy the real root markers from a createinstallmedia stick (written as root)."""
    copied = []
    for name in (".IAPhysicalMedia", ".VolumeIcon.icns", "restore"):
        src = src_root / name
        if src.exists():
            subprocess.run(["sudo", "cp", "-a", str(src), str(volume_root) + "/"], check=True)
            copied.append(name)
    return copied
