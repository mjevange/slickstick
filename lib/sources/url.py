"""url source: download a direct InstallAssistant.pkg URL from Apple's CDN.

This is the "Apple direct" path for when you already have the pkg URL (e.g. from
a catalog listing). It ships no Apple files; it fetches them from Apple at run
time, same as any softwareupdate client.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .base import Source
from .. import payload


class UrlSource(Source):
    id = "url"
    desc = "download a direct InstallAssistant.pkg URL (Apple CDN)"

    def __init__(self, url: str, pkg_size: int = 0):
        self.url = url
        #: real Content-Length from a HEAD at entry (0 = unknown, use estimates)
        self.pkg_size = pkg_size

    def space_needed(self, stream: bool = False) -> int:
        if self.pkg_size:
            # stream: just the download. copy: download + the extracted app,
            # which is essentially the pkg again (the dmg dominates both).
            return self.pkg_size if stream else self.pkg_size * 2
        # fallback when the size is unknown: ~17 GB download, ~36 GB to also extract.
        return 17 * 1024 ** 3 if stream else 36 * 1024 ** 3

    def fetch_pkg(self, workdir: Path) -> Path:
        if not self.url.lower().endswith(".pkg"):
            raise ValueError("--url must point at an InstallAssistant.pkg")
        workdir.mkdir(parents=True, exist_ok=True)
        pkg = workdir / "InstallAssistant.pkg"
        print(f"Downloading {self.url}")
        subprocess.run(
            ["curl", "-L", "--fail", "--progress-bar", "-o", str(pkg), self.url],
            check=True,
        )
        return pkg

    def obtain(self, workdir: Path, version: str | None = None) -> Path:
        pkg = self.fetch_pkg(workdir)
        app = payload.extract_pkg(pkg, workdir / "extracted")
        self.build_version = payload.pkg_build_version(pkg)
        return app
