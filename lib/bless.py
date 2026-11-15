"""HFS+ volume 'bless' via the finderInfo array in the volume header.

Apple Silicon installer media leaves finderInfo all-zero (the firmware keys on
content, not bless) — our key finding. We still provide read/patch here for:
  - diagnostics (confirming the zero-bless on real media), and
  - the EXPERIMENTAL Intel path, where EFI boot historically needs a real bless
    pointing finderInfo at boot.efi.

finderInfo is 8 big-endian uint32s at offset 80 within the HFS+ volume header,
which itself starts 1024 bytes into the partition. CNIDs equal the inode numbers
Linux exposes for files on a mounted HFS+ volume, so `cnid()` is just st_ino.
"""
from __future__ import annotations

import os
import struct
import subprocess

VH_OFFSET = 1024          # HFS+ volume header start, within the partition
FINDERINFO_OFFSET = 80    # finderInfo[] within the volume header
FINDERINFO_ABS = VH_OFFSET + FINDERINFO_OFFSET
FINDERINFO_SIZE = 32      # 8 x uint32


def cnid(path) -> int:
    """Catalog Node ID of a file/dir on a mounted HFS+ volume == its inode."""
    return os.stat(path).st_ino


def read_finderinfo(dev: str) -> list[int]:
    raw = subprocess.run(
        ["sudo", "dd", f"if={dev}", "bs=1", f"skip={FINDERINFO_ABS}",
         f"count={FINDERINFO_SIZE}", "status=none"],
        capture_output=True, check=True,
    ).stdout
    return list(struct.unpack(">8I", raw))


def patch_finderinfo(dev: str, values: dict[int, int]) -> list[int]:
    """Set finderInfo[index]=cnid for each item in `values`. Device must be
    unmounted. Returns the new finderInfo. Writes in place (conv=notrunc)."""
    cur = read_finderinfo(dev)
    for idx, val in values.items():
        if not 0 <= idx <= 7:
            raise ValueError(f"finderInfo index out of range: {idx}")
        cur[idx] = val & 0xFFFFFFFF
    buf = struct.pack(">8I", *cur)
    subprocess.run(
        ["sudo", "dd", f"of={dev}", "bs=1", f"seek={FINDERINFO_ABS}",
         "count=" + str(FINDERINFO_SIZE), "conv=notrunc", "status=none"],
        input=buf, check=True,
    )
    return cur
