"""USB build core: partition, format, copy, verify — with safety guards.

This encodes the method proven on Apple Silicon (see docs/how-it-works.md):
GPT (empty ESP + Apple HFS+) -> non-journaled HFS+ -> faithful content copy ->
fsck. No HFS+ bless or CNID work is needed on Apple Silicon; the firmware keys
on the installer content, not volume metadata.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import payload, ui

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


def parent_disk(part: str) -> str:
    """'/dev/sdc2' -> '/dev/sdc' (whole disk) via lsblk PKNAME."""
    out = subprocess.run(["lsblk", "-no", "pkname", part],
                         capture_output=True, text=True).stdout.strip().splitlines()
    return f"/dev/{out[0]}" if out and out[0] else part


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
def ensure_sudo() -> None:
    """Authenticate sudo once, up front, before anything touches the disk.

    Fails cleanly (BuildError) if the user can't sudo, instead of blowing up
    halfway through with a traceback. No-op when already root.
    """
    if os.geteuid() == 0:
        return
    if shutil.which("sudo") is None:
        raise BuildError("This build needs root to partition the USB, but 'sudo' "
                         "isn't installed. Run as root, or install sudo.")
    print("\nSlickStick needs administrator access (sudo) to partition and format the USB.")
    if subprocess.run(["sudo", "-v"]).returncode != 0:
        raise BuildError("sudo authentication failed — make sure your user may run sudo.")


class SudoKeepAlive:
    """Keep sudo's timestamp fresh during a long build so it won't re-prompt
    mid-copy. Use as a context manager around the build."""

    def __init__(self, interval: int = 60):
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "SudoKeepAlive":
        if os.geteuid() != 0 and shutil.which("sudo"):
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            subprocess.run(["sudo", "-n", "-v"], capture_output=True)


def _run(cmd: list[str]) -> None:
    ui.cmd(shlex.join(cmd))   # quoted so the echoed line is copy-pasteable
    subprocess.run(cmd, check=True)


def _umount_path(part: str) -> None:
    """Unmount a partition, cooperating with the desktop auto-mounter.

    Prefer `udisksctl unmount`: it asks the same service that auto-mounted the
    device to release it, so it won't immediately re-mount, and needs no sudo for
    the user's own mounts. Fall back to umount (then lazy umount) if that fails
    or udisks isn't present.
    """
    if shutil.which("udisksctl"):
        if subprocess.run(["udisksctl", "unmount", "-b", part],
                          capture_output=True).returncode == 0:
            return
    if subprocess.run(["sudo", "umount", part]).returncode != 0:
        subprocess.run(["sudo", "umount", "-l", part])


def unmount_disk(disk: str) -> None:
    """Unmount every mounted partition of a whole disk so it isn't 'busy'."""
    out = subprocess.run(["lsblk", "-J", "-o", "PATH,MOUNTPOINT", disk],
                         capture_output=True, text=True).stdout
    def walk(dev: dict) -> None:
        if dev.get("mountpoint"):
            ui.info(f"unmounting {dev['path']} ({dev['mountpoint']})")
            _umount_path(dev["path"])
        for child in dev.get("children") or []:
            walk(child)
    for d in json.loads(out).get("blockdevices", []):
        walk(d)


def power_off(disk: str) -> bool:
    """Best-effort eject/power-off of a finished (already-unmounted) USB disk so it
    can be pulled safely. Flushes, then `udisksctl power-off` (no sudo, cooperates
    with the auto-mounter, like _umount_path). Returns True on success; False if it
    couldn't power the device off — it's unmounted and safe to remove either way."""
    subprocess.run(["sync"])
    if shutil.which("udisksctl"):
        return subprocess.run(["udisksctl", "power-off", "-b", disk],
                              capture_output=True).returncode == 0
    return False


