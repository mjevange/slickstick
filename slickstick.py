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
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Make `lib` importable no matter the current working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import deps, payload, usb, ui
from lib.sources import (
    make_source, GibMacOSSource, StickSource, LocalSource, UrlSource,
    find_installer_volumes, gibmacos, recovery,
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


def _pick_device(disks: list[dict], allow_nav: bool = False) -> str | None:
    ui.heading("Removable / USB disks:")
    for i, d in enumerate(disks):
        idx = f"{ui.CYAN}{ui.BOLD}[{i}]{ui.RESET}"
        print(f"  {idx} /dev/{d['name']:<6} {_human(d['size']):>9}  {(d.get('model') or '').strip()}")
    tail = "number" + (", s = start over" if allow_nav else "") + ", blank = cancel"
    while True:
        try:
            sel = input(f"Pick the TARGET disk to ERASE ({tail}): ").strip().lower()
        except EOFError:
            return None
        if not sel:
            return None
        if allow_nav and sel == "s":
            raise _StartOver
        if sel.isdigit() and int(sel) < len(disks):
            return "/dev/" + disks[int(sel)]["name"]
        print(f"  Enter a number from 0 to {len(disks) - 1}"
              + (", s to start over," if allow_nav else "") + " or blank to cancel.")


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


def _input_path(prompt: str) -> str:
    """input() with Tab filesystem-path completion (spaces supported). Falls back
    to a plain input if readline isn't available."""
    try:
        import glob
        import readline
    except ImportError:
        return input(prompt).strip()

    def completer(text, state):
        matches = [m + ("/" if os.path.isdir(m) else "")
                   for m in glob.glob(os.path.expanduser(text) + "*")]
        return matches[state] if state < len(matches) else None

    old_completer = readline.get_completer()
    old_delims = readline.get_completer_delims()
    readline.set_completer(completer)
    readline.set_completer_delims("\n")          # whole line is the path (keep spaces)
    readline.parse_and_bind("tab: complete")
    try:
        return input(prompt).strip()
    finally:
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_delims)


class _StartOver(Exception):
    """Wizard navigation: restart from the first question."""


class _Cancelled(Exception):
    """Wizard navigation: quit the wizard."""


def _menu(options: list[str], prompt: str = "Choose", nav: bool = True) -> int:
    """Numbered menu. With nav=True (the default), appends 'Start over' and
    'Cancel' entries that raise _StartOver / _Cancelled for the wizard loop to
    handle — a safe escape hatch on every pre-wipe selection page."""
    nav_opts = ["↺  Start over", "✕  Cancel"] if nav else []
    shown = options + nav_opts
    for i, opt in enumerate(shown, 1):
        print(ui.option(i, opt))
    n = len(shown)
    while True:
        try:
            sel = input(f"{prompt} [1-{n}]: ").strip()
        except EOFError:
            raise _Cancelled
        if sel.isdigit() and 1 <= int(sel) <= n:
            idx = int(sel) - 1
            if nav and idx == len(options):
                raise _StartOver
            if nav and idx == len(options) + 1:
                raise _Cancelled
            return idx
        print("  Please enter a number from the list.")


def _choose_target(exclude: str | None = None, allow_nav: bool = False) -> str | None:
    """Detect inserted USB media and let the user pick; offer rescan if none.

    `exclude` is a whole-disk path (e.g. the source stick) to hide from the list.
    `allow_nav` adds the wizard's Start over option to the picker.
    """
    while True:
        disks = [d for d in usb.list_removable_disks()
                 if exclude is None or f"/dev/{d['name']}" != exclude]
        if disks:
            return _pick_device(disks, allow_nav=allow_nav)  # None == cancelled
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
    label_err = _label_error(label)
    if label_err:
        ui.err(f"Volume name won't work — {label_err}.")
        return 1

    try:
        usb.assert_safe_target(target, source_disk)
    except usb.BuildError as e:
        print(f"SAFETY: {e}")
        return 1

    streaming = strategy == "stream"
    created_work = workdir is None
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
                    full_root = getattr(source, "marker_root", None)
                    if full_root is not None:
                        usb.build_intel_clone(target, Path(full_root), work, lbl)
                    else:
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
        # "stream" means keep nothing — drop our temp work dir (and any pkg we
        # downloaded into it). "copy" keeps it (that's the whole point).
        if created_work and streaming:
            shutil.rmtree(work, ignore_errors=True)

    print()
    ui.ok(f"Finished. Test by booting {target} on the target Mac (hold power -> Options).")
    return 0


