"""recovery source: download an Apple macOS Recovery image for an Intel Mac.

Apple serves Recovery images (BaseSystem.dmg + chunklist) from its recovery
servers. OpenCorePkg's macrecovery.py knows the protocol; we fetch that script
and run it by board-id. The resulting BaseSystem.dmg is a Linux-readable HFS+
volume that usb.build_recovery lays onto a stick — a pkg-free, one-stick Intel
path that boots a real Intel Mac (proven; see docs/intel.md).

Upstream: https://github.com/acidanthera/OpenCorePkg  (macrecovery.py; BSD-3)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

MACRECOVERY_URL = ("https://raw.githubusercontent.com/acidanthera/OpenCorePkg/"
                   "master/Utilities/macrecovery/macrecovery.py")

# macOS version -> (board_id, os_type), from OpenCorePkg's recovery_urls.txt
# (the authoritative list macrecovery itself ships). Newest first. The "note"
# flags our confidence + rough oldest-supported Intel Mac. Monterey is proven on
# hardware and pre-Cryptex; Ventura+ add Cryptexes our file-copy may not satisfy.
# Newest first — pick the newest your Mac supports. "note" is honest VERIFICATION
# STATUS (not a recommendation): Monterey is hardware-verified and Big Sur uses the
# same pre-Cryptex method; Ventura+ add Cryptexes we haven't been able to test
# (no Cryptex-capable Intel Mac on hand) so they're unverified and may not boot.
RECOVERY_VERSIONS = [
    {"name": "Sequoia (15)", "board": "Mac-7BA5B2D9E42DDD94", "os": "default",
     "note": "Cryptex — UNVERIFIED with this build; Intel Macs ~2018-2020"},
    {"name": "Sonoma (14)", "board": "Mac-827FAC58A8FDFA22", "os": "default",
     "note": "Cryptex — UNVERIFIED with this build; Intel Macs ~2018+"},
    {"name": "Ventura (13)", "board": "Mac-B4831CEBD52A0C4C", "os": "default",
     "note": "Cryptex — UNVERIFIED with this build; Intel Macs ~2017+"},
    {"name": "Monterey (12)", "board": "Mac-E43C1C25D4880AD6", "os": "default",
     "note": "verified booting on hardware; pre-Cryptex; Intel Macs ~2015+"},
    {"name": "Big Sur (11)", "board": "Mac-2BD1B31983FE1663", "os": "default",
     "note": "pre-Cryptex (same method as Monterey); Intel Macs ~2013+"},
]


def _cache_base() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "slickstick"


def ensure_macrecovery(cache: Path) -> Path:
    """Fetch macrecovery.py into the cache dir (reused across runs)."""
    script = cache / "macrecovery.py"
    if not script.exists():
        cache.mkdir(parents=True, exist_ok=True)
        print("Fetching macrecovery.py (OpenCorePkg)…")
        subprocess.run(["curl", "-fsSL", MACRECOVERY_URL, "-o", str(script)], check=True)
    return script


def download_recovery(board_id: str, os_type: str, workdir: Path) -> Path:
    """Download an Apple Recovery image and return the BaseSystem.dmg path.

    board_id: the target Mac's board identifier (e.g. Mac-A369DDC4E67F1C45) — get
        it from About This Mac / Recovery `ioreg -l | grep board-id`. Blank uses
        macrecovery.py's generic default board (may not match your Mac).
    os_type: "default" (the macOS the Mac shipped with — safest, pre-Cryptex) or
        "latest" (newest the board supports — may be Cryptex-based, experimental).
    """
    workdir.mkdir(parents=True, exist_ok=True)
    script = ensure_macrecovery(_cache_base())
    out = workdir / "recovery"
    cmd = [sys.executable, str(script)]
    if board_id:
        cmd += ["-b", board_id]
    cmd += ["-os", os_type, "-o", str(out), "download"]
    print(f"Downloading Apple Recovery (board={board_id or 'generic default'}, "
          f"os={os_type}) — this is ~0.6–1 GB from Apple…")
    subprocess.run(cmd, check=True, cwd=str(workdir))
    dmg = out / "BaseSystem.dmg"
    if not dmg.is_file():
        raise RuntimeError("macrecovery.py did not produce a BaseSystem.dmg")
    return dmg