def unmount_if_mounted(part: str) -> None:
    """Unmount a single partition if a desktop auto-mounted it."""
    mp = subprocess.run(["lsblk", "-no", "MOUNTPOINT", part],
                        capture_output=True, text=True).stdout.strip()
    if mp:
        ui.info(f"unmounting {part} ({mp})")
        _umount_path(part)


def partition(target: str, hfs_label: str) -> tuple[str, str]:
    """Wipe target, write GPT (ESP + Apple HFS+). Return (esp_part, hfs_part)."""
    unmount_disk(target)   # desktop may have auto-mounted its partitions
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
                   reference_root: Path | None = None,
                   build_version: str | None = None) -> None:
    """Copy the Install app onto the HFS+ volume and lay the root markers.

    If reference_root (a mounted createinstallmedia stick) is given, its real
    markers are copied (the proven path). Otherwise markers are synthesized from
    the app (the pkg path — see docs: not yet boot-verified end to end).
    """
    dst = work / "dst_rw"
    unmount_if_mounted(hfs_part)   # udisks may auto-mount the freshly formatted volume
    _mount(hfs_part, dst, ro=False)
    try:
        total = ui.du_bytes(str(app_dir))
        ui.info(f"copying {app_dir.name} — {ui.human(total)}")
        rc = ui.run_with_progress(
            ["sudo", "cp", "-a", str(app_dir), str(dst) + "/"],
            total, ui.measure_mount(str(dst)), label="copy",
        )
        if rc != 0:
            raise BuildError("copying the installer app failed")
        app_on_vol = dst / app_dir.name
        if reference_root is not None:
            copied = payload.copy_markers_from_stick(reference_root, dst)
            ui.ok(f"copied real root markers: {', '.join(copied)}")
        else:
            payload.write_markers_from_app(app_on_vol, dst, build_version)
            ui.ok("synthesized root markers (.IAPhysicalMedia + icon, restore/)")
        _flushing_note()
        subprocess.run(["sudo", "sync"])
    finally:
        _umount(dst)


def verify(hfs_part: str) -> bool:
    ui.info("fsck.hfsplus:")
    rc = subprocess.run(["sudo", "fsck.hfsplus", "-f", hfs_part]).returncode
    return rc == 0


def _find_app_on(root: Path) -> Path:
    out = subprocess.run(["sudo", "find", str(root), "-maxdepth", "1", "-iname", "Install macOS *.app"],
                         capture_output=True, text=True).stdout.strip().splitlines()
    if not out:
        raise BuildError("no 'Install macOS *.app' on target after streaming")
    return Path(out[0])


def stream_app_to_target(pkg: Path, dst: Path, repo_root: Path) -> Path:
    """Stream the app from a pkg straight onto the mounted target `dst`.

    Reads the pkg as the normal user (NFS-safe, no root-squash); only the writes
    to the root-mounted target go through sudo. The skeleton comes via the pbzx
    pipe; the big SharedSupport.dmg streams directly with a progress bar.
    """
    # 1. app skeleton -> target root (strip ./Applications/)
    p1 = subprocess.Popen(["bsdtar", "-xO", "-f", str(pkg), "Payload"], stdout=subprocess.PIPE)
    p2 = subprocess.Popen([sys.executable, "-m", "lib.pbzx"],
                          stdin=p1.stdout, stdout=subprocess.PIPE, cwd=str(repo_root))
    p3 = subprocess.Popen(["sudo", "bsdtar", "-x", "--strip-components", "2", "-C", str(dst)],
                          stdin=p2.stdout)
    p1.stdout.close()
    p2.stdout.close()
    p3.communicate()
    p1.wait()
    p2.wait()
    if p1.returncode or p2.returncode or p3.returncode:
        raise BuildError("streaming the app skeleton failed")

    app = _find_app_on(dst)
    shared = app / "Contents" / "SharedSupport"
    subprocess.run(["sudo", "mkdir", "-p", str(shared)], check=True)

    # 2. SharedSupport.dmg -> target, with progress (user reads pkg, root writes).
    # This file must be the WHOLE pkg (a "pkgdmg"), not the inner SharedSupport.dmg
    # member — else startosinstall rejects the media with "pkgdmg is missing a
    # footer" (proven on real hardware 2026-11-17; see payload.extract_pkg).
    dmg = shared / "SharedSupport.dmg"
    size = pkg.stat().st_size
    cmd = ["bash", "-c",
           f"cat {shlex.quote(str(pkg))} "
           f"| sudo tee {shlex.quote(str(dmg))} >/dev/null"]
    rc = ui.run_with_progress(cmd, size, ui.measure_mount(str(dst)), label="dmg")
    if rc:
        raise BuildError("streaming SharedSupport.dmg failed")
    return app


