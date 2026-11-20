#!/usr/bin/env python3
"""SlickStick — build bootable macOS installer USBs, without a Mac.

Partitions a USB (GPT: empty ESP + Apple HFS+), formats HFS+, and copies a
macOS installer onto it so Apple Silicon firmware boots it directly. The macOS
files are fetched from Apple at run time (or supplied locally); none ship here.

Apple Silicon is proven; Intel (recovery + full-installer clone) is proven on
pre-T2 Macs. See docs/how-it-works.md and docs/intel.md.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import struct
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


_APPLE_HFS_GUID = "48465300-0000-11aa-aa11-00306543ecac"   # GPT type "AF00"


def _sudo_bytes(path: Path) -> bytes | None:
    r = subprocess.run(["sudo", "cat", str(path)], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _sudo_exists(path: Path) -> bool:
    return subprocess.run(["sudo", "test", "-e", str(path)]).returncode == 0


def _sudo_is_symlink(path: Path) -> bool:
    return subprocess.run(["sudo", "test", "-L", str(path)]).returncode == 0


def _read_bless(part_path: str) -> list[int] | None:
    """Read the 8 finderInfo CNIDs from the HFS+ volume header (offset 1024+80),
    via sudo so it works without running the whole tool as root."""
    r = subprocess.run(["sudo", "dd", f"if={part_path}", "bs=1024", "skip=1",
                        "count=1", "status=none"], capture_output=True)
    if r.returncode != 0 or len(r.stdout) < 112:
        return None
    return list(struct.unpack(">8I", r.stdout[80:112]))


def cmd_verify(args) -> int:
    """Check a built stick against the recognition rule (AS) + the Intel boot layout.

    Read-only: mounts the HFS+ volume, inspects it, unmounts. Never writes.
    """
    dev = args.device
    info = subprocess.run(["lsblk", "-J", "-o", "PATH,TYPE,FSTYPE,PARTTYPE", dev],
                          capture_output=True, text=True)
    if info.returncode != 0:
        ui.err(f"not a block device: {dev}")
        return 2

    nodes: list[dict] = []
    def _walk(n: dict) -> None:
        nodes.append(n)
        for c in n.get("children") or []:
            _walk(c)
    for d in json.loads(info.stdout).get("blockdevices", []):
        _walk(d)

    this = next((n for n in nodes if n.get("path") == dev), None)
    if this and this.get("type") == "part":
        part = this
    else:
        part = next((n for n in nodes if n.get("type") == "part" and (
            (n.get("parttype") or "").lower() == _APPLE_HFS_GUID
            or n.get("fstype") == "hfsplus")), None)
    if not part:
        ui.err("no Apple HFS+ partition found on this device")
        return 2

    part_path = part["path"]
    ui.heading(f"\nVerifying {part_path}")

    def line(ok: bool, msg: str) -> None:
        mark = f"{ui.GREEN}✓{ui.RESET}" if ok else f"{ui.DIM}·{ui.RESET}"
        print(f"  {mark} {msg}")

    af00 = (part.get("parttype") or "").lower() == _APPLE_HFS_GUID
    hfs = part.get("fstype") == "hfsplus"
    print(f"\n  {ui.BOLD}Geometry{ui.RESET}")
    line(af00, "GPT partition type is Apple HFS+ (AF00)")
    line(hfs, "filesystem is HFS+")

    iaphys_ok = appname_ok = restore_ok = boot_ok = icon_ok = icon_dangling = False
    appname = None
    usb.unmount_if_mounted(part_path)
    mp = Path(tempfile.mkdtemp(prefix="ssverify-"))
    mounted = False
    try:
        subprocess.run(["sudo", "mount", "-t", "hfsplus", "-o", "ro",
                        part_path, str(mp)], check=True)
        mounted = True
        raw = _sudo_bytes(mp / ".IAPhysicalMedia")
        keys: dict = {}
        if raw:
            try:
                keys = plistlib.loads(raw)
            except Exception:
                keys = {}
        iaphys_ok = all(k in keys for k in
                        ("AppName", "ProductVersion", "ProductBuildVersion"))
        appname = keys.get("AppName")
        appname_ok = bool(appname) and _sudo_exists(mp / appname)
        restore_ok = _sudo_exists(mp / "restore")
        boot_ok = _sudo_exists(mp / "System/Library/CoreServices/boot.efi")
        # test -e follows the link, so icon_ok is true only when it RESOLVES to a
        # real file. A recovery BaseSystem ships .VolumeIcon.icns as a *dangling*
        # symlink; flag that case so a broken icon isn't silently read as "absent".
        icon_ok = _sudo_exists(mp / ".VolumeIcon.icns")
        icon_dangling = (not icon_ok) and _sudo_is_symlink(mp / ".VolumeIcon.icns")
    except subprocess.CalledProcessError:
        ui.warn("could not mount the HFS+ volume to inspect its contents")
    finally:
        if mounted:
            usb._umount(mp)
        subprocess.run(["rmdir", str(mp)], capture_output=True)

    finfo = _read_bless(part_path)
    bless_set = bool(finfo) and finfo[1] != 0

    print(f"\n  {ui.BOLD}Apple Silicon recognition rule{ui.RESET}")
    line(iaphys_ok, ".IAPhysicalMedia has AppName + ProductVersion + ProductBuildVersion")
    line(appname_ok, f"AppName resolves to {appname}" if appname
         else "AppName resolves to a real Install macOS *.app")
    if restore_ok:
        line(True, "restore/ present (legacy marker, optional)")
    else:
        print(f"  {ui.DIM}·{ui.RESET} restore/ absent "
              f"{ui.DIM}(optional — not required for recognition){ui.RESET}")

    print(f"\n  {ui.BOLD}Intel boot{ui.RESET}")
    line(boot_ok, "System/Library/CoreServices/boot.efi present")
    if bless_set:
        line(True, f"HFS+ bless set (boot.efi CNID {finfo[1]}) — optional T2 insurance")
    else:
        print(f"  {ui.DIM}·{ui.RESET} HFS+ bless not set "
              f"{ui.DIM}(optional; pre-T2 Macs don't need it){ui.RESET}")

    print(f"\n  {ui.BOLD}Cosmetic{ui.RESET}")
    if icon_dangling:
        print(f"  {ui.YELLOW}!{ui.RESET} .VolumeIcon.icns is a dangling symlink "
              f"{ui.DIM}(broken — the volume will show a generic icon){ui.RESET}")
    else:
        line(icon_ok, ".VolumeIcon.icns present (branded startup icon)")

    as_ready = af00 and hfs and iaphys_ok and appname_ok
    intel_ready = af00 and hfs and boot_ok
    print()
    if as_ready:
        ui.ok("Apple Silicon: READY — meets the recognition rule")
    else:
        ui.info("Apple Silicon: not an AS installer (markers incomplete)")
    if intel_ready:
        ui.ok("Intel: boot layout present (boot.efi)")
    else:
        ui.info("Intel: no boot.efi boot layout")
    if iaphys_ok and appname_ok:
        ui.ok("Clone source: usable — real installer markers present")
    if not (as_ready or intel_ready):
        ui.err("This stick won't boot as a macOS installer.")
        return 1
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
    name = Path(target).name
    # Re-prompt on a mistype rather than cancelling outright — a wrong keystroke
    # here must not throw away a long download or force starting the wizard over.
    while True:
        try:
            ans = input(f"\nType the device name '{name}' to confirm wipe "
                        "(or press Enter to cancel): ").strip()
        except EOFError:
            return False
        if not ans:
            return False
        if ans == name:
            return True
        ui.warn(f"That didn't match '{name}'. Try again, or press Enter to cancel.")


def _default_work_base() -> Path:
    """Default parent for the work dir — the user's cache, NOT /tmp (often a tiny
    tmpfs that can't hold a ~18 GB extraction)."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "slickstick"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _finish_line(target: str) -> None:
    """Best-effort eject the (already-unmounted) USB and print the shared finished
    line, so the user knows it's physically safe to pull."""
    if usb.power_off(target):
        ui.ok(f"Finished — {target} is ejected; safe to remove.")
    else:
        ui.ok(f"Finished — {target} is unmounted; safe to remove.")


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
    build_ok = False
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
        build_ok = True
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
        # "stream" means keep nothing — drop our temp work dir (and the pkg we
        # downloaded into it), but only on SUCCESS. On a failed build we keep it
        # so a retry can reuse the already-downloaded pkg instead of re-pulling
        # ~17 GB (nobody should re-download after a hiccup). "copy" keeps it
        # either way (that's the whole point).
        if created_work and streaming and build_ok:
            shutil.rmtree(work, ignore_errors=True)

    print()
    usb.unmount_disk(target)
    _finish_line(target)
    print("  Test by booting it on the target Mac (hold power -> Options).")
    return 0


