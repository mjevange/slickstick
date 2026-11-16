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
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Make `lib` importable no matter the current working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import deps, payload, usb, ui
from lib.sources import (
    make_source, GibMacOSSource, StickSource, LocalSource, UrlSource,
    find_installer_volumes,
)

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
    ui.heading("Removable / USB disks:")
    for i, d in enumerate(disks):
        idx = f"{ui.CYAN}{ui.BOLD}[{i}]{ui.RESET}"
        print(f"  {idx} /dev/{d['name']:<6} {_human(d['size']):>9}  {(d.get('model') or '').strip()}")
    try:
        sel = input("Pick the TARGET disk to ERASE (number, or blank to cancel): ").strip()
    except EOFError:
        return None
    if not sel.isdigit() or int(sel) >= len(disks):
        return None
    return "/dev/" + disks[int(sel)]["name"]


def _confirm_wipe(target: str, assume_yes: bool) -> bool:
    import subprocess
    ui.danger(f"\nAbout to ERASE {target}:")
    subprocess.run(["lsblk", "-o", "NAME,SIZE,TYPE,LABEL,FSTYPE,MOUNTPOINT", target])
    if assume_yes:
        return True
    try:
        ans = input(f"\nType the device name '{Path(target).name}' to confirm wipe: ").strip()
    except EOFError:
        return False
    return ans == Path(target).name


