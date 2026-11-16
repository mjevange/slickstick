"""gibmacos source: download a full installer from Apple via corpnewt's gibMacOS.

gibMacOS parses Apple's software-update catalogs and knows the download URL of
every full ``InstallAssistant.pkg`` Apple ships. It keeps the (frequently
changing) catalog URLs current, which is why we lean on it rather than
reimplementing catalog parsing.

We don't hand off to gibMacOS's interactive menu. Instead we run its parser in
an *isolated subprocess* (lib/sources/giblist.py) that emits the product list as
JSON — so its screen-clears and multiprocessing downloader stay contained — then
show our own menu and download the chosen build's pkg URL ourselves. The version
is picked up front in the wizard, before anything touches the USB.

Upstream: https://github.com/corpnewt/gibMacOS  (downloads from Apple; MIT)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .base import Source
from .. import payload

REPO = "https://github.com/corpnewt/gibMacOS"
HELPER = Path(__file__).resolve().parent / "giblist.py"


def _cache_base() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "slickstick"


def _ensure_gib(cache: Path) -> Path:
    """Clone gibMacOS into a persistent cache dir (reused across runs)."""
    gib = cache / "gibMacOS"
    if not (gib / "gibMacOS.py").exists():
        gib.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cloning gibMacOS into {gib}")
        subprocess.run(["git", "clone", "--depth", "1", REPO, str(gib)], check=True)
    return gib


def _major(version: str) -> int:
    m = re.match(r"(\d+)", version or "")
    return int(m.group(1)) if m else 0


def _ver_key(version: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", version or "")] or [0]


def list_products(arch: str = "silicon", catalog: str = "publicrelease") -> list[dict]:
    """Full installers Apple currently ships, filtered to the chosen arch and
    sorted newest-first. Each item: {title, version, build, url}.

    Arch is a coarse filter by macOS version: Apple Silicon needs macOS 11+
    (Big Sur, the first Apple Silicon release); macOS 27+ is Apple-Silicon-only,
    so it's hidden for Intel. Most versions in between run on both.
    """
    cache = _cache_base()
    gib = _ensure_gib(cache)
    out = cache / "gib_products.json"
    print("  Fetching Apple's installer catalog via gibMacOS…")
    subprocess.run([sys.executable, "-I", str(HELPER), str(gib), catalog, str(out)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    prods = json.loads(out.read_text())

    items = []
    for p in prods:
        if "beta" in p.get("title", "").lower():
            continue
        mj = _major(p.get("version", ""))
        if arch == "silicon" and mj < 11:
            continue
        if arch == "intel" and mj >= 27:
            continue
        items.append(p)
    items.sort(key=lambda p: _ver_key(p.get("version", "")), reverse=True)
    return items


class GibMacOSSource(Source):
    id = "gibmacos"
    desc = "download a full installer from Apple via gibMacOS (version menu)"

    def __init__(self, product: dict | None = None):
        #: {title, version, build, url} chosen in the wizard; None on the CLI
        #: path, where fetch_pkg falls back to the latest release.
        self.product = product
        self.build_version = product["build"] if product else None

    def space_needed(self, stream: bool = False) -> int:
        # stream: only the ~17 GB pkg download. copy: pkg + extracted app ~36 GB.
        return 17 * 1024 ** 3 if stream else 36 * 1024 ** 3

    def _resolve_product(self) -> dict:
        if self.product is None:
            items = list_products("silicon")
            if not items:
                raise RuntimeError("No full installers found in Apple's catalog.")
            self.product = items[0]
            self.build_version = self.product["build"]
        return self.product

    def fetch_pkg(self, workdir: Path) -> Path:
        prod = self._resolve_product()
        workdir.mkdir(parents=True, exist_ok=True)
        pkg = workdir / "InstallAssistant.pkg"
        print(f'Downloading {prod["title"]} {prod["version"]} ({prod["build"]})')
        subprocess.run(
            ["curl", "-L", "--fail", "--progress-bar", "-o", str(pkg), prod["url"]],
            check=True,
        )
        return pkg

    def obtain(self, workdir: Path, version: str | None = None) -> Path:
        pkg = self.fetch_pkg(workdir)
        app = payload.extract_pkg(pkg, workdir / "extracted")
        self.build_version = payload.pkg_build_version(pkg) or self.build_version
        return app
