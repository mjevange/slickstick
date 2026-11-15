<p align="center">
  <img src="assets/banner.svg" alt="SlickStick — build bootable macOS installer USBs, without a Mac" width="100%">
</p>

# SlickStick

**Build bootable macOS installer USBs — without a Mac.**

SlickStick partitions a USB stick, formats it, and lays a macOS installer onto it
so an Apple Silicon Mac boots it straight from the startup picker — all from
Linux, with no Mac involved in the build. The macOS files are downloaded from
Apple at run time (or supplied from files you already have); **no Apple software
ships in this repo.**

It works because of one finding: an Apple Silicon installer volume has **no HFS+
"bless," an empty EFI partition, and a plain partition type** — the firmware
recognizes the installer by its *content*, not by boot metadata. So a faithful
content copy over Linux's own `hfsplus` driver is enough. See
[docs/how-it-works.md](docs/how-it-works.md).

---

## Status — read this

| Path | State |
|------|-------|
| **Apple Silicon**, files from a reference stick | ✅ **Proven** — boots to the installer |
| **Apple Silicon**, files from a pkg (synthesized markers) | 🧪 Builds; root markers synthesized, **not yet boot-verified end to end** |
| **Intel** | 🧪 **Experimental & unverified** — different boot path (EFI + real bless); no Intel Mac has tested it |

This is a tool for making install media for **Macs you own**. It erases the
target USB.

## Requirements

- Linux with the **`hfsplus` kernel module** (built in or loadable)
- `gptfdisk` (`sgdisk`), `hfsprogs` (`mkfs.hfsplus`/`fsck.hfsplus`), `libarchive`
  (`bsdtar`), `dosfstools`, `util-linux`, `curl`, `git`, Python 3.10+
- A USB stick **≥ 16 GB** and ~25 GB of free disk for the download/extract

Check everything at once:

```bash
python3 slickstick.py deps
```

It prints the exact install command for your distro (Arch / Debian / Fedora) if
anything is missing.

## Usage

```bash
# 1. see what USB disks are plugged in
python3 slickstick.py devices

# 2. build — downloads via gibMacOS (pick the version in its menu),
#    then prompts you to choose the target disk and confirm the wipe
sudo python3 slickstick.py build

# target a specific disk, skip the menu
sudo python3 slickstick.py build --device /dev/sdb

# use files you already have (an Install app, a .pkg, or a mounted stick)
sudo python3 slickstick.py build --local "/path/to/Install macOS 27 Golden Gate.app"

# download a direct InstallAssistant.pkg URL from Apple's CDN
sudo python3 slickstick.py build --url "https://updates.cdn-apple.com/.../InstallAssistant.pkg"

# copy the *real* root markers from a known-good stick (most faithful build)
sudo python3 slickstick.py build --reference-stick /dev/sdc
```

The build **detects inserted USB media, lists it, has you pick the target, shows
its details, and makes you type the device name to confirm** before erasing
anything (use `-y` to skip the prompt in scripts).

## macOS sources

| Source | Flag | What it does |
|--------|------|--------------|
| **gibMacOS** (default) | — | Downloads a full installer from Apple via [corpnewt/gibMacOS](https://github.com/corpnewt/gibMacOS); pick the version in its menu |
| **local** | `--local PATH` | Use an existing Install app, a downloaded `.pkg`, or a mounted stick |
| **url** | `--url URL` | Download a direct `InstallAssistant.pkg` URL from Apple's CDN |

New backends slot into `lib/sources/` behind a small interface.

## How it works

The short version: GPT (empty ESP + Apple HFS+) → non-journaled HFS+ →
content-faithful copy of the installer → `fsck`. No bless, no CNID patching on
Apple Silicon. The full write-up, including how the "zero bless" was discovered
and why Intel is harder, is in **[docs/how-it-works.md](docs/how-it-works.md)**.

## Safety

- Refuses targets that are internal disks or carry a mounted `/`, `/boot`, `/home`.
- Won't use the same disk your source files came from.
- Requires a typed confirmation of the device name before wiping.

Always double-check the device — `/dev/sdX` names can shift between plug-ins.

## Legal

SlickStick ships **no Apple software**. It downloads macOS installers from
Apple's own servers at run time, the same way Apple's `softwareupdate` does, and
is intended for creating install media for Macs you own. macOS is © Apple.

## Credits

- [corpnewt/gibMacOS](https://github.com/corpnewt/gibMacOS) — the catalog downloader (MIT)
- The Asahi Linux project — the best public reference on Apple Silicon boot internals

## License

[MIT](LICENSE)