def _default_work_base() -> Path:
    """Default parent for the work dir — the user's cache, NOT /tmp (often a tiny
    tmpfs that can't hold a ~18 GB extraction)."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "slickstick"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _menu(options: list[str], prompt: str = "Choose") -> int:
    for i, opt in enumerate(options, 1):
        print(ui.option(i, opt))
    while True:
        try:
            sel = input(f"{prompt} [1-{len(options)}]: ").strip()
        except EOFError:
            return 0
        if sel.isdigit() and 1 <= int(sel) <= len(options):
            return int(sel) - 1
        print("  Please enter a number from the list.")


def _choose_target(exclude: str | None = None) -> str | None:
    """Detect inserted USB media and let the user pick; offer rescan if none.

    `exclude` is a whole-disk path (e.g. the source stick) to hide from the list.
    """
    while True:
        disks = [d for d in usb.list_removable_disks()
                 if exclude is None or f"/dev/{d['name']}" != exclude]
        if disks:
            return _pick_device(disks)  # None == cancelled
        try:
            r = input("No USB disk detected. Insert one and press Enter to rescan (q to quit): ").strip()
        except EOFError:
            return None
        if r.lower() == "q":
            return None


def _execute_build(target: str, source, arch: str, reference_stick: str | None,
                   label: str, journaled: bool, yes: bool, workdir: str | None,
                   source_disk: str | None = None, strategy: str = "copy") -> int:
    """Shared build core used by both the wizard and the `build` subcommand.

    strategy: "copy" extracts the app to the work dir then copies it onto the USB;
    "stream" writes straight onto the USB from the pkg (~0 scratch space).
    """
    try:
        usb.assert_safe_target(target, source_disk)
    except usb.BuildError as e:
        print(f"SAFETY: {e}")
        return 1

    streaming = strategy == "stream"
    work = Path(workdir) if workdir else Path(
        tempfile.mkdtemp(prefix="work-", dir=str(_default_work_base())))
    work.mkdir(parents=True, exist_ok=True)

    # Disk-space preflight — before we wipe anything. Streaming needs far less.
    need = source.space_needed(stream=streaming)
    if need:
        free = shutil.disk_usage(work).free
        if free < need:
            ui.err(f"Not enough free space for the '{source.id}' source.")
            print(f"  Need ~{ui.human(need)} in {work}, but only {ui.human(free)} is free.")
            print("  Free up space, pass --workdir on a larger disk, or try --strategy stream.")
            source.cleanup()
            return 1
        ui.info(f"space check: {ui.human(free)} free, ~{ui.human(need)} needed — OK")

    if not _confirm_wipe(target, yes):
        print("Cancelled.")
        source.cleanup()
        return 1

    # Authenticate sudo once, up front — fail cleanly here rather than mid-wipe.
    try:
        usb.ensure_sudo()
    except usb.BuildError as e:
        print(f"\n{e}")
        source.cleanup()
        return 1

    print(f"\nSource: {source.id} — {source.desc}")
    own_reference = False
    reference_root = None
    try:
        with usb.SudoKeepAlive():
            if streaming:
                pkg = source.fetch_pkg(work)
                if pkg is None:
                    ui.err("This source has no pkg to stream — use the copy strategy.")
                    return 1
                lbl = label if label != DEFAULT_LABEL else (payload.pkg_app_name(pkg) or DEFAULT_LABEL)
                print(f"Streaming from: {pkg}")
                usb.build_streaming(target, pkg, work, lbl)
            else:
                app = source.obtain(work)
                print(f"Installer app: {app}")
                lbl = label
                if lbl == DEFAULT_LABEL and app.name.endswith(".app"):
                    lbl = app.name[: -len(".app")]
                    print(f"Volume label: {lbl}")
                if reference_stick:
                    reference_root = _mount_reference(reference_stick, work)
                    own_reference = True
                elif getattr(source, "marker_root", None):
                    reference_root = source.marker_root
                build_version = getattr(source, "build_version", None)
                if arch == "intel":
                    usb.build_intel(target, app, work, lbl)
                else:
                    usb.build(target, app, work, lbl, journaled=journaled,
                              reference_root=reference_root, build_version=build_version)
    except usb.BuildError as e:
        print(f"Build failed: {e}")
        return 1
    except Exception as e:  # noqa: BLE001 - surface any backend failure cleanly
        print(f"Build failed: {e}")
        return 1
    finally:
        if own_reference and reference_root is not None:
            _umount_reference(reference_root)
        source.cleanup()

    print()
    ui.ok(f"Finished. Test by booting {target} on the target Mac (hold power -> Options).")
    return 0


def cmd_build(args) -> int:
    """Flag-driven build (power users)."""
    if deps.missing():
        print("Missing dependencies — run: slickstick deps")
        return 1
    source_disk = usb.parent_disk(args.stick) if getattr(args, "stick", None) else None
    target = args.device or _choose_target(exclude=source_disk)
    if not target:
        print("Cancelled.")
        return 1
    return _execute_build(target, make_source(args), args.arch, args.reference_stick,
                          args.label or DEFAULT_LABEL, args.journaled, args.yes, args.workdir,
                          source_disk=source_disk, strategy=args.strategy)


def cmd_wizard(args) -> int:
    """Guided, menu-driven build — the default when no subcommand is given."""
    ui.banner()
    if deps.missing():
        ui.warn("Some required tools are missing:\n")
        deps.check(verbose=True)
        print("\nInstall the tools listed above, then run SlickStick again.")
        return 1
    ui.ok("Dependencies OK.")
    print()

    ui.heading("\nHow should SlickStick get macOS?")
    pick = _menu([
        "Download from Apple via gibMacOS (recommended)",
        "Clone from an inserted macOS installer stick (real markers, proven)",
        "Use files I already have (an Install app or a .pkg)",
        "Download a direct InstallAssistant.pkg URL",
    ])
    source_disk = None
    streamable = False
    if pick == 0:
        source = GibMacOSSource()
        streamable = True
    elif pick == 1:
        vols = find_installer_volumes()
        if not vols:
            print("  No inserted macOS installer sticks detected. Insert one and")
            print("  restart SlickStick, or choose a different source.")
            return 1
        ui.heading("\n  Detected installer sticks:")
        opts = []
        for v in vols:
            where = v["mountpoint"] or "not mounted"
            opts.append(f'{v["path"]}  {v["size"] / 1e9:.0f} GB  "{v["label"]}"  [{where}]')
        sel = _menu(opts, "  Clone from which stick")
        source = StickSource(vols[sel]["path"])
        source_disk = usb.parent_disk(vols[sel]["path"])
    elif pick == 2:
        p = input("  Path to an Install app or a .pkg: ").strip()
        source = LocalSource(p)
        streamable = p.endswith(".pkg")
    else:
        source = UrlSource(input("  InstallAssistant.pkg URL: ").strip())
        streamable = True

    ui.heading("\nTarget Mac architecture?")
    arch = "silicon" if _menu([
        "Apple Silicon (M1/M2/M3/… — proven)",
        "Intel (EXPERIMENTAL — not verified to boot)",
    ]) == 0 else "intel"
    if arch == "intel":
        print("  ! Intel is experimental and unverified; continuing anyway.")

    strategy = "copy"
    if streamable and arch == "silicon":
        ui.heading("\nMaking more than one USB from this installer?")
        strategy = "stream" if _menu([
            "Just this one — write straight to the USB, keep nothing (fastest, ~0 scratch space)",
            "I might make more — keep the unpacked installer on disk (~18 GB) for instant re-copies",
        ]) == 0 else "copy"

    name = input("\nName for the USB (shown in the Mac's startup picker),\n"
                 "or press Enter for the default: ").strip()
    label = name or DEFAULT_LABEL

    ui.heading("\nNow pick the TARGET USB to erase.")
    target = _choose_target(exclude=source_disk)
    if not target:
        print("Cancelled.")
        return 1

    return _execute_build(target, source, arch, reference_stick=None,
                          label=label, journaled=False, yes=False, workdir=None,
                          source_disk=source_disk, strategy=strategy)


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
    p = argparse.ArgumentParser(
        prog="slickstick",
        description=__doc__.splitlines()[0] + "  Run with no arguments for the guided wizard.",
    )
    # No subcommand => guided wizard.
    p.set_defaults(func=cmd_wizard)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("wizard", help="guided, menu-driven build (this is the default)").set_defaults(func=cmd_wizard)
    sub.add_parser("deps", help="check required tools").set_defaults(func=cmd_deps)
    sub.add_parser("devices", help="list removable/USB disks").set_defaults(func=cmd_devices)

    b = sub.add_parser("build", help="flag-driven build (advanced / scripting)")
    b.add_argument("-d", "--device", help="target disk, e.g. /dev/sdb (prompted if omitted)")
    b.add_argument("--arch", choices=["silicon", "intel"], default="silicon",
                   help="silicon (proven) or intel (EXPERIMENTAL, unverified)")
    b.add_argument("--strategy", choices=["copy", "stream"], default="copy",
                   help="copy: extract to the work dir then copy onto the USB (default). "
                        "stream: write straight onto the USB from the pkg (~0 scratch space)")
    src = b.add_argument_group("macOS source (default: gibmacos)")
    src.add_argument("--stick", help="clone an inserted installer stick's partition, e.g. /dev/sdc2 (real markers)")
    src.add_argument("--local", help="path to an existing Install app or a .pkg")
    src.add_argument("--url", help="direct InstallAssistant.pkg URL (Apple CDN)")
    src.add_argument("--version", help="version hint for backends that support it")
    b.add_argument("--reference-stick",
                   help="advanced: copy real root markers from this stick when the app came "
                        "from a non-stick source (usually unnecessary — markers are synthesized, "
                        "or use --stick to clone a stick directly)")
    b.add_argument("--journaled", action="store_true", help="make HFS+ journaled (needs force-rw writes)")
    b.add_argument("--label", help=f"HFS+ volume label (default: {DEFAULT_LABEL!r})")
    b.add_argument("--workdir", help="where to download/extract (default: ~/.cache/slickstick)")
    b.add_argument("-y", "--yes", action="store_true", help="skip the typed wipe confirmation")
    b.set_defaults(func=cmd_build)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