def _flushing_note() -> None:
    """Flag that the post-write flush + unmount is slow on USB flash, so a long,
    silent final stage doesn't look like a hang."""
    ui.info("Flushing buffered writes to the USB and unmounting — on slow flash this "
            "can take a few minutes; it's working, not stuck.")


def _write_firmware_check(dst: Path, pkg: Path, work: Path) -> None:
    """Drop a 'firmware-check' helper at the installer stick's root — a T2
    bridgeOS advisor the user can run in Recovery to see whether this install
    needs a one-time internet connection (see payload.firmware_check_script and
    docs/intel.md). Written at the volume root (never inside the .app bundle,
    which would break codesign). The HFS+ mount is root-owned, so via sudo."""
    script = payload.firmware_check_script(payload.pkg_app_name(pkg))
    local = work / "firmware-check"
    local.write_text(script)
    subprocess.run(["sudo", "cp", str(local), str(dst / "firmware-check")], check=True)
    subprocess.run(["sudo", "chmod", "0755", str(dst / "firmware-check")], check=True)


def build_streaming(target: str, pkg: Path, work: Path, hfs_label: str) -> None:
    """Write-only strategy: partition, then stream the app straight onto the USB
    with ~0 scratch space. Markers are synthesized from the streamed app."""
    repo_root = Path(__file__).resolve().parent.parent
    ui.step(1, 4, "Partitioning (GPT: ESP + Apple HFS+)")
    esp, hfs = partition(target, hfs_label)
    ui.step(2, 4, "Formatting (empty ESP + HFS+)")
    format_parts(esp, hfs, hfs_label)
    ui.step(3, 4, "Streaming installer onto the USB (no scratch space)")
    dst = work / "dst_rw"
    unmount_if_mounted(hfs)
    _mount(hfs, dst, ro=False)
    try:
        app = stream_app_to_target(pkg, dst, repo_root)
        payload.write_markers_from_app(app, dst, payload.pkg_build_version(pkg))
        ui.ok("synthesized root markers (.IAPhysicalMedia + icon, restore/)")
        _flushing_note()
        subprocess.run(["sudo", "sync"])
    finally:
        _umount(dst)
    ui.step(4, 4, "Verifying")
    if verify(hfs):
        ui.ok("Volume verified.")
    else:
        ui.warn("fsck reported issues — inspect before use.")


def build(target: str, app_dir: Path, work: Path, hfs_label: str,
          journaled: bool = False, reference_root: Path | None = None,
          build_version: str | None = None) -> None:
    """Full Apple Silicon build pipeline (copy an already-unpacked app)."""
    ui.step(1, 4, "Partitioning (GPT: ESP + Apple HFS+)")
    esp, hfs = partition(target, hfs_label)
    ui.step(2, 4, "Formatting (empty ESP + HFS+)")
    format_parts(esp, hfs, hfs_label, journaled=journaled)
    ui.step(3, 4, "Copying installer content")
    copy_installer(hfs, app_dir, work, reference_root=reference_root,
                   build_version=build_version)
    ui.step(4, 4, "Verifying")
    if verify(hfs):
        ui.ok("Volume verified.")
    else:
        ui.warn("fsck reported issues — inspect before use.")


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
    unmount_if_mounted(hfs)
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


