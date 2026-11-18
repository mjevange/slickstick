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

## Building Intel media from a downloaded pkg (from scratch, no clone)

Modern macOS (Big Sur and later) ships the OS inside `InstallAssistant.pkg`. That
pkg is a **"pkgdmg"**: a xar package (members `Payload` — the app skeleton — and
`SharedSupport.dmg`) with a UDIF/koly trailer appended. Inside its
`SharedSupport.dmg` the OS lives as a sealed **MobileAsset** alongside Apple's
**UpdateBrain**, and the long-standing assumption was that reproducing
`createinstallmedia`'s output on Linux would require running UpdateBrain / unsealing
system volumes — i.e. a Mac.

**Finding — UpdateBrain is *not* required for the install (proven on a pre-T2 Intel
Mac, 2026-11-17).** We hand-built a stick entirely on Linux:

- root = a **recovery `BaseSystem`** (our proven `macrecovery` path) — boots the Mac
- a reconstructed **Install app** (skeleton from the pkg's `Payload`)
- the app's `Contents/SharedSupport/SharedSupport.dmg` = the **whole pkg** (see the
  critical subtlety below), carrying the offline OS payload
- synthesized `.IAPhysicalMedia` + bless

Booted on the iMac, `startosinstall` from the on-stick app **accepted the media,
skipped bridgeOS (pre-T2), prepared, and ran the install onto the target — no
UpdateBrain, no clock/cert workaround.** It handed off to the second "macOS
Installer" phase normally; only writing to a slow/failing throwaway target USB kept
it from fully finishing (a limit of that USB, not the method). A clean end-to-end
completion on good media is the one remaining confirmation.

**The critical subtlety — `SharedSupport.dmg` must be the *whole pkg*, not the inner
member.** On a real `createinstallmedia` stick the app's
`Contents/SharedSupport/SharedSupport.dmg` is byte-identical to the entire
`InstallAssistant.pkg` (it *is* the pkgdmg). The installer opens it expecting a
*pkg* and reads the real dmg's offset *inside* it ("Getting offset for dmg in pkg");
hand it the bare inner `SharedSupport.dmg` member and it rejects the media with
**"pkgdmg is missing a footer."** SlickStick now places the whole pkg on both the
copy and stream paths (`payload.extract_pkg`, `usb.stream_app_to_target`). A second
gotcha: the reconstructed app bundle must contain no stray files (e.g. a nested copy
of the app), or codesign fails with "unsealed contents" and the installer calls the
media "damaged."

So a **pkg-based, no-Mac, no-clone Intel full installer** is viable; it's validated
through "installer accepted + install running," pending only a clean finish on a
fast target. The **recovery path below** remains the simplest one-stick option when
a reinstall (vs. an offline full install) is enough.

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

**Feasibility confirmed (2026-11-16):** a Monterey recovery downloaded with
`macrecovery.py -b Mac-E43C1C25D4880AD6 -os default` gave `BaseSystem.dmg`
(624 MB) whose inner volume is **HFS+** ("macOS Base System") — **Linux-readable
with no `apfs-fuse`** (`7z` lists/extracts it; the raw HFS partition can be pulled
with `7z e` and loop-mounted, no `dmg2img`/`qemu-img` needed). It contains
`System/Library/CoreServices/boot.efi` (723,512 bytes) and **no Cryptexes** (clean,
pre-sealed-system). Version macOS 12.5.1 / 21G83.

Plan: `7z` extract the raw HFS → loop-mount ro → partition+format the target
(GPT: EFI + HFS+) → `bsdtar`-pipe copy the BaseSystem onto it (like
`build_intel_clone`) → optional bless → `fsck`. One stick, no clone source.

**PROVEN on real hardware (2026-11-16).** `slickstick build --device /dev/sdX
--recovery BaseSystem.dmg` built a Monterey recovery stick on Linux — decode
(lib.udif) → loop-mount → partition/format → clone → bless → fsck — and it
**booted a real Intel iMac into macOS Recovery** (orange "macOS Base System" icon
in the Startup Manager). No OpenCore, no Mac, no pkg; bless included but not
required. **Monterey** is the proven default today; for Ventura+ (which adds
`System/Cryptexes/`), see the Cryptex recon below.

**Usage.** Wizard: pick **Intel → Download macOS Recovery from Apple**, give the
target Mac's board-id (About This Mac → System Report, or Recovery Terminal
`ioreg -l | grep board-id`), choose the shipped (safe) or newest (experimental)
version, then the target USB. CLI with a pre-downloaded image:
`slickstick build --device /dev/sdX --recovery path/to/BaseSystem.dmg`.

(For *old*, pre–Big Sur macOS, the full installer used a simpler
InstallESD/BaseSystem layout that is also likely Linux-expandable.)

## Cryptex recon — does the recovery path extend to Ventura+?

Ventura (macOS 13) introduced **Cryptexes** (`System/Cryptexes/OS` and `App`),
raising the worry that a plain file-copy couldn't reproduce a bootable Ventura+
recovery. We settled the *structural* half of that on Linux, with no Mac:

Decoded a **Ventura** `BaseSystem.dmg` (board `Mac-B4831CEBD52A0C4C`, `-os default`)
with `lib.udif`, loop-mounted the "macOS Base System" HFS+ volume, and dissected it:

- `System/Cryptexes/OS` (180 MB) and `App` (14 MB) are **plain, expanded
  directories** of ordinary code-signed frameworks (WebKit / JavaScriptCore /
  Safari) — **not** sealed DMGs, **not** APFS snapshots, **not** empty mount points.
- **No** `.im4m`, trustcache, `BuildManifest`, or any in-volume seal a copy would
  break (only `usr/bin/trustcachectl`, the tool itself).
- `System/Library/CoreServices/boot.efi` is present — Intel-bootable by the same
  conventional path proven on Monterey.

So the Ventura recovery is a flat HFS+ volume whose Cryptexes are just files our
`bsdtar` copy already carries. **Strong structural evidence the recovery path
extends to Ventura+ unchanged** — pending the one thing Linux can't answer: a real
boot (a 2018 Mac mini is ordered to confirm). Worst case, the recovery still boots
to reinstall and only the Safari cryptex fails to verify — the core function works.

## Status

| Piece | State |
|-------|-------|
| GPT + HFS+ layout | ✅ same as Apple Silicon |
| Bless not required (recognition by content) | ✅ verified (pre-T2 Intel Mac) |
| Bless-from-Linux (optional T2 insurance) | ✅ implemented + byte-validated |
| Full-tree clone for Intel (`build_intel_clone`) | ✅ implemented |
| **End-to-end clone → boot on real Intel hardware** | ✅ **PROVEN** — a Monterey clone built on Linux booted an Intel iMac into the full installer (branded icon) |
| **Recovery path** (`macrecovery.py` → BaseSystem → stick) | ✅ **PROVEN on hardware** — `build --recovery` built a Monterey stick on Linux that booted a real Intel iMac into Recovery (pkg-free, one stick, pure-Python UDIF decode) |
| **Ventura+ recovery (Cryptexes)** | 🧪 structurally viable — cryptexes are plain files, no in-volume seal; boot test pending (2018 mini) |
| **full-installer pkg → Intel (from scratch, no clone)** | 🧪 **validated** — a hand-built stick's `startosinstall` accepted the media & ran the install (no UpdateBrain, no cert/date trick) on a pre-T2 iMac; `SharedSupport.dmg` must be the whole **pkgdmg**; clean end-to-end finish pending a fast target disk |
