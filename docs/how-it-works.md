# How it works

## The question

Everyone "knows" you need a Mac to make a macOS installer USB, because Apple's
`createinstallmedia` only runs on macOS. But *booting* and *building* are
different problems. What does an Apple Silicon Mac actually require of the USB to
boot it — and can Linux produce that?

## The finding: there is no bless

On Intel Macs, bootable media is "blessed": the HFS+ volume header's `finderInfo`
array points at the boot files, and the EFI system partition holds `boot.efi`.
The assumption was that Apple Silicon needs the same, and that Linux can't write
it.

Reading a real `createinstallmedia` stick says otherwise:

- **`finderInfo` is all zero.** No blessed folder, no blessed boot file. The
  Intel-era bless everyone assumes is required simply isn't used.
- **The EFI partition is empty.** Apple Silicon doesn't EFI-boot the installer.
- **The HFS+ partition is a plain `Apple HFS/HFS+` type** (`48465300-…`) with no
  special GPT attributes.

None of the usual "bootable" markers are set anywhere. The conclusion: **Apple
Silicon firmware recognizes the installer by its content/structure on the HFS+
volume — the `Install macOS ….app` bundle and its (Apple-signed) boot objects —
not by volume metadata.** The Mac's own iBoot handles external install media by
finding the installer payload and loading Apple's pre-signed boot objects from
inside it.

That reframes the whole problem. You are not forging anything (the boot objects
are Apple-signed and come straight from Apple's installer); you are only
*arranging* them on a volume. And the arrangement turns out to be: copy the
content faithfully.

## The recipe

1. **GPT** — a 200 MB EFI system partition (`EF00`) and an Apple HFS+ partition
   (`AF00`) filling the rest. Generic, known parameters; nothing read from a
   reference disk.
2. **Format** — empty FAT on the ESP, **non-journaled** HFS+ on the big
   partition.
3. **Copy** — the entire `Install macOS ….app`, plus the root markers
   `.IAPhysicalMedia` and `restore/` (and cosmetic `.VolumeIcon.icns`).
4. **`fsck.hfsplus`** and unmount.

No bless step. No CNID patching. On a from-scratch build this produced a stick
that an M1 recognized in the startup picker and booted into the macOS installer.

## Getting the files onto the USB: copy vs stream

The installer lives in Apple's `InstallAssistant.pkg` — a xar holding a small
**pbzx**-compressed app skeleton and a huge plain `SharedSupport.dmg` (~17 GB).
SlickStick can lay step 3 down two ways:

- **copy** — decode the skeleton and extract the dmg into a work dir (~18 GB),
  then copy the assembled app onto the USB. Keeps the unpacked app around, so
  building a second stick is an instant re-copy.
- **stream** — pipe the pkg straight onto the mounted USB: `bsdtar` reads the
  pkg (as the normal user, so NFS `root_squash` is a non-issue), the skeleton is
  pbzx-decoded in a pipe, and the dmg is `tee`'d directly to the target. Only
  the writes to the root-mounted volume go through `sudo`. **Near-zero scratch
  space** — nothing lands on local disk but a mount point. Ideal when the disk
  is tight or the pkg sits on a network share.

Both produce an identical on-disk result; stream just skips the work dir. The
pbzx decoder is pure stdlib Python (`lib/pbzx.py`) — no external `pbzx` tool.

The pkg itself comes from one of four sources: **gibMacOS** (its catalog parser,
run in an isolated subprocess, lists every full installer Apple ships so you pick
a version in SlickStick's own menu), a direct Apple **URL**, a **local** file, or
a **stick** clone. Only the stick clone carries real `createinstallmedia`
markers; the others synthesize them (see below).

## Why non-journaled

Linux writes **non-journaled** HFS+ cleanly. It will only write a **journaled**
volume with `mount -o force,rw`, which the kernel warns can corrupt the volume
because it doesn't maintain the journal. Non-journaled isolates the one variable
we care about (does a faithful content copy boot?) with a trustworthy write.
Journaling is a crash-consistency feature; it doesn't change the on-disk layout
the firmware reads, so the real media being journaled is almost certainly
incidental. `--journaled` is available if you want to match the original exactly.

## The markers

A `createinstallmedia` stick carries root files the pkg does not: `.IAPhysicalMedia`
(a small plist naming the app, version, and build), an empty `restore/`, and a
volume icon. SlickStick handles these in three tiers:

- **stick source** (`--stick`) — clones a real installer stick, copying its
  markers verbatim. This is the only boot-verified path, so the wizard offers it
  as a first-class source that auto-detects inserted installer sticks.
- **synthesized** (any other source) — `.IAPhysicalMedia` is generated with the
  three keys the picker requires: `AppName` (the bundle name), `ProductVersion`
  (from the app's `Info.plist`), and `ProductBuildVersion` (decoded from the
  pkg's `PackageInfo` version, e.g. `26.1.131` → `26B131`), plus a volume icon.
  **Boot-verified on hardware.** The exact requirement — and everything that
  turned out *not* to matter — is in
  [recognition-rules.md](recognition-rules.md).
- **`--reference-stick`** — advanced escape hatch: copy real markers onto an app
  that came from a non-stick source. Rarely needed now that the stick source
  exists.

## The reverse-engineering method (the "oracle")

You don't need a known-good stick as a *component* — nothing on it is a secret
unextractable blob. But it's invaluable as an **oracle**: build a candidate,
mount both read-only, and diff them — `finderInfo`, GPT types, file tree,
symlinks, attributes. Every difference is a lead. That's how the zero-bless and
empty-ESP facts surfaced, and it's how the remaining unknowns (file attributes
the Linux `hfsplus` driver may drop) would be run down if a build fails to boot.

## Intel is different (and unverified)

Intel Macs *do* boot via EFI: the media needs `.IABootFiles` (with `boot.efi`,
`prelinkedkernel`) **and** a real HFS+ bless pointing `finderInfo` at `boot.efi`
— exactly the machinery Apple Silicon doesn't use. SlickStick's Intel path lays
the content and attempts the bless via `lib/bless.py` (finderInfo is just eight
big-endian `uint32`s at offset 1104 in the partition; CNIDs equal the inode
numbers Linux reports). But generating `.IABootFiles` correctly is unsolved here,
and **we have no Intel Mac to test on** — so the Intel path is a research
artifact, loudly marked experimental. Contributions and test reports welcome.

## Honest limits

- Both the **stick-clone** and **pkg (synthesized-marker)** paths are proven to
  **list + boot** on Apple Silicon; a full install to completion wasn't run.
- **Intel** is unverified entirely.