def _run_recovery(target: str, label: str, yes: bool, dmg: Path | None = None,
                  board: str | None = None, os_type: str = "default",
                  workdir: str | None = None) -> int:
    """Safety-check, (download,) confirm, build, and unmount an Intel recovery stick.
    Pass a local ``dmg`` (CLI) or ``board``/``os_type`` to download (wizard).
    Order: safety -> download -> confirm -> build -> unmount, so nothing heavy runs
    until committed and a bad download fails before anything is erased."""
    try:
        usb.assert_safe_target(target, None)
    except usb.BuildError as e:
        print(f"SAFETY: {e}")
        return 1
    work = Path(workdir) if workdir else Path(
        tempfile.mkdtemp(prefix="rec-", dir=str(_default_work_base())))
    work.mkdir(parents=True, exist_ok=True)
    if dmg is None:
        try:
            dmg = recovery.download_recovery(board, os_type, work)
        except Exception as e:  # noqa: BLE001
            print(f"Recovery download failed: {e}")
            return 1
    if not _confirm_wipe(target, yes):
        print("Cancelled.")
        return 1
    try:
        usb.ensure_sudo()
        with usb.SudoKeepAlive():
            usb.build_recovery(target, Path(dmg), work, label)
            usb.unmount_disk(target)
    except Exception as e:  # noqa: BLE001
        print(f"Build failed: {e}")
        return 1
    print()
    _finish_line(target)
    print("  Boot it on the Intel Mac: hold Option, pick the recovery.")
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
    return _run_recovery(target, args.label or DEFAULT_LABEL, args.yes,
                         dmg=dmg, workdir=args.workdir)