# HFS+ internal/cache dirs that are per-volume state, not installer content —
# never copy them into a fresh clone.
_HFS_INTERNAL = [".journal", ".journal_info_block", ".Spotlight-V100",
                 ".fseventsd", ".HFS+ Private Directory Data", ".HFS+ Private Data"]


def build_intel_clone(target: str, stick_root: Path, work: Path, hfs_label: str) -> None:
    """Clone a full createinstallmedia stick onto ``target`` for an Intel Mac.

    build()/build_intel copy only the app; an Intel Mac boots from the *expanded*
    tree (``System/Library/CoreServices/boot.efi``, ``BaseSystem/``, ``System/``,
    ``Firmware/`` …), so here we copy the whole source-stick root — minus the HFS+
    internal/cache dirs — then optionally bless.

    Bless is NOT required on old Intel Macs (recognition is by the conventional
    boot.efi path — verified on hardware 2026-11-16); we set it anyway as harmless
    insurance for T2 Macs. EXPERIMENTAL: structure-validated; boot-test on real
    Intel hardware. Modern-macOS pkg sources can't produce this tree on Linux, so
    this path requires a real createinstallmedia stick to clone. See intel-oracle.md.
    """
    from . import bless

    ui.step(1, 4, "Partitioning (GPT: ESP + Apple HFS+)")
    esp, hfs = partition(target, hfs_label)
    ui.step(2, 4, "Formatting (empty ESP + HFS+)")
    format_parts(esp, hfs, hfs_label, journaled=False)
    ui.step(3, 4, "Cloning the full installer tree (several minutes)")
    dst = work / "dst_rw"
    unmount_if_mounted(hfs)
    _mount(hfs, dst, ro=False)
    bless_targets = None
    try:
        excl = " ".join(f"--exclude {shlex.quote('./' + e)}" for e in _HFS_INTERNAL)
        _run(["bash", "-c",
              f"cd {shlex.quote(str(stick_root))} && "
              f"sudo bsdtar -cf - {excl} . | sudo bsdtar -xpf - -C {shlex.quote(str(dst))}"])
        subprocess.run(["sudo", "sync"])
        boot_efi = dst / "System" / "Library" / "CoreServices" / "boot.efi"
        if boot_efi.exists():
            bless_targets = (bless.cnid(boot_efi.parent), bless.cnid(boot_efi))
        else:
            ui.warn("no System/Library/CoreServices/boot.efi copied — is the source a "
                    "full createinstallmedia stick? (Intel needs the expanded tree)")
    finally:
        _umount(dst)
    if bless_targets:
        f_cnid, b_cnid = bless_targets
        bless.patch_finderinfo(hfs, {0: f_cnid, 1: b_cnid, 5: f_cnid})
        ui.ok(f"blessed boot.efi (optional T2 insurance): folder={f_cnid} file={b_cnid}")
    ui.step(4, 4, "Verifying")
    if verify(hfs):
        ui.ok("Volume verified.")
    else:
        ui.warn("fsck reported issues — inspect before use.")


def _hfs_partition_of(loop_dev: str) -> str:
    """Return the HFS+ partition of a loop device (blkid-identified)."""
    for i in range(1, 16):
        part = f"{loop_dev}p{i}"
        if not os.path.exists(part):
            break
        r = subprocess.run(["sudo", "blkid", "-o", "value", "-s", "TYPE", part],
                           capture_output=True, text=True)
        if r.stdout.strip() == "hfsplus":
            return part
    raise BuildError("no HFS+ partition found in the decoded BaseSystem image")