def _run_recovery(target: str, dmg: Path, label: str, yes: bool,
                  workdir: str | None = None) -> int:
    """Safety-check, confirm, and build an Intel recovery stick from BaseSystem.dmg.
    Shared by the CLI (--recovery) and the wizard's Intel path."""
    try:
        usb.assert_safe_target(target, None)
    except usb.BuildError as e:
        print(f"SAFETY: {e}")
        return 1
    if not _confirm_wipe(target, yes):
        print("Cancelled.")
        return 1
    work = Path(workdir) if workdir else Path(
        tempfile.mkdtemp(prefix="rec-", dir=str(_default_work_base())))
    work.mkdir(parents=True, exist_ok=True)
    try:
        usb.ensure_sudo()
        with usb.SudoKeepAlive():
            usb.build_recovery(target, dmg, work, label)
    except Exception as e:  # noqa: BLE001
        print(f"Build failed: {e}")
        return 1
    print()
    ui.ok(f"Finished. Boot {target} on the Intel Mac (hold Option) — EXPERIMENTAL.")
    return 0


def _execute_recovery(args) -> int:
    """CLI: build an Intel recovery stick from a pre-downloaded BaseSystem.dmg."""
    dmg = Path(args.recovery)
    if not dmg.is_file():
        ui.err(f"BaseSystem.dmg not found: {dmg}")
        return 1
    target = args.device or _choose_target()
    if not target:
        print("Cancelled.")
        return 1
    return _run_recovery(target, dmg, args.label or DEFAULT_LABEL, args.yes, args.workdir)


def cmd_build(args) -> int:
    """Flag-driven build (power users)."""
    if deps.missing():
        print("Missing dependencies — run: slickstick deps")
        return 1
    if getattr(args, "recovery", None):
        return _execute_recovery(args)
    source_disk = usb.parent_disk(args.stick) if getattr(args, "stick", None) else None
    target = args.device or _choose_target(exclude=source_disk)
    if not target:
        print("Cancelled.")
        return 1
    return _execute_build(target, make_source(args), args.arch, args.reference_stick,
                          args.label or DEFAULT_LABEL, args.journaled, args.yes, args.workdir,
                          source_disk=source_disk, strategy=args.strategy)


_NAME_VER = {
    "big sur": 11, "monterey": 12, "ventura": 13, "sonoma": 14,
    "sequoia": 15, "tahoe": 26, "golden gate": 27,
}


def _installer_major(path: Path) -> int:
    """Best-effort macOS major version of a local installer, or 0 if unknown."""
    name = ""
    if path.name.endswith(".app"):
        name = path.name
    elif path.suffix == ".pkg":
        try:
            name = payload.pkg_app_name(path) or ""
        except Exception:
            name = ""
    low = name.lower()
    for key, ver in _NAME_VER.items():
        if key in low:
            return ver
    m = re.search(r"macos\s+(\d+)", low)
    return int(m.group(1)) if m else 0


