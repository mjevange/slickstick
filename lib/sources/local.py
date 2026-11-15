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

    def space_needed(self) -> int:
        # A .pkg is extracted two-stage: Payload (~14) + app (~17) in the work
        # dir (the pkg itself lives elsewhere). An existing app is used in place.
        return 31 * 1024 ** 3 if self.path.suffix == ".pkg" else 0

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
            return payload.extract_pkg(p, workdir / "extracted")

        raise RuntimeError(f"Don't know how to use --local path: {p}")
