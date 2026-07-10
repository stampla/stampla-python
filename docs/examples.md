# Example setups

Six complete configurations, from minimal to advanced. Each shows a
config, the workflow it enables, and why its settings are what they
are. All of them share the same guarantees: every command is a dry run
unless `--apply` is given, nothing is ever overwritten, and every apply
is journaled, resumable with `stampla resume` and revertable with
`stampla undo`.

A config describes one *archive*: a root directory containing one or
more *trees* (photo or video), a *naming pattern*, and rules for dates,
sidecars and imports. Start from the closest example and adjust.

## 1. Minimal: one folder of photos

The smallest useful setup — a single photo tree, all defaults.

```toml
root = "/home/anna/Photos"

[[trees]]
path = "Archive"
media = "photo"
```

```console
$ stampla import /media/card --config photos.toml --apply
$ stampla verify --config photos.toml
```

Files land in `Archive/2026/2026-07/20260701_100000_9b677b64.nef`,
named by capture time and content hash. `verify` re-derives every name
and reports anything that disagrees. That is the whole loop.

## 2. RAW photographer with Lightroom Classic

Shooting RAW+JPEG (the JPEG only for the in-camera preview), managing
photos in Lightroom Classic, occasionally using vendor tools that keep
sidecars in subdirectories (Nikon NX Studio).

```toml
root = "/mnt/archive"

[[trees]]
path = "Photos"
media = "photo"

# NX Studio keeps sidecars in NKSC_PARAM/ next to the masters;
# they belong to the master's group and are renamed with it.
[[sidecar_dirs]]
subdir = "NKSC_PARAM"
strip = ".nksc"

[import]
# camera housekeeping files, visible in reports but never imported
ignore = ["NIKON001.DSC", "NC_FLLST.DAT"]
# drop the JPEG twin when its RAW is present; a JPEG without a RAW
# twin still imports, so a JPEG-only photo can never be lost
skip_jpeg_twins = true

[dam]
# Lightroom Classic's "Job Identifier" filename token
token_tag = "XMP-photoshop:TransmissionReference"
trees = ["Photos"]
```

The workflow:

```console
$ stampla import /Volumes/CARD --config archive.toml --apply
  ... card fully accounted for — safe to format
$ stampla verify --config archive.toml
```

When names go stale (a capture-time fix, content edited in place),
Lightroom must do the renaming itself or its catalog loses the files.
`inject` writes each master's freshly computed name into the Job
Identifier field; then, in Lightroom: *Metadata → Read Metadata from
Files* on the affected folders, and *Library → Rename Photos* with the
single `{Job Identifier}` token. Everything Lightroom does not know
about (double-extension sidecars, NX Studio files, editor derivatives)
is renamed by the tool:

```console
$ stampla inject --config archive.toml --apply
$ stampla rename --config archive.toml --apply
```

Order does not matter: both sides derive the same target name.

## 3. JPEG or phone shooter: names that never drift

JPEG (and HEIF, DNG, TIFF) files carry their metadata *inside* the
file, so every keyword, rating or edit rewrites them — and a name based
on the whole file's hash goes stale on the first star you give a photo.
The pattern's `image_hash` list solves this: those extensions are
hashed over their image data only, so metadata edits never drift the
name, and a mismatch means the pixels themselves changed.

Phone videos store capture time as UTC; the `@utc` marker converts it
into your timezone (and the conversion is always visible in reports).

```toml
root = "/home/anna/Media"

[[trees]]
path = "Photos"
media = "photo"

[[trees]]
path = "Videos"
media = "video"

[pattern]
name = "md5-image"
image_hash = ["jpg", "jpeg", "dng", "tif", "tiff", "heic"]

[dates]
timezone = "Europe/Warsaw"
video = ["DateTimeOriginal", "CreateDate", "QuickTime:CreateDate@utc"]
```

Do **not** mark a source `@utc` unless you know it stores UTC: BRAW
files, for instance, keep local time in their QuickTime atoms.

## 4. Photos and videos from many cameras

Two trees, several bodies, mixed containers. The default video date
chain already handles the hard part: cameras write trustworthy local
time into maker notes and a UTC copy into QuickTime atoms, so the
unqualified entries prefer maker notes and `QuickTime:CreateDate` is
the explicit last resort (needed by formats like BRAW that offer
nothing else — and store local time there, hence no `@utc`).

```toml
root = "/mnt/archive"

[[trees]]
path = "Photos"
media = "photo"
layout = "{yyyy}/{yyyy}-{mm}"

[[trees]]
path = "Video"
media = "video"
layout = "{yyyy}/{yyyy}-{mm}"

[extensions]
video = ["mov", "mp4", "braw", "nev", "r3d", "avi", "mkv"]
```

Import routes each group by its master's extension: RAW and JPEG
masters to the photo tree, containers from `extensions.video` to the
video tree — one card, one command.

## 5. Taming an old, messy dump

Years of unsorted folders. `organize` runs the full import planning
over the mess and reports — it never renames anything and has no
`--apply` at all:

```console
$ stampla organize /mnt/old-dump --config archive.toml
  /mnt/old-dump/2010/DSCF2401.AVI  ->  /mnt/archive/Video/2010/2010-06/20100628_163141_069e4fb1.avi
  ...
collision (53):    # groups whose content duplicates another group
mtime-dated (113): # dateable only from file modification time — verify by hand
already-imported (30):
```

Old dumps are full of files whose EXIF is gone but whose *name* still
carries the capture time (`20190504_101112.jpg`, `PXL_20220612_…`,
exports and thumbnails derived from well-named originals). `organize`
recovers these automatically: a strict, year-first-only parser
(`YYYY?MM?DD` then `HH?mm?ss`, consistent separators, validated ranges)
ranks between real metadata and mtime, and such proposals are reported
as `name-dated`. Day-first or US month-first names (`31.12.2016`,
`12312016`) are never interpreted — month and day are indistinguishable
across locales, and a wrong-but-plausible date is worse than none. To
use the same source in other commands, add `File:NameTimestamp` to a
date chain:

```toml
[dates]
photo = ["EXIF:DateTimeOriginal", "EXIF:CreateDate",
         "XMP:DateCreated", "File:NameTimestamp"]
```

Work through the dump in slices: move a reviewed batch into a staging
folder, import it, repeat. Files whose capture time comes from mtime
are proposed but flagged — mtime is hearsay, confirm before importing.
If your import policy ignores JPEGs, remember it applies here too;
triage an old JPEG-era dump with a config that includes them.

## 6. Changing the naming scheme (a migration)

The digest, its length, and the per-extension image-hash mapping are
part of the pattern's identity — changing any of them means every
affected file needs a new name. Multiple patterns exist exactly for
this window: list the new pattern as primary and the old one under
`additional`, and every file stays classifiable mid-migration. Files
still named under the old pattern are reported as `other-pattern`
(intact, pending migration) — never mistaken for corruption, never
silently accepted as fine.

```toml
[pattern]
name = "sha256-12"
digest = "sha256"
digest_length = 12
image_hash = ["jpg", "jpeg", "dng", "tif", "tiff"]

[[pattern.additional]]
name = "md5-8"    # the scheme the archive was built with
```

Migrate with the ordinary commands — `rename --apply` for everything
the tool owns, `inject --apply` plus the DAM round for DAM-managed
masters — then delete the `additional` entry. A mixed archive is a
loudly temporary state, not a destination.
