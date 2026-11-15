"""Pluggable macOS source backends.

A Source knows how to produce an extracted 'Install macOS <name>.app' directory
on the local disk. The USB builder is agnostic to where it came from.

Register new backends in lib/sources/__init__.py.
"""
from __future__ import annotations

import abc
from pathlib import Path


class Source(abc.ABC):
    #: short id used on the CLI (e.g. --source gibmacos)
    id: str = ""
    #: one-line description for help/listing
    desc: str = ""

    @abc.abstractmethod
    def obtain(self, workdir: Path, version: str | None = None) -> Path:
        """Return the path to an extracted 'Install macOS <name>.app' directory.

        May download and/or extract into ``workdir``. ``version`` is a backend
        specific hint (e.g. "27.2.1") and may be ignored by sources that can't
        select a version.
        """

    def list_versions(self) -> list[str]:  # optional
        """Return selectable version strings, if the backend supports it."""
        return []