def _download_full_pkg(major: int, work: Path) -> Path:
    """Download the newest full-installer pkg for a macOS major version (via
    gibMacOS), so it matches the recovery BaseSystem of the same version."""
    items = gibmacos.list_products("intel")
    match = next((p for p in items
                  if re.match(r"(\d+)", p.get("version", "") or "")
                  and int(re.match(r"(\d+)", p["version"]).group(1)) == major), None)
    if not match:
        raise RuntimeError(f"no macOS {major} full installer found in Apple's catalog")
    return gibmacos.GibMacOSSource(match).fetch_pkg(work)


def _download_cache() -> Path:
    """Stable location for reusable downloads (BaseSystem.dmg, InstallAssistant.pkg),
    separate from the per-run (ephemeral) build scratch."""
    base = _default_work_base() / "downloads"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _cached_download(key: str, fetch, min_bytes: int, scratch: Path,
                     no_cache: bool) -> Path:
    """Reuse a previously downloaded file, or fetch it and cache it for next time.

    A multi-GB download should be a one-time cost per version, resilient to any
    later failure in the build — a hiccup must never force re-pulling ~12 GB. We
    key the cached file by version/board (``key``) and reuse it when a complete
    copy is already present. ``fetch(dir)`` downloads into ``dir`` and returns the
    produced file; we promote it to the cache by an atomic rename *only after it
    returns successfully*, so a partial/interrupted download never lands under the
    final name and can't be wrongly reused (``min_bytes`` is a cheap truncation
    guard on top). ``no_cache`` bypasses the cache: the file is fetched straight
    into ``scratch`` (the ephemeral build dir) and never persisted.
    """
    if no_cache:
        return fetch(scratch)
    cache = _download_cache()
    final = cache / key
    if final.is_file() and final.stat().st_size >= min_bytes:
        ui.ok(f"reusing cached {final.name} ({ui.human(final.stat().st_size)}) "
              f"— skipping download")
        return final
    tmp = Path(tempfile.mkdtemp(prefix="dl-", dir=str(cache)))
    try:
        produced = fetch(tmp)
        size = produced.stat().st_size
        if size < min_bytes:
            raise RuntimeError(f"{produced.name} looks truncated ({size} bytes) — "
                               f"not caching it")
        final.unlink(missing_ok=True)
        os.replace(produced, final)   # same filesystem (tmp is under cache) → rename
        return final
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_intel_full(target: str, label: str, yes: bool, dmg: Path | None = None,
                    pkg: Path | None = None, board: str | None = None,
                    os_type: str = "default", pkg_major: int | None = None,
                    workdir: str | None = None, no_cache: bool = False,
                    keep_files: bool = True) -> int:
    """Safety-check, (download,) confirm, build, and unmount a from-scratch Intel
    full *offline* installer (recovery BaseSystem + pkg; no Mac, no clone).

    Supply ``dmg``/``pkg`` as files, or ``board``/``os_type``/``pkg_major`` to
    download both. Downloaded files are cached under ~/.cache/slickstick/downloads
    and reused on later runs (so a build failure never forces re-pulling ~12 GB).
    ``keep_files`` keeps those downloads after a *successful* build; otherwise they
    are removed (a failed build always keeps them so a retry is free). ``no_cache``
    opts out of the persistent cache entirely (downloads go to scratch, nothing is
    kept) and therefore forces cleanup. Downloads run after the wipe confirm but
    before anything is erased, so a bad download fails before the target is touched."""
    try:
        usb.assert_safe_target(target, None)
    except usb.BuildError as e:
        print(f"SAFETY: {e}")
        return 1
    created_work = workdir is None
    work = Path(workdir) if workdir else Path(
        tempfile.mkdtemp(prefix="intelfull-", dir=str(_default_work_base())))
    work.mkdir(parents=True, exist_ok=True)
    # Caching is on by default (a build hiccup shouldn't re-pull ~12 GB); set
    # SLICKSTICK_NO_CACHE=1 to opt out (downloads go to scratch, nothing persists —
    # which also means there is nothing to keep).
    no_cache = no_cache or (os.environ.get("SLICKSTICK_NO_CACHE", "") not in ("", "0"))
    if no_cache:
        keep_files = False
    # Confirm the wipe BEFORE downloading: a mistyped confirmation must never cost
    # a multi-GB re-download, and the download shouldn't start until you've
    # committed to a target. Nothing is erased until the build step below.
    if not _confirm_wipe(target, yes):
        print("Cancelled.")
        return 1
    downloaded: list[Path] = []   # files WE fetched into the cache (cleanup candidates)
    try:
        if dmg is None:
            dmg = _cached_download(
                f"BaseSystem-{board or 'generic'}-{os_type}.dmg",
                lambda d: recovery.download_recovery(board, os_type, d),
                100_000_000, work, no_cache)
            downloaded.append(Path(dmg))
        if pkg is None:
            pkg = _cached_download(
                f"InstallAssistant-macos{pkg_major}.pkg",
                lambda d: _download_full_pkg(pkg_major, d),
                1_000_000_000, work, no_cache)
            downloaded.append(Path(pkg))
    except Exception as e:  # noqa: BLE001
        print(f"Download failed: {e}")
        return 1
    build_ok = False
    try:
        usb.ensure_sudo()
        with usb.SudoKeepAlive():
            usb.build_intel_full(target, Path(dmg), Path(pkg), work, label)
            usb.unmount_disk(target)
        build_ok = True
    except Exception as e:  # noqa: BLE001
        print(f"Build failed: {e}")
        return 1
    finally:
        # Only ever clean up on SUCCESS; a failed build keeps everything (scratch
        # AND downloads) so a retry doesn't re-pull ~12 GB or re-extract.
        if build_ok:
            if created_work:
                shutil.rmtree(work, ignore_errors=True)   # ephemeral decode/extract junk
            if not keep_files and not no_cache:
                for f in downloaded:
                    try:
                        f.unlink()
                    except OSError:
                        pass
    print()
    if downloaded:
        if keep_files:
            ui.info(f"Kept the downloaded files in {_download_cache()} "
                    f"(reused automatically next run).")
        elif not no_cache:
            ui.info("Cleaned up the downloaded files (re-downloaded if you build again).")
    _finish_line(target)
    name = (payload.pkg_app_name(pkg) or "macOS").replace("Install macOS ", "").strip() or "macOS"
    print("  Boot it on the Intel Mac (hold Option), pick this stick, then in macOS")
    print(f'  Utilities choose "Reinstall macOS {name}" ▸ Continue ▸ pick the target')
    print("  disk. It installs offline from the stick — no Terminal. (Erase or")
    print("  repartition the target first with Disk Utility in that same window.)")
    print()
    ui.warn("T2 Macs (2018+) may need a one-time internet connection during install")
    print("  (the T2 pulls a firmware update from Apple). If it stalls ~50% at \"An error")
    print("  occurred installing macOS\", connect Ethernet/Wi-Fi and retry. Pre-flight in")
    print("  Utilities ▸ Terminal: bash \"/Volumes/<name>/firmware-check\".")
    return 0


