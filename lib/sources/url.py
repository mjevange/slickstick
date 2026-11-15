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

    def __init__(self, url: str):
        self.url = url

    def obtain(self, workdir: Path, version: str | None = None) -> Path:
        if not self.url.lower().endswith(".pkg"):
            raise ValueError("--url must point at an InstallAssistant.pkg")
        workdir.mkdir(parents=True, exist_ok=True)
        pkg = workdir / "InstallAssistant.pkg"
        print(f"Downloading {self.url}")
        subprocess.run(
            ["curl", "-L", "--fail", "--progress-bar", "-o", str(pkg), self.url],
            check=True,
        )
        return payload.extract_pkg(pkg, workdir / "extracted")