def _verify_local_arch(path: Path, arch: str) -> None:
    """Stop (and offer to start over) if a supplied installer clearly can't boot
    the chosen arch. Best-effort by macOS version; an unknown version passes
    through. An InstallAssistant.pkg is always Big Sur+, so the real case this
    catches is Intel + an Apple-Silicon-only (macOS 27+) installer."""
    mj = _installer_major(path)
    if not mj:
        return
    reason = target = None
    if arch == "silicon" and mj < 11:
        reason = f"macOS {mj} predates Apple Silicon (needs macOS 11+)"
        target = "an Apple Silicon Mac"
    elif arch == "intel" and mj >= 27:
        reason = f"macOS {mj} is Apple-Silicon only"
        target = "an Intel Mac"
    if reason:
        ui.err(f"That installer can't boot {target} — {reason}.")
        input("  Press Enter to start over (or Ctrl-C to cancel): ")
        raise _StartOver


def _label_error(name: str) -> str | None:
    """Reason a volume name is incompatible with HFS+, or None if it's fine.

    Spaces and symbols like # % $ & @ are all valid (the default name itself has
    spaces); only ':' (HFS+'s path separator), control characters, and names
    longer than 255 characters are rejected.
    """
    if not name:
        return None
    if len(name) > 255:
        return f"too long ({len(name)} chars; HFS+ allows up to 255)"
    if ":" in name:
        return "':' isn't allowed — HFS+ uses it as the path separator"
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "control characters aren't allowed"
    return None


def _local_label_guess(path: Path) -> str | None:
    """The installer's bundle name (for the volume-label default), or None."""
    if path.name.endswith(".app"):
        return path.name[:-4]
    if path.suffix == ".pkg":
        try:
            return payload.pkg_app_name(path)
        except Exception:
            return None
    return None


def _url_head(url: str) -> tuple[bool, int, str]:
    """HEAD a URL (following redirects): returns (reachable_2xx, size, content_type)."""
    r = subprocess.run(["curl", "-sIL", "--max-time", "25", url],
                       capture_output=True, text=True)
    ok, size, ctype = False, 0, ""
    for line in r.stdout.splitlines():
        low = line.strip().lower()
        if low.startswith("http/"):
            parts = line.split()
            ok = len(parts) > 1 and parts[1].startswith("2")
        elif low.startswith("content-length:"):
            try:
                size = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif low.startswith("content-type:"):
            ctype = line.split(":", 1)[1].strip()
    return ok, size, ctype


def _prompt_valid_url() -> tuple[str, int]:
    """Prompt for an InstallAssistant.pkg URL and validate it at entry: it must
    look like a .pkg, be reachable, and not be a web page. Returns (url, size)."""
    while True:
        url = input("  InstallAssistant.pkg URL (Ctrl-C to cancel): ").strip()
        if not url:
            continue
        if not url.lower().split("?")[0].endswith(".pkg"):
            ui.warn("That must be a direct link to an InstallAssistant.pkg (ending in .pkg).")
            continue
        print("  Checking the URL…")
        ok, size, ctype = _url_head(url)
        if not ok:
            ui.warn("Couldn't reach that URL — it's not found or unreachable. Check the link.")
            continue
        if "html" in ctype.lower():
            ui.warn("That URL returns a web page, not a pkg file.")
            continue
        if size:
            ui.ok(f"Reachable — {ui.human(size)}.")
        ui.warn("With a custom URL (not the Option 1 gibMacOS method) SlickStick can't "
                "verify the installer's macOS version against your Mac's architecture "
                "and model.")
        ans = input("  Enter = confirm it's compatible & continue  ·  "
                    "s = start over  ·  Ctrl-C = cancel: ").strip().lower()
        if ans == "s":
            raise _StartOver
        return url, size


def _prompt_label(default: str, hint: str) -> str:
    """Prompt for a validated HFS+ volume name, re-asking on bad input."""
    while True:
        name = input(f"\n  Volume name ({hint}): ").strip()
        label = name or default
        err = _label_error(label)
        if err:
            ui.warn(f"That name won't work — {err}. Try another.")
            continue
        return label


def _wizard_intel() -> dict:
    """Intel build flow: Apple Recovery download (proven) or clone a stick."""
    ui.heading("\nHow should SlickStick build the Intel stick?")
    if _menu([
        "Download macOS Recovery from Apple (proven — boots real Intel Macs, 1 stick)",
        "Clone an inserted createinstallmedia stick (full offline installer, experimental)",
    ]) == 0:
        return _wizard_intel_recovery()
    return _wizard_intel_clone()