def _execute_intel_full(args) -> int:
    """CLI: build a from-scratch Intel full installer from a BaseSystem.dmg plus
    an InstallAssistant.pkg (triggered by --recovery together with --local)."""
    dmg = Path(args.recovery)
    pkg = Path(args.local)
    if not dmg.is_file():
        ui.err(f"BaseSystem.dmg not found: {dmg}")
        return 1
    if pkg.suffix != ".pkg" or not pkg.is_file():
        ui.err("a full-installer build needs --local to be an InstallAssistant.pkg")
        return 1
    label = args.label or (payload.pkg_app_name(pkg) or DEFAULT_LABEL)
    target = args.device or _choose_target()
    if not target:
        print("Cancelled.")
        return 1
    return _run_intel_full(target, label, args.yes, dmg, pkg, workdir=args.workdir)


def cmd_build(args) -> int:
    """Flag-driven build (power users)."""
    if deps.missing():
        print("Missing dependencies — run: slickstick deps")
        return 1
    if getattr(args, "recovery", None):
        if getattr(args, "local", None):
            return _execute_intel_full(args)
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


def _ask_keep_files(what: str, approx: str) -> bool:
    """Ask whether to keep the big reusable download/extract afterward.

    One consistent question across arches (the only difference is what gets kept:
    the unpacked installer on silicon, the downloaded BaseSystem+pkg on Intel).
    Returns True to keep, False to clean up. Cleanup only ever happens on a
    *successful* build — a failed build always keeps the files so a retry is free.
    """
    ui.heading(f"\nKeep the {what} afterward?")
    return _menu([
        "Keep them — I might make more sticks, or want the files for other reasons",
        f"Clean up — just this one stick (frees ~{approx} after a successful build)",
    ], "  Keep or clean up") == 0


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
    choice = _menu([
        "Download macOS Recovery from Apple (proven — boots real Intel Macs, 1 stick)",
        "Clone an inserted createinstallmedia stick (full offline installer, experimental)",
        "Full offline installer from a BaseSystem.dmg + a .pkg (no Mac, no clone — experimental)",
    ])
    if choice == 0:
        return _wizard_intel_recovery()
    if choice == 1:
        return _wizard_intel_clone()
    return _wizard_intel_full()


