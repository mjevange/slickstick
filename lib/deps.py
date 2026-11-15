"""Dependency checking with per-distro install hints.

slickstick shells out to standard system tools rather than reimplementing
partitioning/filesystem logic. This module verifies they're present and, when
something is missing, prints the right install command for the user's distro.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    cmd: str          # executable to look for on PATH
    purpose: str      # human-readable reason we need it
    # package name per distro family
    arch: str
    debian: str
    fedora: str


# The tools the build actually invokes.
REQUIRED: tuple[Tool, ...] = (
    Tool("sgdisk", "write the GPT partition table", "gptfdisk", "gdisk", "gdisk"),
    Tool("mkfs.hfsplus", "create the HFS+ installer volume", "hfsprogs", "hfsprogs", "hfsplus-tools"),
    Tool("fsck.hfsplus", "verify the HFS+ volume", "hfsprogs", "hfsprogs", "hfsplus-tools"),
    Tool("bsdtar", "extract InstallAssistant.pkg (xar archive)", "libarchive", "libarchive-tools", "bsdtar"),
    Tool("mkfs.fat", "create the (empty) EFI system partition", "dosfstools", "dosfstools", "dosfstools"),
    Tool("mount", "mount the source and target volumes", "util-linux", "util-linux", "util-linux"),
    Tool("curl", "download from Apple / clone helpers", "curl", "curl", "curl"),
    Tool("git", "fetch the gibMacOS downloader", "git", "git", "git"),
)


def _distro_family() -> str:
    """Best-effort distro family detection from /etc/os-release."""
    data = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    k, _, v = line.partition("=")
                    data[k] = v.strip().strip('"')
    except OSError:
        return "unknown"
    ids = (data.get("ID", "") + " " + data.get("ID_LIKE", "")).lower()
    if any(x in ids for x in ("arch", "manjaro", "endeavouros")):
        return "arch"
    if any(x in ids for x in ("debian", "ubuntu", "mint", "pop")):
        return "debian"
    if any(x in ids for x in ("fedora", "rhel", "centos", "rocky", "alma")):
        return "fedora"
    return "unknown"


def _install_cmd(family: str, pkgs: list[str]) -> str:
    uniq = sorted(set(pkgs))
    if family == "arch":
        return "sudo pacman -S --needed " + " ".join(uniq) + "   # hfsprogs is in the AUR: yay -S hfsprogs"
    if family == "debian":
        return "sudo apt install " + " ".join(uniq)
    if family == "fedora":
        return "sudo dnf install " + " ".join(uniq)
    return "install these packages with your package manager: " + " ".join(uniq)


def kernel_hfsplus_available() -> bool:
    """The in-kernel hfsplus driver is the one hard requirement we can't install
    as a package. It may be built-in, a loadable module, or absent."""
    # built-in?
    try:
        with open("/proc/filesystems", encoding="utf-8") as fh:
            if any("hfsplus" in line for line in fh):
                return True
    except OSError:
        pass
    # loadable module present on disk?
    rel = os.uname().release
    mod = f"/lib/modules/{rel}/kernel/fs/hfsplus"
    if os.path.isdir(mod) or shutil.which("modinfo") and _modinfo_ok("hfsplus"):
        return True
    return False


def _modinfo_ok(name: str) -> bool:
    try:
        return subprocess.run(["modinfo", name], capture_output=True).returncode == 0
    except OSError:
        return False


def check(verbose: bool = True) -> list[Tool]:
    """Return the list of missing tools (empty == all good)."""
    family = _distro_family()
    missing = [t for t in REQUIRED if shutil.which(t.cmd) is None]

    if verbose:
        for t in REQUIRED:
            ok = shutil.which(t.cmd) is not None
            mark = "OK " if ok else "-- "
            print(f"  [{mark}] {t.cmd:<14} {t.purpose}")
        hfs = kernel_hfsplus_available()
        print(f"  [{'OK ' if hfs else '?? '}] kernel hfsplus module  read/write the installer volume")
        if not hfs:
            print("       (could not confirm the hfsplus kernel module — the build may fail to mount)")

    if missing:
        pkgfield = {"arch": "arch", "debian": "debian", "fedora": "fedora"}.get(family)
        pkgs = [getattr(t, pkgfield) if pkgfield else t.arch for t in missing]
        print()
        print(f"Missing {len(missing)} tool(s). On your system ({family}):")
        print("  " + _install_cmd(family, pkgs))

    return missing