def _wizard_intel_recovery() -> dict:
    """Download an Apple Recovery image and build a bootable recovery stick."""
    print("\n  Downloads an Apple Recovery image for the TARGET Mac and builds a")
    print("  bootable recovery stick (boots to macOS Recovery -> reinstall over the net).")
    print("\n  Tip: the target Mac's board-id is in About This Mac -> System Report,")
    print("  or in Recovery's Terminal:  ioreg -l | grep board-id")
    board = input("\n  Target Mac's board-id (e.g. Mac-A369DDC4E67F1C45),\n"
                  "  or press Enter for a generic image: ").strip()
    ui.heading("\nWhich macOS version?")
    os_type = "default" if _menu([
        "The macOS your Mac shipped with (safest — pre-Cryptex, proven)",
        "The newest your Mac supports (may be Cryptex-based — experimental)",
    ]) == 0 else "latest"
    work = Path(tempfile.mkdtemp(prefix="rec-", dir=str(_default_work_base())))
    try:
        dmg = recovery.download_recovery(board, os_type, work)
    except Exception as e:  # noqa: BLE001
        ui.err(f"Recovery download failed: {e}")
        raise _StartOver
    label = _prompt_label("macOS Recovery", 'Enter for "macOS Recovery"')
    ui.heading("\nNow pick the TARGET USB to erase.")
    target = _choose_target(allow_nav=True)
    if not target:
        raise _Cancelled
    return {"_recovery": True, "target": target, "dmg": str(dmg),
            "label": label, "workdir": str(work)}