def _wizard_intel_recovery() -> dict:
    """Download an Apple Recovery image and build a bootable recovery stick."""
    print("\n  Downloads an Apple Recovery image and builds a bootable recovery")
    print("  stick (boots to macOS Recovery -> reinstall macOS over the internet).")
    ui.heading("\nWhich macOS version? (pick the NEWEST your target Mac supports)")
    print("  Monterey is hardware-verified; Ventura+ use Cryptexes we couldn't test —")
    print("  if a Cryptex version won't boot, drop to Monterey-era and let us know.")
    opts = [f'{v["name"]} — {v["note"]}' for v in recovery.RECOVERY_VERSIONS]
    opts.append("Let my Mac pick the newest it supports (I'll enter its board-id)")
    sel = _menu(opts, "  Version")
    if sel < len(recovery.RECOVERY_VERSIONS):
        v = recovery.RECOVERY_VERSIONS[sel]
        board, os_type = v["board"], v["os"]
    else:
        print("\n  board-id is in About This Mac -> System Report, or Recovery's")
        print("  Terminal:  ioreg -l | grep board-id")
        board = input("  Target Mac's board-id (e.g. Mac-A369DDC4E67F1C45): ").strip()
        os_type = "latest"
    ui.heading("\nNow pick the TARGET USB to erase.")
    target = _choose_target(allow_nav=True)
    if not target:
        raise _Cancelled
    # Download happens later (after the wipe confirm) so nothing heavy runs until
    # you're committed, and a bad board-id/download fails before anything's erased.
    return {"_recovery": True, "target": target, "board": board,
            "os_type": os_type, "label": "macOS Recovery"}


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


