# Intel Macs — status & findings

Both architectures are **proven on real hardware**. On Intel, all three paths
(recovery, full-installer clone, and from-scratch full installer) have booted real
Macs, with the from-scratch offline install carried end to end on a **pre-T2** iMac
through a rebooted, running macOS. **Cryptex-era boot (Ventura+) is now confirmed on
T2**: a Sonoma (14) from-scratch stick booted a 2018 Mac mini into Recovery. The one
remaining boundary is the **T2 offline *install*** when a firmware (bridgeOS) **bump** is
required — that step needs Apple's signing servers and is online-only for *everyone*
(Apple's own `createinstallmedia` included). Offline T2 install still works when the Mac's
current bridgeOS already meets the target's minimum (see "T2 and the bridgeOS firmware wall"
below). This page documents
what we've learned, because Intel boots differently — and some of it corrects
widespread assumptions.

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

Intel needs a **bootable HFS+ volume** — `boot.efi` at the conventional path, plus
a `SharedSupport.dmg` payload if you want an offline install. SlickStick has three
Intel build modes (plus a no-USB **download-only** path — wizard "Download macOS
files only" or `slickstick download` — that just fetches the raw pkg/BaseSystem into
a folder you choose, to archive, inspect, or feed the "provide the files" build later):

1. **Clone** an existing `createinstallmedia` stick (`stick` source /
   `build_intel_clone`): copies the whole tree (skipping HFS+ internal/cache dirs)
   and optionally blesses it. Needs a stick someone already made on a Mac.
   Wizard: **Intel → Clone from an inserted stick**.
2. **Recovery** (`--recovery BaseSystem.dmg` / `build_recovery`): a one-stick,
   pkg-free *reinstall* stick from an Apple recovery image. Boots → downloads &
   installs over the internet. Wizard: **Intel → Download macOS Recovery**.
3. **Full installer from scratch** (`--recovery BaseSystem.dmg --local <pkg>` /
   `build_intel_full`): combines a recovery `BaseSystem.dmg` (bootable root) with an
   `InstallAssistant.pkg` (app + whole-pkgdmg `SharedSupport.dmg`) into a one-stick
   **offline** full installer — no Mac, no clone. Wizard: **Intel → Full offline
   installer**, then either **download both for me** (pick one macOS version — the
   BaseSystem comes from `macrecovery` by a behind-the-scenes board-id, the matching
   pkg from gibMacOS; no board typing) or **provide the two files** as paths.
   Downloaded files are cached under `~/.cache/slickstick/downloads` (keyed by
   version/board) and reused on later runs, so a build hiccup never forces
   re-pulling the ~12 GB pkg; set `SLICKSTICK_NO_CACHE=1` to opt out (downloads go
   to scratch and aren't kept). This path is **hardware-validated end to end**
   (2026-11-18): a web-downloaded build booted a pre-T2 iMac, installed offline to
   a 3 TB disk, and the installed macOS booted to Setup Assistant — no Mac anywhere.

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
Installer" phase normally. The first attempt was limited only by a slow/failing
throwaway target USB; on a healthy 3 TB disk (2026-11-18) the install **ran to
completion and the installed macOS booted to Setup Assistant** — the full chain is
confirmed.

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

So a **pkg-based, no-Mac, no-clone Intel full installer** is viable — and now
**proven end to end** (2026-11-18): a web-downloaded build booted a pre-T2 iMac,
`startosinstall` installed offline onto a 3 TB disk, and the installed macOS rebooted
to Setup Assistant. The **recovery path below** remains the simplest one-stick option
when a reinstall (vs. an offline full install) is enough.

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

## T2 hardware results (2018 Mac mini, Macmini8,1) — Cryptex boot ✅, offline install ⛔

Settled on a real **T2** Mac what Linux alone couldn't:

**Cryptex boot works on T2 (proven 2026-11-20).** A **Sonoma (14)** from-scratch full
installer, built entirely on Linux, booted the T2 2018 Mac mini into macOS Recovery with
the branded "macOS Base System" logo. So the `System/Cryptexes/OS|App` directories survive
our plain `bsdtar` file-copy and boot on real T2 hardware — the structural recon was right,
and the recovery/full-installer path extends into the Cryptex era (Ventura/Sonoma/Sequoia).

Two T2-specific operational notes:
- **Secure Boot must allow external media first.** A T2 Mac ships "Full Security" with
  external boot disabled. Boot built-in recoveryOS (⌘-R), open **Startup Security Utility**,
  set **Reduced/No Security** *and* **Allow booting from external media**, or the stick won't
  appear in the ⌥ boot picker (a *policy* block, not a media failure).
- The install log during `startosinstall` is **RAM-only** (the Recovery environment is a RAM
  disk); it does **not** persist to the target, so it must be read live
  (`tail -n 200 /var/log/install.log`) before unplugging/rebooting. Recovery's Terminal has a
  minimal toolset (`grep -iE` fails with "illegal operator"; use `tail`/`cat`/single-word `grep`).

### T2 and the bridgeOS firmware wall (the offline-install boundary)

The from-scratch **install** on T2 reproducibly fails at ~50% with "An error occurred
installing macOS" (`OSInstallerSetup.error Code=561`). The live install.log root-causes it
precisely — and it is **not** personalization and **not** a media defect:

```
osinstallersetupd: Personalization is not required … Skipping personalization   ← NOT the blocker
OSISDownloadOperation: bridgeOS update is required (minimum bridge version: 23.16.1066.0.0.0)
OSISDownloadOperation: Using catalog for bridgeOS: https://mescon.apple.com/content/catalog/...
The Internet connection appears to be offline (NSURLErrorDomain Code=-1009)
Failed to verify clock with apple.com … offline        Could not verify trust (5)
Error loading software update catalog: MSUProductManagerErrorDomain Code=51   →  Code=561
```

**Root cause:** the T2 install insists on updating the **bridgeOS** (the T2 chip's own
firmware) to a minimum version, and reaches for Apple's software-update catalog
(`mescon.apple.com`) to do it. Offline, that fetch fails and the install aborts. **Pre-T2
Macs have no bridgeOS at all** — which is exactly why the pre-T2 iMac installed *fully
offline* and the T2 mini cannot. The stick itself is provably correct: it boots, prepares,
extracts the payload, and skips personalization; the wall is an Apple T2 requirement.

**Why this is an Apple wall, not a stick defect (researched 2026-11-20).** bridgeOS is
iOS-style **IMG4 secure-boot firmware**. The *payload* can sit on the USB — that's exactly
the staged `UpdateBundle` / `BuildManifest.plist` — but **flashing it requires a live,
Apple-signed manifest personalized to that T2's ECID + an anti-replay nonce** (a TSS
"personalization"), which only Apple's signing servers (`gs.apple.com`/`gdmf`/`mescon`) can
produce, and only for firmware Apple is currently signing. **Payload = shippable;
authorization = not shippable.** Confirmed implications:

