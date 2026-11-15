"""stick source: clone from an inserted macOS installer stick.

The most faithful source: it copies the installer app AND the real root markers
(.IAPhysicalMedia, restore/, .VolumeIcon.icns) straight off a known-good
createinstallmedia stick — the path that has actually been boot-verified.

Detects HFS+ volumes labeled 'Install macOS …' on removable/USB disks, mounts
the chosen one read-only, and exposes its root as `marker_root` so the builder
copies the real markers rather than synthesizing them.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .base import Source
from .. import payload


def find_installer_volumes() -> list[dict]:
    """Return HFS+ partitions labeled 'Install macOS …' on removable/USB disks."""
    out = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "PATH,LABEL,FSTYPE,MOUNTPOINT,SIZE,TRAN,RM"],
        capture_output=True, text=True, check=True,
    ).stdout
    found: list[dict] = []

    def walk(dev: dict, parent_removable: bool) -> None:
        removable = parent_removable or dev.get("tran") == "usb" or bool(dev.get("rm"))
        label = dev.get("label") or ""
        if removable and dev.get("fstype") == "hfsplus" and label.startswith("Install macOS"):
            found.append({
                "path": dev["path"],
                "label": label,
                "size": int(dev.get("size") or 0),
                "mountpoint": dev.get("mountpoint"),
            })
        for child in dev.get("children") or []:
            walk(child, removable)

    for disk in json.loads(out).get("blockdevices", []):
        walk(disk, False)
    return found


class StickSource(Source):
    id = "stick"
    desc = "clone from an inserted macOS installer stick (real markers, proven)"

    def __init__(self, partition: str):
        self.partition = partition          # e.g. /dev/sdc2
        self._own_mount: str | None = None   # set if we mounted it ourselves

    def obtain(self, workdir: Path, version: str | None = None) -> Path:
        mp = self._current_mountpoint()
        if mp is None:
            mp = tempfile.mkdtemp(prefix="slickstick-stick-")
            subprocess.run(
                ["sudo", "mount", "-t", "hfsplus", "-o", "ro", self.partition, mp],
                check=True,
            )
            self._own_mount = mp
        app = payload.locate_install_app(Path(mp))
        if app is None:
            raise RuntimeError(f"No 'Install macOS *.app' found on {self.partition}")
        self.marker_root = Path(mp)         # builder copies the real markers from here
        return app

    def cleanup(self) -> None:
        if self._own_mount:
            subprocess.run(["sudo", "umount", self._own_mount])
            self._own_mount = None

    def _current_mountpoint(self) -> str | None:
        r = subprocess.run(
            ["lsblk", "-no", "MOUNTPOINT", self.partition],
            capture_output=True, text=True,
        ).stdout.strip()
        return r or None