def _wizard_intel_full() -> dict:
    """From-scratch Intel full OFFLINE installer: a recovery BaseSystem.dmg
    (bootable root) + an InstallAssistant.pkg (the offline payload). No Mac, no
    clone source. Both files can be downloaded, or supplied as paths."""
    print("\n  Builds a one-stick Intel FULL OFFLINE installer — no Mac, no clone —")
    print("  from a recovery BaseSystem (bootable root) + a full-installer pkg")
    print("  (the offline payload).")
    print(f"  {ui.DIM}Proven on a pre-T2 iMac: web download -> build on Linux -> boot")
    print(f"  -> offline install -> booted to Setup Assistant (no Mac anywhere).{ui.RESET}")
    if _menu([
        "Download both for me (just pick a macOS version)",
        "I'll provide the files (BaseSystem.dmg + .pkg paths)",
    ]) == 0:
        return _wizard_intel_full_download()
    return _wizard_intel_full_local()


def _wizard_intel_full_download() -> dict:
    """Pick one macOS version; download the matching BaseSystem + pkg, then build.
    No board-id typing — the version maps to a board-id behind the scenes."""
    ui.heading("\nWhich macOS version? (must boot your target Intel Mac)")
    print("  Monterey is hardware-verified; Ventura+ use Cryptexes we couldn't test.")
    opts = [f'{v["name"]} — {v["note"]}' for v in recovery.RECOVERY_VERSIONS]
    sel = _menu(opts, "  Version")
    v = recovery.RECOVERY_VERSIONS[sel]
    major = int(re.search(r"\((\d+)\)", v["name"]).group(1))
    name = v["name"].split(" (")[0]
    label = _prompt_label(f"Install macOS {name}", "Enter for the default")
    keep_files = _ask_keep_files("downloaded macOS files (~12 GB)", "12 GB")
    ui.heading("\nNow pick the TARGET USB to erase.")
    target = _choose_target(allow_nav=True)
    if not target:
        raise _Cancelled
    # BaseSystem + pkg download after the wipe confirm (in _run_intel_full), so a
    # bad download fails before anything is erased.
    return {"_intel_full": True, "target": target, "label": label,
            "board": v["board"], "os_type": v["os"], "pkg_major": major,
            "keep_files": keep_files}


def _wizard_intel_full_local() -> dict:
    """Supply the two files as paths."""
    dmg = Path(_input_path("\n  Path to the BaseSystem.dmg (Tab to complete): "))
    if not dmg.is_file():
        ui.warn(f"Not a file: {dmg}")
        raise _StartOver
    pkg = Path(_input_path("  Path to the InstallAssistant.pkg (Tab to complete): "))
    if pkg.suffix != ".pkg" or not pkg.is_file():
        ui.warn("That must be an InstallAssistant.pkg.")
        raise _StartOver
    label = _prompt_label(payload.pkg_app_name(pkg) or DEFAULT_LABEL,
                          "Enter for the default")
    ui.heading("\nNow pick the TARGET USB to erase.")
    target = _choose_target(allow_nav=True)
    if not target:
        raise _Cancelled
    return {"_intel_full": True, "target": target, "label": label,
            "dmg": str(dmg), "pkg": str(pkg)}


