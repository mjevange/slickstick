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
| **Apple Silicon**, cloned from an installer stick (`--stick`) | ✅ **Proven** — boots to the installer |
| **Apple Silicon**, from a pkg (synthesized markers) | 🧪 Builds; root markers synthesized, **not yet boot-verified end to end** |
| **Intel** | 🧪 **Experimental & unverified** — different boot path (EFI + real bless); no Intel Mac has tested it |

This is a tool for making install media for **Macs you own**. It erases the
target USB.

## Quick start

**Run it once — nothing installed.** Fetches to a temp dir, runs the wizard,
cleans up after itself:

```bash
curl -fsSL https://raw.githubusercontent.com/mjevange/slickstick/main/run.sh | bash
```

**Or install a reusable `slickstick` command** (clones to
`~/.local/share/slickstick`, launcher in `~/.local/bin`):

```bash
curl -fsSL https://raw.githubusercontent.com/mjevange/slickstick/main/install.sh | bash
```

Works in **any shell** — bash, zsh, **fish**. The bootstraps read your terminal
directly (`/dev/tty`), so the wizard stays interactive even through the pipe. To
pass arguments, append `-s --`, e.g. `… | bash -s -- devices`.

<details>
<summary>Prefer process substitution? (bash/zsh or fish)</summary>

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/mjevange/slickstick/main/run.sh)       # bash / zsh
bash (curl -fsSL https://raw.githubusercontent.com/mjevange/slickstick/main/run.sh | psub) # fish
```
</details>

## Requirements

- Linux with the **`hfsplus` kernel module** (built in or loadable)
- `gptfdisk` (`sgdisk`), `hfsprogs` (`mkfs.hfsplus`/`fsck.hfsplus`), `libarchive`
  (`bsdtar`), `dosfstools`, `util-linux`, `curl`, `git`, Python 3.10+
- A USB stick **≥ 16 GB**
- Free disk for the work dir: **~46 GB** for pkg download+extract (gibMacOS/URL),
  ~31 GB for a local `.pkg`, and **almost none** for the stick source (it streams
  straight to the target). SlickStick checks this before wiping anything.
  (A future streaming extractor would cut the pkg footprint to ~17 GB.)

Check everything at once:

```bash
slickstick deps
```

It prints the exact install command for your distro (Arch / Debian / Fedora) if
anything is missing.

## Usage

Just run it — the guided **wizard** walks you through everything:

```bash
slickstick
```

It checks your dependencies, asks where to get macOS, asks the target
architecture, then **detects your inserted USB, lists it, has you pick it, and
makes you type the device name to confirm** before erasing anything. Run it as
your **normal user** — it uses `sudo` only for the privileged partition/format
steps and prompts for your password then. No flags to memorize.

> Running from a git clone instead of the installer? Use `python3 slickstick.py`
> everywhere you see `slickstick` below.

<details>
<summary><b>Advanced / scripting</b> (flag-driven, non-interactive)</summary>

```bash
slickstick devices          # list USB disks
slickstick deps             # check required tools

# build to a specific disk without prompts
slickstick build --device /dev/sdb -y

# choose the macOS source explicitly
slickstick build -d /dev/sdb --local "/path/to/Install macOS ….app"
slickstick build -d /dev/sdb --url "https://.../InstallAssistant.pkg"

# clone an inserted installer stick (real markers — the proven path)
slickstick build -d /dev/sdb --stick /dev/sdc2
```
</details>

## macOS sources

| Source | Flag | What it does |
|--------|------|--------------|
| **gibMacOS** (default) | — | Downloads a full installer from Apple via [corpnewt/gibMacOS](https://github.com/corpnewt/gibMacOS); pick the version in its menu |
| **stick** | `--stick /dev/sdX2` | **Clone an inserted installer stick** — the wizard detects and lists them. Copies the app *and the real root markers*, so it's the boot-verified path |
| **local** | `--local PATH` | Use an existing Install app or a downloaded `.pkg` |
| **url** | `--url URL` | Download a direct `InstallAssistant.pkg` URL from Apple's CDN |

New backends slot into `lib/sources/` behind a small interface.

### Root markers — three tiers

A `createinstallmedia` stick carries small root files (`.IAPhysicalMedia`,
`restore/`, `.VolumeIcon.icns`) that the pkg doesn't:

- **stick source** → copies the **real** markers (the only boot-verified path).
- **any other source** → **synthesizes** them from the app (builds fine, not yet
  boot-verified end to end — see [Status](#status--read-this)).
- **`--reference-stick /dev/sdX`** → advanced escape hatch to copy real markers
  onto an app that came from a non-stick source. Usually unnecessary.

## How it works

The short version: GPT (empty ESP + Apple HFS+) → non-journaled HFS+ →
content-faithful copy of the installer → `fsck`. No bless, no CNID patching on
Apple Silicon. The full write-up, including how the "zero bless" was discovered
and why Intel is harder, is in **[docs/how-it-works.md](docs/how-it-works.md)**.

## The story — why this "couldn't" be done, and how we proved it could

### Why it looked impossible

Every sign said *you need a Mac*:

- `createinstallmedia`, the only official way to build the media, **runs only on
  macOS**.
- Apple Silicon has a **signed, sealed boot chain** specifically designed to
  reject boot media assembled from parts.
- Conventional wisdom treated the HFS+ **"bless"** as a required, Mac-only step —
  and Linux can't even reliably *write* journaled HFS+.
- The installer's boot objects are **Apple-signed**; you can't forge them.

### The hypothesis

The crack in the wall: **booting and building are different problems.** A plain
`dd` clone of a working stick boots fine from Linux — which means the bytes
aren't secret. `createinstallmedia` contributes *procedure*, not magic material.
So if cloning works, maybe the "hard part" is just **arranging Apple's own
already-signed files**, not forging anything.

We chased some dead ends to get there — an "empty blessed skeleton" you could
reuse (impossible: a bless points *at* content, so there's nothing to pre-bake),
and swapping files into a blessed shell (exactly what the sealed chain blocks).
The move that worked: stop treating a known-good stick as a *donor* and use it as
an **oracle** — build a candidate, then *diff* against the real thing.

### What we tested

Reading a real installer stick overturned the core assumption:

- Its HFS+ **`finderInfo` was all zero** — *no bless at all.* (That one `dd` read
  also retired the bless-patching code we'd written, unused.)
- Its **EFI partition was empty**, and the HFS+ partition was a **plain Apple
  type** with no special flags.

No boot markers anywhere → **Apple Silicon recognizes the installer by its
content, not by metadata.** So we built a candidate entirely on Linux — GPT +
**non-journaled** HFS+ (chosen for clean writes, to isolate the one variable) +
a faithful content copy — then diffed it against the real stick: **identical file
tree (895/895 files, 20 symlinks, 0 hard links), `fsck`-clean.**

### The proof

The Linux-built stick, plugged into an M1, **appeared in the startup picker and
booted into the macOS installer's Recovery Assistant** — the same behavior as a
`createinstallmedia` stick. Theory proven: a bootable Apple Silicon installer,
built with no Mac in the loop.

*Honest scope:* proven to **boot to the installer**. A full install to
completion, the pkg-sourced (synthesized-marker) path, and Intel are still open —
see the [Status](#status--read-this) table.

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

**Depends on**

- [corpnewt/gibMacOS](https://github.com/corpnewt/gibMacOS) — downloads macOS installers from Apple (MIT)

**Prior art & inspiration** (ideas followed, not code bundled)

- The [Asahi Linux](https://asahilinux.org) project — the best public reference on Apple Silicon boot internals
- The one-line installer convention of [Homebrew](https://brew.sh), [rustup](https://rustup.rs), and [Oh My Zsh](https://ohmyz.sh) — the `bash <(curl …)` / `bash -c "$(curl …)"` bootstrap
- The `/dev/tty` idiom for keeping prompts interactive even under `curl | bash`

## License

[MIT](LICENSE)
