"""gibmacos source: download a full installer via corpnewt's gibMacOS.

gibMacOS parses Apple's software-update catalogs and downloads
InstallAssistant.pkg straight from Apple. It keeps the (frequently-changing)
catalog URLs current, which is why we lean on it rather than reimplementing
catalog parsing. We drive it interactively so you pick the version in its own
menu, then locate and extract the resulting pkg.

Upstream: https://github.com/corpnewt/gibMacOS  (downloads from Apple; MIT)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .base import Source
from .. import payload

REPO = "https://github.com/corpnewt/gibMacOS"


class GibMacOSSource(Source):
    id = "gibmacos"
    desc = "download a full installer from Apple via gibMacOS (version menu)"

    def __init__(self, clone_dir: Path | None = None):
        self.clone_dir = clone_dir

    def space_needed(self, stream: bool = False) -> int:
        # stream: only the ~17 GB pkg download. copy: pkg + extracted app ~36 GB.
        return 17 * 1024 ** 3 if stream else 36 * 1024 ** 3

    def fetch_pkg(self, workdir: Path) -> Path:
        gib = self._ensure_clone(workdir)
        dl_root = gib / "macOS Downloads"
        before = set(dl_root.rglob("InstallAssistant.pkg")) if dl_root.exists() else set()

        print("\nLaunching gibMacOS — pick your macOS version in its menu, then let it")
        print("finish downloading. slickstick will take over when it exits.\n")
        # gibMacOS.command is a bash wrapper; gibMacOS.py is the actual entry.
        subprocess.run([sys.executable, str(gib / "gibMacOS.py")], cwd=str(gib))

        after = set(dl_root.rglob("InstallAssistant.pkg")) if dl_root.exists() else set()
        new = sorted(after - before, key=lambda p: p.stat().st_mtime, reverse=True)
        pkg = new[0] if new else self._newest(after)
        if pkg is None:
            raise RuntimeError(
                "No InstallAssistant.pkg found under gibMacOS/macOS Downloads. "
                "Did the download complete? You can re-run and point --local at it."
            )
        print(f"Using downloaded pkg: {pkg}")
        return pkg

    def obtain(self, workdir: Path, version: str | None = None) -> Path:
        pkg = self.fetch_pkg(workdir)
        app = payload.extract_pkg(pkg, workdir / "extracted")
        self.build_version = payload.pkg_build_version(pkg)
        return app

    def _ensure_clone(self, workdir: Path) -> Path:
        target = self.clone_dir or (workdir / "gibMacOS")
        if (target / "gibMacOS.command").exists():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cloning gibMacOS into {target}")
        subprocess.run(["git", "clone", "--depth", "1", REPO, str(target)], check=True)
        return target

    @staticmethod
    def _newest(paths: set[Path]) -> Path | None:
        items = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
        return items[0] if items else None
