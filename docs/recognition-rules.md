# What Apple Silicon requires to boot a USB installer

An empirically-derived, hardware-tested account of **exactly** what makes an
Apple Silicon Mac list and boot a macOS installer USB. This is otherwise
undocumented publicly; we reverse-engineered it by bisection on a real M1 while
building SlickStick, and it's what the tool's synthesizer is built around.

> TL;DR — a volume **lists** in the startup picker iff it's a GPT partition typed
> **exactly** `Apple HFS+`, formatted HFS+, with a root `.IAPhysicalMedia`
> containing **all three** of `AppName`, `ProductBuildVersion`, `ProductVersion`,
> where `AppName` names an `*.app` bundle that exists at the root. It **boots**
> iff that bundle actually contains the installer. Nothing else matters.

## Method

Two things made a clean bisection possible:

- **Live hot-plug testing.** On the "Loading startup options" screen, plugging a
  USB in makes it appear within **~1 second** — the picker rescans live. So each
  variant could be tested in seconds, no reboots.
- **Marker-swap bisection.** Starting from a known-good `createinstallmedia`
  stick and a stick built from Apple's `InstallAssistant.pkg`, we changed **one
  variable at a time** — markers, GPT type, filesystem structure, bundle
  contents — mounted read-write from Linux, and hot-plug-tested.

Two independent signals were recorded: does the volume **list** in the picker,
and (if listed) does it **boot** when selected.

## The rule

A USB volume **lists** in the Apple Silicon startup picker if and only if **all**
of the following hold:

1. **GPT partition typed exactly `Apple HFS+`** — GUID
   `48465300-0000-11AA-AA11-00306543ECAC` (`AF00`). Foreign types (e.g. Microsoft
   basic data) are skipped, **and so are mismatched Apple types** — tagging an
   HFS+ volume with the Apple *APFS* GUID makes it disappear. The type must match
   the filesystem.
2. **HFS+ filesystem** on that partition.
3. A **root `.IAPhysicalMedia`** plist containing **all three** keys:
   `AppName`, `ProductBuildVersion`, `ProductVersion`. Missing any one → not
   listed.
4. **`AppName` names an `*.app` bundle that exists** at the volume root. The name
   can be *anything* (no `Install macOS …` requirement) — but it must resolve to
   a real directory of that exact name.

It additionally **boots** (rather than just listing) iff:

5. **That bundle actually contains the installer.** An empty bundle *lists* but
   stalls at the Apple-logo progress bar.

### Listing is shallow; booting is deep

The picker's *listing* check is shallow — it validates the markers and that the
named bundle *exists*, but not its contents. The deep validation (real boot
objects) happens only when you select the entry and boot. This is the precise
explanation for "it shows up but won't boot."

### Values aren't validated — only `AppName` resolution

`ProductBuildVersion` and `ProductVersion` can hold **garbage** (`99Z999`,
`99.99.99`) and the stick still lists *and* boots. The only value that matters is
`AppName`, because the picker uses it to **locate** the bundle.

## The full test matrix

| Variant (vs. a working stick) | Lists? | Boots? | Takeaway |
|---|:---:|:---:|---|
| `createinstallmedia` stick | ✅ | ✅ | baseline |
| pkg app + `{AppName, ProductVersion}` (no build, no icon) | ❌ | — | original synth failed |
| pkg app + real 3-key markers + icon | ✅ | ✅ | markers are the discriminator |
| `{AppName, Build, Version}` + app icon | ✅ | ✅ | pure-pkg markers work |
| `{AppName, Version}` + icon (no build) | ❌ | — | **build needed**, not the icon |
| `{AppName, Build, Version}`, **no icon** | ✅ | ✅ | icon is cosmetic |
| `{AppName, Build, Version=99.99.99}` | ✅ | ✅ | version *value* ignored |
| `{AppName, Build=99Z999, Version}` | ✅ | ✅ | build *value* ignored |
| no `.IAPhysicalMedia` | ❌ | — | the plist is the gate |
| `{Build}` only | ❌ | — | all three keys required |
| `{Build, Version}` (no AppName) | ❌ | — | `AppName` required |
| `{AppName, Build}` (no Version) | ❌ | — | `ProductVersion` required |
| `{AppName=TotallyFake.app, Build, Version}` | ❌ | — | `AppName` must resolve |
| `{AppName, Build, Version=garbage}` real AppName | ✅ | ✅ | `AppName` value matters, Version doesn't |
| 3-key markers, `restore/` removed | ✅ | ✅ | `restore/` not needed |
| 3-key markers, **empty** app bundle | ✅ | ❌ | picker shallow, boot deep |
| custom bundle name + matching `AppName` | ✅ | ✅ | name is arbitrary |
| **no ESP** (HFS+-only stick) | ✅ | ✅ | EFI partition is vestigial |
| GPT type → `Microsoft basic data` | ❌ | — | type must be Apple |
| GPT type → `Apple APFS` (HFS+ FS underneath) | ❌ | — | type must *match* the FS |

## What does **not** matter

Each of these was shown irrelevant to listing *and* booting:

- **`.VolumeIcon.icns`** — cosmetic. Without it the volume shows as a plain drive
  and still boots. (We include it anyway so the picker shows the macOS logo.)
- **`restore/`** — a leftover empty directory.
- **HFS+ journaling** — non-journaled volumes boot fine (createinstallmedia makes
  journaled; it isn't required).
- **3-part vs 2-part `ProductVersion`** — `27.2` works as well as `27.2.1`.
- **The exact `SharedSupport.dmg` bytes** — Apple's full-installer pkg ships a dmg
  ~14.7 MB different from `createinstallmedia`'s; both boot.
- **HFS+ "bless"** — the volume header's `finderInfo` is all-zero on real media;
  Apple Silicon doesn't use the Intel-era bless.
- **The EFI System Partition** — vestigial; a single-HFS+-partition stick boots
  (with a split-second extra delay before listing).
- **The `Install macOS …` naming** — the bundle can be named anything.
- **Finder/Spotlight metadata** (`.DS_Store`, `.Spotlight-V100`, etc.).

## Implications for building from Linux

To mint a bootable Apple Silicon installer you need exactly:

1. a GPT with the installer on an **`Apple HFS+`-typed** partition,
2. an **HFS+** filesystem,
3. a root **`.IAPhysicalMedia`** with `AppName` (matching the bundle),
   `ProductBuildVersion` (decodable from the pkg's `PackageInfo` version, e.g.
   `26.1.131` → `26B131`), and `ProductVersion` (from the app's `Info.plist`), and
4. the real **`Install macOS *.app`** bundle.

No Mac, no bless, no signing step, no SharedSupport dive. That's what SlickStick's
`write_markers_from_app` produces — and it's been confirmed to list (with icon)
and boot on real M1 hardware.
