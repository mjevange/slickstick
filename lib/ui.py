"""Tiny zero-dependency terminal styling + progress.

Pure stdlib ANSI so SlickStick stays self-contained (no Rich, no gum binary).
Colors auto-disable when NO_COLOR is set or stdout isn't a terminal.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

_TTY = sys.stdout.isatty()
_COLOR = _TTY and not os.environ.get("NO_COLOR")


def _c(code: str) -> str:
    return code if _COLOR else ""


RESET = _c("\033[0m")
BOLD = _c("\033[1m")
DIM = _c("\033[2m")
RED = _c("\033[31m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
CYAN = _c("\033[36m")


_BANNER = r"""
  ____  _ _      _     ____  _   _      _
 / ___|| (_) ___| | __/ ___|| |_(_) ___| | __
 \___ \| | |/ __| |/ /\___ \| __| |/ __| |/ /
  ___) | | | (__|   < \___ \| |_| | (__|   <
 |____/|_|_|\___|_|\_\|____/ \__|_|\___|_|\_\
"""

_REPO_URL = "https://github.com/mjevange/slickstick"


def link(text: str, url: str) -> str:
    """A clickable terminal hyperlink (OSC 8) when interactive; else 'text (url)'."""
    if _TTY:
        return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"
    return f"{text} ({url})"


def banner() -> None:
    """The initial splash: ASCII wordmark, tagline, clickable repo link."""
    art = _BANNER.strip("\n")
    for line in art.splitlines():
        print(f"{BOLD}{CYAN}{line}{RESET}")
    print(f"  {BOLD}Build bootable macOS installer USBs{RESET} {DIM}— without a Mac{RESET}")
    print(f"  {DIM}{link('github.com/mjevange/slickstick', _REPO_URL)}{RESET}")
    print()


# --- messages ---------------------------------------------------------------
def title(text: str) -> None:
    print(f"{BOLD}{CYAN}{text}{RESET}")


def heading(text: str) -> None:
    print(f"{BOLD}{text}{RESET}")


def option(i: int, text: str) -> str:
    return f"  {CYAN}{BOLD}{i}{RESET}) {text}"


def step(n: int, total: int, text: str) -> None:
    print(f"{BOLD}{CYAN}[{n}/{total}]{RESET} {BOLD}{text}{RESET}")


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}!{RESET} {msg}")


def err(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {DIM}{msg}{RESET}")


def cmd(line: str) -> None:
    print(f"  {DIM}$ {line}{RESET}")


def danger(msg: str) -> None:
    print(f"{BOLD}{RED}{msg}{RESET}")


# --- formatting -------------------------------------------------------------
def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def _eta(seconds: float) -> str:
    if seconds <= 0 or seconds != seconds:  # 0 or NaN
        return "—"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _bar(frac: float, width: int = 24) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = int(frac * width)
    return "█" * filled + "░" * (width - filled)


# --- progress ---------------------------------------------------------------
def du_bytes(path: str) -> int:
    """Total apparent size of a path, in bytes (via du -sb; 0 on failure)."""
    out = subprocess.run(["du", "-sb", path], capture_output=True, text=True).stdout
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return 0


def _used_bytes(mount_point: str) -> int:
    try:
        st = os.statvfs(mount_point)
        return (st.f_blocks - st.f_bfree) * st.f_frsize
    except OSError:
        return 0


def run_with_progress(command: list[str], mount_point: str, total_bytes: int,
                      label: str = "copy") -> int:
    """Run a command that writes into mount_point, showing a live progress bar
    driven by the destination's growing used-space. Returns the exit code."""
    base = _used_bytes(mount_point)
    start = time.time()
    proc = subprocess.Popen(command)

    def render(copied: int, done: bool = False) -> None:
        elapsed = time.time() - start
        rate = copied / elapsed if elapsed > 0 else 0
        frac = (copied / total_bytes) if total_bytes else 0
        remain = (total_bytes - copied) / rate if rate > 0 and total_bytes else 0
        pct = int(min(1.0, frac) * 100)
        text = (f"  {label} {_bar(frac)} {pct:3d}%  "
                f"{human(copied)}/{human(total_bytes)}  {human(rate)}/s  ETA {_eta(remain)}")
        if _TTY:
            width = shutil.get_terminal_size((80, 20)).columns
            sys.stdout.write("\r" + text[:width] + " " * max(0, width - len(text)))
            sys.stdout.flush()
        elif done or int(elapsed) % 10 == 0:   # piped/CI: occasional lines
            print(text.strip())

    last_line = 0.0
    while proc.poll() is None:
        copied = max(0, _used_bytes(mount_point) - base)
        now = time.time()
        if _TTY or now - last_line >= 10:
            render(copied)
            last_line = now
        time.sleep(0.5)

    rc = proc.wait()
    render(max(0, _used_bytes(mount_point) - base), done=True)
    if _TTY:
        print()
    return rc
