"""local source: use files you already have.

Accepts either:
  - a path to an existing 'Install macOS <name>.app' directory, or
  - a path to an InstallAssistant.pkg (we extract it), or
  - a mounted createinstallmedia volume (we use its app + real root markers).
"""
from __future__ import annotations

from pathlib import Path

from .base import Source
from .. import payload


class LocalSource(Source):
    id = "local"
    desc = "use an existing Install app or a downloaded .pkg"

    def __init__(self, path: str):
        self.path = Path(path).expanduser()

    def fetch_pkg(self, workdir: Path) -> Path | None:
        # The pkg already exists on disk; stream straight from it (~0 scratch).
        return self.path if self.path.suffix == ".pkg" else None

    def space_needed(self, stream: bool = False) -> int:
        if self.path.suffix != ".pkg":
            return 0                          # an existing app is used in place
        return 0 if stream else 18 * 1024 ** 3  # stream reads the pkg in place

    def obtain(self, workdir: Path, version: str | None = None) -> Path:
        p = self.path
        if not p.exists():
            raise FileNotFoundError(f"--local path does not exist: {p}")

        # Directly an app bundle?
        if p.is_dir() and p.name.startswith("Install macOS") and p.suffix == ".app":
            return p

        # A directory that *contains* an app (e.g. a mounted stick)?
        if p.is_dir():
            app = payload.locate_install_app(p)
            if app is not None:
                return app
            raise RuntimeError(f"No 'Install macOS *.app' found under {p}")

        # A pkg file?
        if p.is_file() and p.suffix == ".pkg":
            app = payload.extract_pkg(p, workdir / "extracted")
            self.build_version = payload.pkg_build_version(p)
            return app

        raise RuntimeError(f"Don't know how to use --local path: {p}")