- **A real `createinstallmedia` USB *also* fails offline on T2 when a bump is required** —
  real users hit the same ~40–50% "internet required" error with Apple's own media
  (Apple Discussions 253788316; Jamf; Mr. Macintosh). So **no one** gets an offline T2
  firmware bump — this is not a SlickStick defect.
- **A correct clock does *not* help offline.** The `-1009`/"failed to verify clock" lines are
  symptoms of *no network*, not a bad clock; no date value reaches Apple's signing servers.
  (A wrong clock only matters when you *are* online.)
- **`UpdateBrain` vs `UpdateBundle`:** UpdateBundle = the firmware/OS *payload*; UpdateBrain =
  a zipped XPC bundle of installer *code* shipped with a given update (builds the snapshot /
  seal). Both can be local; neither supplies the Apple authorization.

**The offline T2 win that DOES exist — matched firmware.** When the Mac's current bridgeOS
**already meets** the target macOS's minimum, the firmware step is *skipped* and the rest of
the install runs locally. So **offline T2 install works when `current bridgeOS ≥ required`** —
e.g. re-installing the same or an older macOS release than the Mac last ran. Only a *bump*
is online-only. (Our mini demanded a bump for Sonoma 14, so its firmware is below 14's
minimum; an offline install of an older release it already satisfies should complete.)

**Product behavior:** the from-scratch Intel full-installer build now **warns** about the T2
firmware step at the end of a build (what the ~50% "An error occurred" failure means and the
"connect Ethernet/Wi-Fi in Recovery and re-run" fix), prints the **exact ready-to-paste
`startosinstall` command** for the stick, and ships a **`firmware-check`** helper at the stick
root: run it in the Recovery Terminal and it reads the Mac's current bridgeOS via
`/usr/libexec/remotectl dumpstate` and advises whether this install will need the network. It
stays advisory (T2 firmware is Apple-personalized and point-release bumps exist, so the only
guaranteed-safe rule is "if in doubt, connect the network — it's used only if a bump is
needed"). A precise auto-gate (compare current vs required and branch) remains future work.
See [[slickstick-backlog]].

Sources: newosxbook.com/articles/BridgeOS.html; eclecticlight.co (UpdateBrain / "install
personalised" / nonces); groups.google.com/g/munki-dev/c/3ixXsCbHUTM (startosinstall ignores
CatalogURL; `softwareupdate -iaR` handles the iBridge restart); theiphonewiki SecureBoot +
Apple Platform Security (IMG4 personalization tied to T2 ECID).

## Status

| Piece | State |
|-------|-------|
| GPT + HFS+ layout | ✅ same as Apple Silicon |
| Bless not required (recognition by content) | ✅ verified (pre-T2 Intel Mac) |
| Bless-from-Linux (optional T2 insurance) | ✅ implemented + byte-validated |
| Full-tree clone for Intel (`build_intel_clone`) | ✅ implemented |
| **End-to-end clone → boot on real Intel hardware** | ✅ **PROVEN** — a Monterey clone built on Linux booted an Intel iMac into the full installer (branded icon) |
| **Recovery path** (`macrecovery.py` → BaseSystem → stick) | ✅ **PROVEN on hardware** — `build --recovery` built a Monterey stick on Linux that booted a real Intel iMac into Recovery (pkg-free, one stick, pure-Python UDIF decode) |
| **Ventura+ recovery (Cryptexes) — boot** | ✅ **PROVEN on T2** — a Sonoma (14) from-scratch stick (built on Linux) booted a T2 2018 Mac mini into Recovery with the branded "macOS Base System" logo; the Cryptexes survive our plain file-copy and boot on real T2 hardware |
| **full-installer pkg → Intel (from scratch, no clone) — install** | ✅ **PROVEN end to end on pre-T2** — a web-downloaded build booted a pre-T2 iMac, `startosinstall` installed offline onto a 3 TB disk, and the installed macOS booted to Setup Assistant (no UpdateBrain, no cert/date trick); `SharedSupport.dmg` must be the whole **pkgdmg** |
| **T2 offline *install* — firmware *bump* case** | ⛔ **online-only for everyone** (incl. Apple's `createinstallmedia`) — a bridgeOS bump needs Apple's signing servers; fails at ~50% (Code 561). Not a stick defect |
| **T2 offline *install* — matched-firmware case** | 🧪 **expected to work** — when current bridgeOS ≥ target's minimum, the firmware step is skipped; boot + prepare already proven on T2. Pending a matched-version install test on the mini |
