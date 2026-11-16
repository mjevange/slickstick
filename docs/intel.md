# Intel Macs — status & findings

SlickStick's proven path is **Apple Silicon**. Intel support is **experimental**;
this page documents what we've learned, because Intel boots differently — and some
of it corrects widespread assumptions.

## How an Intel Mac boots USB installer media

Intel Macs boot a USB installer from the HFS+ volume's
`System/Library/CoreServices/boot.efi`. The conventional wisdom is that this
requires an HFS+ **bless** — three CNIDs written into the volume header's
`finderInfo` pointing at `boot.efi` and its folder.

**Finding — the bless is *not* required (verified on a pre-T2 Intel Mac).**
Zeroing the `finderInfo` bless on a known-good `createinstallmedia` stick, it
*still* appeared in the startup manager (with icon) and booted. The firmware finds
`boot.efi` at the conventional path on its own. This mirrors Apple Silicon, which
recognizes installers by **content/layout**, not boot metadata.

> Caveat: verified on an **old (pre-T2)** Intel Mac. T2 Macs (2018+) enforce
> stricter secure boot and were not tested, so SlickStick writes the bless anyway
> as harmless insurance.

The GPT layout is identical to Apple Silicon: a 200 MB **EFI System Partition
(left empty)** + an Apple **HFS+ (`AF00`)** partition. Intel does **not** boot from
the ESP.

## Setting the bless from Linux (no special tool)

The bless is just values in the HFS+ volume header. On the Linux `hfsplus` driver a
file's HFS+ **CNID equals its inode**, so `stat` yields the CNIDs, and we write them
into `finderInfo` (8×big-endian uint32 at partition offset **1104**):

- `finderInfo[0]` = CNID of the folder holding `boot.efi` (`System/Library/CoreServices`)
- `finderInfo[1]` = CNID of `boot.efi`
- `finderInfo[5]` = same folder CNID

Byte-validated against a real `createinstallmedia` stick. No `bless`,
`grub-macbless`, or Mac required. (`lib/bless.py`.)

## How SlickStick builds Intel media

Intel needs the **full expanded `createinstallmedia` tree** on the HFS+ volume
(`boot.efi`, `BaseSystem/`, `System/`, `Firmware/`, the installer app) — not just
the app. SlickStick produces this by **cloning an existing `createinstallmedia`
stick** (the `stick` source): it copies the whole tree (skipping HFS+
internal/cache dirs) and optionally blesses it. See `build_intel_clone`.

Wizard: choose **Intel** → **Clone from an inserted stick** → pick the installer
stick → target a *different* USB.

## Why not build Intel straight from a downloaded pkg?

Modern macOS (Big Sur and later) ships the OS inside `InstallAssistant.pkg` →
`SharedSupport.dmg` as a sealed **MobileAsset** (a large zip under
`com_apple_MobileAsset_MacSoftwareUpdate/`) alongside Apple's **UpdateBrain**.
`createinstallmedia` runs Apple's own installer logic *on macOS* to turn that into
bootable media. Reconstructing that output on Linux means dealing with sealed
system volumes / cryptexes — substantial reverse-engineering.

**So building Intel media from a raw *full-installer* pkg is a future research
item, not currently supported** — cloning a stick sidesteps it. But there's a
more promising pkg-free route below.

## A promising pkg-free path: Apple Recovery (`macrecovery.py`)

Apple also serves **Recovery images** (`BaseSystem.dmg` + `BaseSystem.chunklist`)
from its recovery servers — *not* the sealed MobileAsset format — and they're
downloadable on Linux with **`macrecovery.py`** (from OpenCorePkg), by board-id.

Apple's own firmware boots these **directly**: per Apple's security
documentation, the UEFI firmware verifies the chunklist signature, hashes the
`BaseSystem.dmg` chunk-by-chunk, then **mounts it as a RAM disk and launches
`boot.efi`**. On a *real* Intel Mac this needs **no OpenCore** (OpenCore only does
this job on non-Apple hardware). Combined with our finding that the conventional
`boot.efi` path boots a real Intel Mac, a BaseSystem restored to an HFS+ stick
should boot the same way.

This would be a **pkg-free, one-stick, Linux-native** Intel path — download from
Apple, build one stick, boot — sidestepping both the MobileAsset wall and the
two-stick clone/dongle problem. It yields a **recovery/reinstall** stick (boots →
downloads & installs macOS), not a full offline installer.

Open questions to resolve: whether `BaseSystem.dmg` is HFS+ or APFS (extract via
`dmg2img` / `apfs-fuse` / `darling-dmg`), the exact on-stick layout
(`com.apple.recovery.boot/` vs. a BaseSystem restored to the volume root), and a
real-hardware boot test. Note: the Sonoma+ *recovery environment* refuses to mount
FAT32/ExFAT, so prefer Monterey-era for an initial test.

(For *old*, pre–Big Sur macOS, the full installer used a simpler
InstallESD/BaseSystem layout that is also likely Linux-expandable.)

## Status

| Piece | State |
|-------|-------|
| GPT + HFS+ layout | ✅ same as Apple Silicon |
| Bless not required (recognition by content) | ✅ verified (pre-T2 Intel Mac) |
| Bless-from-Linux (optional T2 insurance) | ✅ implemented + byte-validated |
| Full-tree clone for Intel (`build_intel_clone`) | ✅ implemented, structure-tested |
| **End-to-end clone → boot on real Intel hardware** | ⏳ **not yet verified** |
| **Recovery path** (`macrecovery.py` → BaseSystem → stick) | 🔬 **promising** — pkg-free, one-stick; Apple firmware boots BaseSystem directly; to build & test |
| full-installer pkg → Intel expansion (modern macOS) | 🔬 open future research |
