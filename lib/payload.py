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
import sys
import tempfile
from pathlib import Path


def extract_pkg(pkg: Path, dest: Path) -> Path:
    """Extract the Install app from an InstallAssistant.pkg into ``dest``.

    The pkg is a xar with two relevant members: a small ``Payload`` (the app
    skeleton, Apple **pbzx**-compressed) and a huge plain ``SharedSupport.dmg``.
    We decode the pbzx skeleton with our stdlib decoder (no external pbzx tool)
    and extract the dmg directly with bsdtar, then slot the dmg into the app's
    ``Contents/SharedSupport/``. Returns the assembled app dir.
    """
    dest.mkdir(parents=True, exist_ok=True)
    _extract_skeleton(pkg, dest)
    app = locate_install_app(dest)
    if app is None:
        raise RuntimeError(f"Could not find 'Install macOS *.app' after unpacking {pkg.name}.")
    shared = app / "Contents" / "SharedSupport"
    shared.mkdir(parents=True, exist_ok=True)
    _extract_member(pkg, "SharedSupport.dmg", shared / "SharedSupport.dmg")
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


def _extract_member(pkg: Path, member: str, out: Path) -> None:
    """Extract one plain xar member directly to a file (streams, no temp copy)."""
    with open(out, "wb") as fh:
        rc = subprocess.run(["bsdtar", "-xO", "-f", str(pkg), member], stdout=fh).returncode
    if rc:
        raise RuntimeError(f"extracting {member} from {pkg.name} failed (rc={rc})")


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