def _run_download_only(dest: Path, want_pkg: bool, want_recovery: bool,
                       major: int | None, board: str | None,
                       os_type: str = "default") -> int:
    """Download the raw macOS files into a folder the user picks — no USB, no build.

    The files can later feed the "provide the files" build path, or be archived,
    inspected, or used on another machine. Unlike the build cache (hidden plumbing),
    these land where the user asked and are theirs to keep."""
    dest = dest.expanduser()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        ui.err(f"can't use {dest}: {e}")
        return 1
    got: list[Path] = []
    try:
        if want_recovery:
            got.append(Path(recovery.download_recovery(board, os_type, dest)))
        if want_pkg:
            got.append(Path(_download_full_pkg(major, dest)))
    except Exception as e:  # noqa: BLE001
        print(f"Download failed: {e}")
        return 1
    print()
    ui.ok("Downloaded:")
    for p in got:
        try:
            size = f"  ({ui.human(p.stat().st_size)})"
        except OSError:
            size = ""
        print(f"  {p}{size}")
    print('\n  Build a stick from these later with "provide the files"')
    print("  (wizard: Intel → Full offline installer → I'll provide the files;")
    print("   or: build --recovery <BaseSystem.dmg> --local <pkg>).")
    return 0


def _wizard_download_only() -> dict:
    """Pick which macOS files + a version + a destination; download, no USB."""
    print("\n  Downloads the raw macOS files (no USB, no build) into a folder you")
    print("  choose — to archive, inspect, build on another machine, or make a stick")
    print('  later via the "provide the files" option.')
    which = _menu([
        "Full installer pkg (InstallAssistant.pkg)",
        "Recovery image (BaseSystem.dmg)",
        "Both",
    ], "  Which files")
    want_pkg = which in (0, 2)
    want_recovery = which in (1, 2)
    ui.heading("\nWhich macOS version?")
    print("  Monterey is hardware-verified; Ventura+ use Cryptexes we couldn't test.")
    opts = [f'{v["name"]} — {v["note"]}' for v in recovery.RECOVERY_VERSIONS]
    v = recovery.RECOVERY_VERSIONS[_menu(opts, "  Version")]
    major = int(re.search(r"\((\d+)\)", v["name"]).group(1))
    raw = _input_path("\n  Download to which folder "
                      "(Tab to complete, Enter = current dir): ").strip()
    dest = Path(raw) if raw else Path.cwd()
    return {"_download_only": True, "dest": str(dest), "want_pkg": want_pkg,
            "want_recovery": want_recovery, "major": major,
            "board": v["board"], "os_type": v["os"]}


