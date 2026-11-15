"""USB build core: partition, format, copy, verify — with safety guards.

This encodes the method proven on Apple Silicon (see docs/how-it-works.md):
GPT (empty ESP + Apple HFS+) -> non-journaled HFS+ -> faithful content copy ->
fsck. No HFS+ bless or CNID work is needed on Apple Silicon; the firmware keys
on the installer content, not volume metadata.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import payload

HFS_TYPE = "AF00"   # sgdisk alias: Apple HFS/HFS+
ESP_TYPE = "EF00"   # sgdisk alias: EFI system partition


class BuildError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Device discovery + safety
# --------------------------------------------------------------------------- #
def list_removable_disks() -> list[dict]:
    """Return removable, non-mounted-as-root USB disks as lsblk dicts."""
    out = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,TRAN,RM,MODEL,LABEL,MOUNTPOINT"],
        capture_output=True, text=True, check=True,
    ).stdout
    disks = []
    for dev in json.loads(out).get("blockdevices", []):
        if dev.get("type") == "disk" and (dev.get("rm") or dev.get("tran") == "usb"):
            disks.append(dev)
    return disks


def assert_safe_target(target: str, source_disk: str | None = None) -> None:
    """Refuse obviously dangerous targets. Raises BuildError if unsafe."""
    name = Path(target).name
    sysfs = Path(f"/sys/block/{name}")
    if not sysfs.exists():
        raise BuildError(f"{target} is not a whole-disk device.")
    if (sysfs / "removable").read_text().strip() != "1":
        # allow USB-attached non-removable, but block internal disks hard
        tran = _tran(name)
        if tran != "usb":
            raise BuildError(f"{target} is not removable and not USB ({tran}). Refusing.")
    # never the disk our source came from
    if source_disk and Path(source_disk).name == name:
        raise BuildError(f"{target} is the same disk as the source. Refusing.")
    # never a disk that carries a mounted root/boot
    for part in sysfs.glob(f"{name}*"):
        mp = _mountpoint(part.name)
        if mp in ("/", "/boot", "/home", "/boot/efi"):
            raise BuildError(f"{target} has a partition mounted at {mp}. Refusing.")


def _tran(name: str) -> str:
    out = subprocess.run(["lsblk", "-dno", "TRAN", f"/dev/{name}"],
                         capture_output=True, text=True).stdout.strip()
    return out


def _mountpoint(part: str) -> str:
    return subprocess.run(["lsblk", "-no", "MOUNTPOINT", f"/dev/{part}"],
                          capture_output=True, text=True).stdout.strip()


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def _run(cmd: list[str]) -> None:
    print("  $ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def partition(target: str, hfs_label: str) -> tuple[str, str]:
    """Wipe target, write GPT (ESP + Apple HFS+). Return (esp_part, hfs_part)."""
    _run(["sudo", "wipefs", "-a", target])
    _run(["sudo", "sgdisk", "-Z", target])
    _run(["sudo", "sgdisk", "-n", "1:0:+200M", "-t", f"1:{ESP_TYPE}",
          "-c", "1:EFI System Partition", target])
    _run(["sudo", "sgdisk", "-n", "2:0:0", "-t", f"2:{HFS_TYPE}", "-c", "2:", target])
    _run(["sudo", "partprobe", target])
    subprocess.run(["sudo", "sync"])
    p = "p" if target[-1].isdigit() else ""
    return f"{target}{p}1", f"{target}{p}2"


def format_parts(esp: str, hfs: str, hfs_label: str, journaled: bool = False) -> None:
    _run(["sudo", "mkfs.fat", "-n", "EFI", esp])
    mk = ["sudo", "mkfs.hfsplus", "-v", hfs_label]
    if journaled:
        mk.append("-J")
    mk.append(hfs)
    _run(mk)


def _mount(dev: str, mp: Path, ro: bool = False, force: bool = False) -> None:
    mp.mkdir(parents=True, exist_ok=True)
    opts = "ro" if ro else ("rw,force" if force else "rw")
    _run(["sudo", "mount", "-t", "hfsplus", "-o", opts, dev, str(mp)])


def _umount(mp: Path) -> None:
    subprocess.run(["sudo", "umount", str(mp)])


def copy_installer(hfs_part: str, app_dir: Path, work: Path,
                   reference_root: Path | None = None) -> None:
    """Copy the Install app onto the HFS+ volume and lay the root markers.

    If reference_root (a mounted createinstallmedia stick) is given, its real
    markers are copied (the proven path). Otherwise markers are synthesized from
    the app (the pkg path — see docs: not yet boot-verified end to end).
    """
    dst = work / "dst_rw"
    _mount(hfs_part, dst, ro=False)
    try:
        print(f"  copying {app_dir.name} (this is the slow part)")
        _run(["sudo", "cp", "-a", str(app_dir), str(dst) + "/"])
        app_on_vol = dst / app_dir.name
        if reference_root is not None:
            copied = payload.copy_markers_from_stick(reference_root, dst)
            print(f"  copied real root markers from reference stick: {copied}")
        else:
            payload.write_markers_from_app(app_on_vol, dst)
            print("  synthesized root markers (.IAPhysicalMedia, restore/) from app")
        subprocess.run(["sudo", "sync"])
    finally:
        _umount(dst)


def verify(hfs_part: str) -> bool:
    print("  fsck.hfsplus:")
    rc = subprocess.run(["sudo", "fsck.hfsplus", "-f", hfs_part]).returncode
    return rc == 0


def build(target: str, app_dir: Path, work: Path, hfs_label: str,
          journaled: bool = False, reference_root: Path | None = None) -> None:
    """Full Apple Silicon build pipeline."""
    print(">>> [1/4] Partitioning (GPT: ESP + Apple HFS+)")
    esp, hfs = partition(target, hfs_label)
    print(">>> [2/4] Formatting (empty ESP + HFS+)")
    format_parts(esp, hfs, hfs_label, journaled=journaled)
    print(">>> [3/4] Copying installer content")
    copy_installer(hfs, app_dir, work, reference_root=reference_root)
    print(">>> [4/4] Verifying")
    ok = verify(hfs)
    print("Done." if ok else "Done (fsck reported issues — inspect before use).")


# --------------------------------------------------------------------------- #
# Intel (EXPERIMENTAL — see docs/how-it-works.md)
# --------------------------------------------------------------------------- #
def build_intel(target: str, app_dir: Path, work: Path, hfs_label: str) -> None:
    """EXPERIMENTAL and UNVERIFIED. Intel Macs boot via EFI (boot.efi in
    .IABootFiles) plus a real HFS+ bless — unlike Apple Silicon. We have no
    Intel Mac to test this on; treat output as a research artifact."""
    from . import bless

    print("!! EXPERIMENTAL Intel path — not verified to boot. See docs/how-it-works.md")
    esp, hfs = partition(target, hfs_label)
    format_parts(esp, hfs, hfs_label, journaled=False)
    copy_installer(hfs, app_dir, work, reference_root=None)

    # Attempt the Intel-specific bless: point finderInfo at a boot.efi if present.
    folder_cnid = file_cnid = None
    dst = work / "dst_rw"
    _mount(hfs, dst, ro=False)
    try:
        boot_efi = next(dst.rglob("boot.efi"), None)
        if boot_efi is None:
            print("!! No boot.efi found in the app — Intel media needs .IABootFiles; "
                  "this build will almost certainly NOT boot. Documented gap.")
        else:
            folder_cnid = bless.cnid(boot_efi.parent)
            file_cnid = bless.cnid(boot_efi)
            print(f"!! Blessing (experimental): folder={folder_cnid} file={file_cnid}")
    finally:
        _umount(dst)
    # Patch finderInfo on the unmounted volume if we found a boot file.
    if file_cnid is not None:
        bless.patch_finderinfo(hfs, {0: folder_cnid, 1: file_cnid, 5: folder_cnid})
    verify(hfs)
    print("Intel build finished (EXPERIMENTAL). Test on Intel hardware and report back.")