def build_recovery(target: str, basesystem_dmg: Path, work: Path, hfs_label: str) -> None:
    """Build an Intel recovery/installer stick from an Apple ``BaseSystem.dmg``
    (as fetched by ``macrecovery.py``).

    Decodes the dmg to a raw image (lib.udif — no dmg2img), loop-mounts its HFS+
    'macOS Base System' partition, and clones that onto a fresh HFS+ target. The
    result carries ``boot.efi`` at the conventional path, which a real Intel Mac
    boots (no OpenCore; bless optional — see intel-oracle.md / docs/intel.md).

    One stick, pkg-free. EXPERIMENTAL: pipeline validated; boot-test on Intel.
    """
    from . import bless, udif

    raw = work / "basesystem.img"
    src = work / "src_ro"
    src.mkdir(parents=True, exist_ok=True)
    ui.step(1, 5, "Decoding BaseSystem.dmg -> raw image")
    udif.decode(str(basesystem_dmg), str(raw))

    ui.step(2, 5, "Mounting the recovery filesystem")
    loop = subprocess.run(["sudo", "losetup", "-fP", "--show", str(raw)],
                          capture_output=True, text=True, check=True).stdout.strip()
    hfs_src = _hfs_partition_of(loop)
    subprocess.run(["sudo", "mount", "-o", "ro", "-t", "hfsplus", hfs_src, str(src)], check=True)
    try:
        ui.step(3, 5, "Partitioning + formatting the USB (GPT: ESP + Apple HFS+)")
        esp, hfs = partition(target, hfs_label)
        format_parts(esp, hfs, hfs_label, journaled=False)

        ui.step(4, 5, "Copying the recovery system onto the USB")
        dst = work / "dst_rw"
        unmount_if_mounted(hfs)
        _mount(hfs, dst, ro=False)
        bless_targets = None
        try:
            excl = " ".join(f"--exclude {shlex.quote('./' + e)}" for e in _HFS_INTERNAL)
            _run(["bash", "-c",
                  f"cd {shlex.quote(str(src))} && "
                  f"sudo bsdtar -cf - {excl} . | sudo bsdtar -xpf - -C {shlex.quote(str(dst))}"])
            subprocess.run(["sudo", "sync"])
            boot_efi = dst / "System" / "Library" / "CoreServices" / "boot.efi"
            if boot_efi.exists():
                bless_targets = (bless.cnid(boot_efi.parent), bless.cnid(boot_efi))
            else:
                ui.warn("no boot.efi in the recovery — unexpected for a BaseSystem")
            # Branded icon in the Intel Startup Manager (it reads .VolumeIcon.icns).
            # The recovery ships a *dangling* .VolumeIcon.icns symlink -> a generic
            # drive icon; replace it with the installer app's real icon.
            icns = subprocess.run(["sudo", "find", str(dst), "-name", "InstallAssistant.icns"],
                                  capture_output=True, text=True).stdout.splitlines()
            if icns:
                subprocess.run(["sudo", "rm", "-f", str(dst / ".VolumeIcon.icns")])
                subprocess.run(["sudo", "cp", icns[0], str(dst / ".VolumeIcon.icns")], check=True)
                ui.ok("added the installer's volume icon (branded)")
        finally:
            _umount(dst)
        if bless_targets:
            f_cnid, b_cnid = bless_targets
            bless.patch_finderinfo(hfs, {0: f_cnid, 1: b_cnid, 5: f_cnid})
            ui.ok(f"blessed boot.efi (optional T2 insurance): folder={f_cnid} file={b_cnid}")
    finally:
        subprocess.run(["sudo", "umount", str(src)])
        subprocess.run(["sudo", "losetup", "-d", loop])

    ui.step(5, 5, "Verifying")
    if verify(hfs):
        ui.ok("Volume verified.")
    else:
        ui.warn("fsck reported issues — inspect before use.")
    raw.unlink(missing_ok=True)


