#!/usr/bin/env python3
"""SlickStick — build bootable macOS installer USBs, without a Mac.

Partitions a USB (GPT: empty ESP + Apple HFS+), formats HFS+, and copies a
macOS installer onto it so Apple Silicon firmware boots it directly. The macOS
files are fetched from Apple at run time (or supplied locally); none ship here.

Apple Silicon is the proven path. Intel is EXPERIMENTAL and unverified.
See docs/how-it-works.md.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

# Make `lib` importable no matter the current working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import deps, usb
from lib.sources import make_source

DEFAULT_LABEL = "Install macOS"


def _human(nbytes: int) -> str:
    gb = int(nbytes) / 1e9
    return f"{gb:.1f} GB"


def cmd_deps(args) -> int:
    print("Checking dependencies:")
    missing = deps.check(verbose=True)
    return 1 if missing else 0


def cmd_devices(args) -> int:
    disks = usb.list_removable_disks()
    if not disks:
        print("No removable/USB disks found.")
        return 1
    print("Removable / USB disks:")
    for d in disks:
        model = (d.get("model") or "").strip()
        print(f"  /dev/{d['name']:<6} {_human(d['size']):>9}  {model}")
    return 0


def _pick_device(disks: list[dict]) -> str | None:
    print("Removable / USB disks:")
    for i, d in enumerate(disks):
        print(f"  [{i}] /dev/{d['name']:<6} {_human(d['size']):>9}  {(d.get('model') or '').strip()}")
    try:
        sel = input("Pick the TARGET disk to ERASE (number, or blank to cancel): ").strip()
    except EOFError:
        return None
    if not sel.isdigit() or int(sel) >= len(disks):
        return None
    return "/dev/" + disks[int(sel)]["name"]


def _confirm_wipe(target: str, assume_yes: bool) -> bool:
    import subprocess
    print(f"\nAbout to ERASE {target}:")
    subprocess.run(["lsblk", "-o", "NAME,SIZE,TYPE,LABEL,FSTYPE,MOUNTPOINT", target])
    if assume_yes:
        return True
    try:
        ans = input(f"\nType the device name '{Path(target).name}' to confirm wipe: ").strip()
    except EOFError:
        return False
    return ans == Path(target).name


def cmd_build(args) -> int:
    # 0. deps
    if deps.check(verbose=False):
        print("Missing dependencies — run: slickstick deps")
        return 1

    # 1. choose target
    target = args.device
    disks = usb.list_removable_disks()
    if not target:
        target = _pick_device(disks)
        if not target:
            print("Cancelled.")
            return 1

    # 2. safety
    try:
        usb.assert_safe_target(target)
    except usb.BuildError as e:
        print(f"SAFETY: {e}")
        return 1

    # 3. confirm
    if not _confirm_wipe(target, args.yes):
        print("Cancelled.")
        return 1

    # 4. obtain the installer app
    work = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="slickstick-"))
    work.mkdir(parents=True, exist_ok=True)
    source = make_source(args)
    print(f"\nSource: {source.id} — {source.desc}")
    try:
        app = source.obtain(work, version=args.version)
    except Exception as e:  # noqa: BLE001 - surface any backend failure cleanly
        print(f"Failed to obtain installer from source '{source.id}': {e}")
        return 1
    print(f"Installer app: {app}")

    # 5. optional reference stick (copy real root markers)
    reference_root = None
    if args.reference_stick:
        reference_root = _mount_reference(args.reference_stick, work)

    # 6. build
    label = args.label or DEFAULT_LABEL
    try:
        if args.arch == "intel":
            usb.build_intel(target, app, work, label)
        else:
            usb.build(target, app, work, label,
                      journaled=args.journaled, reference_root=reference_root)
    except usb.BuildError as e:
        print(f"Build failed: {e}")
        return 1
    finally:
        if reference_root is not None:
            _umount_reference(reference_root)

    print(f"\nFinished. Test by booting {target} on the target Mac "
          "(hold power -> Options).")
    return 0


def _mount_reference(dev: str, work: Path) -> Path:
    import subprocess
    mp = work / "reference_ro"
    mp.mkdir(parents=True, exist_ok=True)
    part = dev if dev[-1].isdigit() else dev + "2"
    subprocess.run(["sudo", "mount", "-t", "hfsplus", "-o", "ro", part, str(mp)], check=True)
    return mp


def _umount_reference(mp: Path) -> None:
    import subprocess
    subprocess.run(["sudo", "umount", str(mp)])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="slickstick", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("deps", help="check required tools").set_defaults(func=cmd_deps)
    sub.add_parser("devices", help="list removable/USB disks").set_defaults(func=cmd_devices)

    b = sub.add_parser("build", help="build an installer USB")
    b.add_argument("-d", "--device", help="target disk, e.g. /dev/sdb (prompted if omitted)")
    b.add_argument("--arch", choices=["silicon", "intel"], default="silicon",
                   help="silicon (proven) or intel (EXPERIMENTAL, unverified)")
    src = b.add_argument_group("macOS source (default: gibmacos)")
    src.add_argument("--local", help="path to an Install app, a .pkg, or a mounted stick")
    src.add_argument("--url", help="direct InstallAssistant.pkg URL (Apple CDN)")
    src.add_argument("--version", help="version hint for backends that support it")
    b.add_argument("--reference-stick", help="a createinstallmedia stick to copy real root markers from")
    b.add_argument("--journaled", action="store_true", help="make HFS+ journaled (needs force-rw writes)")
    b.add_argument("--label", help=f"HFS+ volume label (default: {DEFAULT_LABEL!r})")
    b.add_argument("--workdir", help="where to download/extract (default: temp dir)")
    b.add_argument("-y", "--yes", action="store_true", help="skip the typed wipe confirmation")
    b.set_defaults(func=cmd_build)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