def _wizard_intel_clone() -> dict:
    """Clone a createinstallmedia stick onto a target (full offline installer)."""
    vols = find_installer_volumes()
    if not vols:
        print("  No inserted createinstallmedia stick detected. Insert one,")
        print("  or start over to use the Recovery download instead.")
        raise _StartOver
    ui.heading("\n  Detected installer sticks:")
    opts = []
    for v in vols:
        where = v["mountpoint"] or "not mounted"
        opts.append(f'{v["path"]}  {v["size"] / 1e9:.0f} GB  "{v["label"]}"  [{where}]')
    sel = _menu(opts, "  Clone from which stick")
    source = StickSource(vols[sel]["path"])
    source_disk = usb.parent_disk(vols[sel]["path"])
    label = _prompt_label(DEFAULT_LABEL, "Enter for the default")
    ui.heading("\nNow pick the TARGET USB to erase.")
    target = _choose_target(exclude=source_disk, allow_nav=True)
    if not target:
        raise _Cancelled
    return {"target": target, "source": source, "arch": "intel",
            "reference_stick": None, "label": label, "journaled": False,
            "yes": False, "workdir": None, "source_disk": source_disk,
            "strategy": "copy"}


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

    while True:
        try:
            plan = _wizard_collect(args)
        except _StartOver:
            print("\n↺  Starting over.\n")
            continue
        except (_Cancelled, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
        if plan.pop("_recovery", False):
            return _run_recovery(plan["target"], Path(plan["dmg"]),
                                 plan["label"], yes=False, workdir=plan.get("workdir"))
        return _execute_build(**plan)


def _wizard_collect(args) -> dict:
    """Walk the pre-wipe selection pages and return the build params. Menu nav
    entries raise _StartOver / _Cancelled for cmd_wizard's loop to handle."""
    # Architecture first — it drives which installers we offer (and lets us
    # check a supplied pkg against the target Mac) before anything else.
    ui.heading("\nTarget Mac architecture?")
    arch = "silicon" if _menu([
        "Apple Silicon (M1/M2/M3/… — proven)",
        "Intel (recovery stick proven; or clone a full installer)",
    ]) == 0 else "intel"
    if arch == "intel":
        return _wizard_intel()

    ui.heading("\nHow should SlickStick get macOS?")
    pick = _menu([
        "Download from Apple via gibMacOS (recommended)",
        "Clone from an inserted macOS installer stick (real markers, proven)",
        "Use files I already have (an Install app or a .pkg)",
        "Download a direct InstallAssistant.pkg URL",
    ])
    source_disk = None
    streamable = False
    default_label = DEFAULT_LABEL
    if pick == 0:
        prods = gibmacos.list_products(arch)
        if not prods:
            print("  No full installers in Apple's catalog for that architecture.")
            raise _StartOver
        ui.heading("\nWhich macOS version?")
        sel = _menu([f'{p["title"]}  {p["version"]}  ({p["build"]})' for p in prods],
                    "  Version")
        source = GibMacOSSource(product=prods[sel])
        streamable = True
        default_label = f'Install {prods[sel]["title"]}'
    elif pick == 1:
        vols = find_installer_volumes()
        if not vols:
            print("  No inserted macOS installer sticks detected. Insert one,")
            print("  or start over and choose a different source.")
            raise _StartOver
        ui.heading("\n  Detected installer sticks:")
        opts = []
        for v in vols:
            where = v["mountpoint"] or "not mounted"
            opts.append(f'{v["path"]}  {v["size"] / 1e9:.0f} GB  "{v["label"]}"  [{where}]')
        sel = _menu(opts, "  Clone from which stick")
        source = StickSource(vols[sel]["path"])
        source_disk = usb.parent_disk(vols[sel]["path"])
    elif pick == 2:
        p = _input_path("  Path to an Install app or a .pkg (Tab to complete): ")
        source = LocalSource(p)
        streamable = p.endswith(".pkg")
        _verify_local_arch(Path(p), arch)
        default_label = _local_label_guess(Path(p)) or DEFAULT_LABEL
    else:
        url, size = _prompt_valid_url()
        source = UrlSource(url, pkg_size=size)
        streamable = True

    strategy = "copy"
    if streamable and arch == "silicon":
        ui.heading("\nMaking more than one USB from this installer?")
        strategy = "stream" if _menu([
            "Just this one — write straight to the USB, keep nothing (fastest, ~0 scratch space)",
            "I might make more — keep the unpacked installer on disk (~18 GB) for instant re-copies",
        ]) == 0 else "copy"

    hint = f'"{default_label}"' if default_label != DEFAULT_LABEL else "copied from the installer"
    while True:
        name = input(f"\n  Choose a USB volume name (Enter = {hint}): ").strip()
        err = _label_error(name)
        if err:
            ui.warn(f"That name won't work — {err}. Try another.")
            continue
        break
    label = name or default_label

    ui.heading("\nNow pick the TARGET USB to erase.")
    target = _choose_target(exclude=source_disk, allow_nav=True)
    if not target:
        raise _Cancelled

    return {
        "target": target, "source": source, "arch": arch,
        "reference_stick": None, "label": label, "journaled": False,
        "yes": False, "workdir": None, "source_disk": source_disk,
        "strategy": strategy,
    }


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
    src.add_argument("--recovery", help="build an Intel recovery stick from a "
                                        "BaseSystem.dmg (downloaded with macrecovery.py)")
    src.add_argument("--version", help="version hint for backends that support it")
    b.add_argument("--reference-stick",
                   help="advanced: copy real root markers from this stick when the app came "
                        "from a non-stick source (usually unnecessary — markers are synthesized, "
                        "or use --stick to clone a stick directly)")
    b.add_argument("--journaled", action="store_true", help="make HFS+ journaled (needs force-rw writes)")
    b.add_argument("--label", help="USB name shown in the Mac's startup picker "
                                   "(cosmetic; default: auto from the installer name)")
    b.add_argument("--workdir", help="where to download/extract (default: ~/.cache/slickstick)")
    b.add_argument("-y", "--yes", action="store_true", help="skip the typed wipe confirmation")
    b.set_defaults(func=cmd_build)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