def build_intel_full(target: str, basesystem_dmg: Path, pkg: Path,
                     work: Path, hfs_label: str) -> None:
    """Build a no-Mac, no-clone Intel *full offline installer* stick.

    Combines two Apple-served inputs, entirely on Linux:
      - a recovery ``BaseSystem.dmg`` (``macrecovery.py``) — the bootable root; it
        carries ``boot.efi`` at the conventional path and boots a real Intel Mac
      - an ``InstallAssistant.pkg`` — the Install app skeleton plus the **whole
        pkg** placed as ``Contents/SharedSupport/SharedSupport.dmg`` (the "pkgdmg"
        the installer expects), i.e. the offline OS payload

    The booted recovery's ``startosinstall`` then installs macOS offline from that
    SharedSupport — no UpdateBrain, no network. Validated on a pre-T2 Intel iMac
    (2026-11-17: startosinstall accepted the media and ran the install); a clean
    install-to-desktop finish is still being confirmed, so this path is
    EXPERIMENTAL. See docs/intel.md.
    """
    from . import bless, udif
    repo_root = Path(__file__).resolve().parent.parent

    raw = work / "basesystem.img"
    src = work / "src_ro"
    src.mkdir(parents=True, exist_ok=True)
    ui.step(1, 6, "Decoding BaseSystem.dmg -> raw image")
    udif.decode(str(basesystem_dmg), str(raw))

    ui.step(2, 6, "Mounting the recovery filesystem")
    loop = subprocess.run(["sudo", "losetup", "-fP", "--show", str(raw)],
                          capture_output=True, text=True, check=True).stdout.strip()
    hfs_src = _hfs_partition_of(loop)
    subprocess.run(["sudo", "mount", "-o", "ro", "-t", "hfsplus", hfs_src, str(src)], check=True)
    try:
        ui.step(3, 6, "Partitioning + formatting the USB (GPT: ESP + Apple HFS+)")
        esp, hfs = partition(target, hfs_label)
        format_parts(esp, hfs, hfs_label, journaled=False)

        dst = work / "dst_rw"
        unmount_if_mounted(hfs)
        _mount(hfs, dst, ro=False)
        bless_targets = None
        try:
            ui.step(4, 6, "Copying the bootable recovery system onto the USB")
            excl = " ".join(f"--exclude {shlex.quote('./' + e)}" for e in _HFS_INTERNAL)
            _run(["bash", "-c",
                  f"cd {shlex.quote(str(src))} && "
                  f"sudo bsdtar -cf - {excl} . | sudo bsdtar -xpf - -C {shlex.quote(str(dst))}"])
            subprocess.run(["sudo", "sync"])

            ui.step(5, 6, "Overlaying the Install app + SharedSupport payload (~12 GB)")
            app = stream_app_to_target(pkg, dst, repo_root)
            payload.write_markers_from_app(app, dst, payload.pkg_build_version(pkg))
            ui.ok("synthesized root markers (.IAPhysicalMedia + icon)")
            _write_firmware_check(dst, pkg, work)
            ui.ok("added firmware-check helper (T2 bridgeOS advisor)")
            _flushing_note()
            subprocess.run(["sudo", "sync"])

            boot_efi = dst / "System" / "Library" / "CoreServices" / "boot.efi"
            if boot_efi.exists():
                bless_targets = (bless.cnid(boot_efi.parent), bless.cnid(boot_efi))
            else:
                ui.warn("no boot.efi in the recovery — unexpected for a BaseSystem")
        finally:
            _umount(dst)
        if bless_targets:
            f_cnid, b_cnid = bless_targets
            bless.patch_finderinfo(hfs, {0: f_cnid, 1: b_cnid, 5: f_cnid})
            ui.ok(f"blessed boot.efi (optional T2 insurance): folder={f_cnid} file={b_cnid}")
    finally:
        subprocess.run(["sudo", "umount", str(src)])
        subprocess.run(["sudo", "losetup", "-d", loop])

    ui.step(6, 6, "Verifying")
    if verify(hfs):
        ui.ok("Volume verified.")
    else:
        ui.warn("fsck reported issues — inspect before use.")
    raw.unlink(missing_ok=True)