def cmd_download(args) -> int:
    """CLI: download the raw macOS files only (no USB). Pick a version by name/major
    against the known recovery versions; defaults to the full-installer pkg."""
    want_pkg = args.pkg or not args.recovery
    want_recovery = args.recovery
    key = (args.version or "").strip().lower()
    match = None
    for v in recovery.RECOVERY_VERSIONS:
        mj = re.search(r"\((\d+)\)", v["name"]).group(1)
        if key and (key in v["name"].lower() or key == mj):
            match = v
            break
    if key and match is None:
        ui.err(f"unknown version '{args.version}'. Known: "
               + ", ".join(v["name"] for v in recovery.RECOVERY_VERSIONS))
        return 1
    if match is None:
        if want_recovery:
            ui.err("--recovery needs --version (a recovery image is board-specific). "
                   "Known: " + ", ".join(v["name"] for v in recovery.RECOVERY_VERSIONS))
            return 1
        match = recovery.RECOVERY_VERSIONS[-2]  # Monterey — the verified default
        ui.info(f"no --version given; defaulting to {match['name']}.")
    major = int(re.search(r"\((\d+)\)", match["name"]).group(1))
    dest = Path(args.output) if args.output else Path.cwd()
    return _run_download_only(dest, want_pkg, want_recovery, major,
                              match["board"], match["os"])


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

    try:
        action = _menu(["Build a bootable macOS installer USB",
                        "Download macOS files only (no USB)",
                        "Check an existing stick (verify — read-only)"],
                       "What would you like to do?")
    except (_Cancelled, _StartOver, KeyboardInterrupt):
        print("\nCancelled.")
        return 1
    if action == 1:
        try:
            plan = _wizard_download_only()
        except (_Cancelled, _StartOver, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
        return _run_download_only(Path(plan["dest"]), plan["want_pkg"],
                                  plan["want_recovery"], plan.get("major"),
                                  plan.get("board"), plan.get("os_type", "default"))
    if action == 2:
        return _wizard_verify()

    while True:
        try:
            plan = _wizard_collect(args)
        except _StartOver:
            print("\n↺  Starting over.\n")
            continue
        except (_Cancelled, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
        if plan.pop("_intel_full", False):
            dmg, pkg = plan.get("dmg"), plan.get("pkg")
            return _run_intel_full(
                plan["target"], plan["label"], yes=False,
                dmg=Path(dmg) if dmg else None,
                pkg=Path(pkg) if pkg else None,
                board=plan.get("board"), os_type=plan.get("os_type", "default"),
                pkg_major=plan.get("pkg_major"),
                keep_files=plan.get("keep_files", True))
        if plan.pop("_recovery", False):
            return _run_recovery(plan["target"], plan["label"], yes=False,
                                 board=plan["board"], os_type=plan["os_type"])
        return _execute_build(**plan)


def _wizard_verify() -> int:
    """Pick a stick from the wizard and run the read-only verifier."""
    disks = usb.list_removable_disks()
    if not disks:
        ui.warn("No removable/USB disks found. Plug the stick in and try again.")
        return 1
    ui.heading("\nWhich stick should I check?  (read-only — nothing is erased)")
    for i, d in enumerate(disks):
        idx = f"{ui.CYAN}{ui.BOLD}[{i}]{ui.RESET}"
        print(f"  {idx} /dev/{d['name']:<6} {_human(d['size']):>9}  "
              f"{(d.get('model') or '').strip()}")
    while True:
        try:
            sel = input(f"Pick a disk [0-{len(disks) - 1}], blank = cancel: ").strip()
        except EOFError:
            return 1
        if not sel:
            return 1
        if sel.isdigit() and int(sel) < len(disks):
            return cmd_verify(argparse.Namespace(device="/dev/" + disks[int(sel)]["name"]))
        print(f"  Enter 0 to {len(disks) - 1}, or blank to cancel.")


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
        # Keep → copy (unpack the installer and keep it); clean up → stream (write
        # straight to the USB, ~0 extra scratch). Same question as the Intel path.
        strategy = "copy" if _ask_keep_files("unpacked installer (~18 GB)", "18 GB") \
            else "stream"

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

    v = sub.add_parser("verify", help="check a built stick against the recognition rule (read-only)")
    v.add_argument("device", help="the stick or its HFS+ partition, e.g. /dev/sdc or /dev/sdc2")
    v.set_defaults(func=cmd_verify)

    b = sub.add_parser("build", help="flag-driven build (advanced / scripting)")
    b.add_argument("-d", "--device", help="target disk, e.g. /dev/sdb (prompted if omitted)")
    b.add_argument("--arch", choices=["silicon", "intel"], default="silicon",
                   help="silicon, or intel (recovery + clone, proven on pre-T2 Macs)")
    b.add_argument("--strategy", choices=["copy", "stream"], default="copy",
                   help="copy: extract to the work dir then copy onto the USB (default). "
                        "stream: write straight onto the USB from the pkg (~0 scratch space)")
    src = b.add_argument_group("macOS source (default: gibmacos)")
    src.add_argument("--stick", help="clone an inserted installer stick's partition, e.g. /dev/sdc2 (real markers)")
    src.add_argument("--local", help="path to an existing Install app or a .pkg")
    src.add_argument("--url", help="direct InstallAssistant.pkg URL (Apple CDN)")
    src.add_argument("--recovery", help="Intel: a BaseSystem.dmg (from macrecovery.py). "
                                        "Alone → a recovery stick; together with --local <pkg> "
                                        "→ a from-scratch full offline installer (no Mac, no clone)")
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

    dl = sub.add_parser("download", help="download the raw macOS files only (no USB)")
    dl.add_argument("--pkg", action="store_true",
                    help="download the full-installer InstallAssistant.pkg (default)")
    dl.add_argument("--recovery", action="store_true",
                    help="download the recovery BaseSystem.dmg (needs --version)")
    dl.add_argument("--version",
                    help="macOS version by name or major, e.g. 'monterey' or '12' "
                         "(required for --recovery; defaults to Monterey for --pkg)")
    dl.add_argument("-o", "--output", help="destination folder (default: current dir)")
    dl.set_defaults(func=cmd_download)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
