# Design

This document describes the concepts behind stampla. It grows alongside
the implementation; sections are added as the corresponding modules land.

## Goals

An archive of photos and videos should be able to answer two questions
without external records: *is every file where and what its name claims,*
and *has any file changed since it was named?* stampla answers both by
making the filename itself the record — a deterministic function of the
file's capture time and content.

Guiding principles, in priority order:

1. **Never destroy information.** Every command is a dry run unless
   explicitly applied; renames are journaled and reversible; a file whose
   capture time cannot be resolved is reported and left alone, never given
   a partial name.
2. **Derive, don't track.** Names are recomputable from file content and
   metadata at any time. There is no database that can drift out of sync
   with the files.
3. **Families move as one.** A RAW master and everything that annotates or
   derives from it share one name prefix and are renamed atomically.

## Naming pattern

A *pattern* defines the identity part of a filename — the **prefix**:

```
20260703_150727_9b677b64
└──────┬──────┘│└──┬───┘
  capture time │  digest slice
           separator
```

- **Capture time** is formatted from a strftime-style format string
  (default `%Y%m%d_%H%M%S`) as naive local wall-clock time — the time a
  human at the scene would have read off a watch. Sorting names sorts by
  capture time.
- **Digest slice** is the first *n* lowercase hex characters of the whole
  file's content digest (default: 8 characters of MD5). The slice is long
  enough to make collisions within one archive implausible, short enough to
  keep names readable. The digest is an identity and integrity check, not a
  cryptographic guarantee.
- A prefix is capped at 31 characters so it always fits IPTC fields with a
  32-character limit, which DAM integrations use as a rename token.

Multiple patterns can be recognized simultaneously. The grammar tries the
current pattern first, then legacy ones, so an archive migrating from, say,
`md5:8` to `sha256:12` names remains fully classifiable mid-transition:
every file is either canonical under the current pattern, valid under a
legacy pattern (pending migration), or not canonically named at all.

## Filename grammar

A canonical filename decomposes as:

```
<prefix>[<suffix>][.<raw_ext>].<ext>
```

| part | rule | examples |
|---|---|---|
| `prefix` | pattern-defined identity | `20260214_125556_1355acb2` |
| `suffix` | optional label starting with `-` or `_`, no dots | `-Edit`, `-Enhanced-NR`, `_pr` |
| `raw_ext` | optional master extension, only on append-style sidecars | `.nef` in `….nef.xmp` |
| `ext` | the file's own extension, lowercase | `nef`, `xmp`, `pp3` |

This grammar covers the naming behavior of common photo tools:

- DAM sidecars that *replace* the extension: `prefix.xmp`
- Sidecars that *append* to the full name (Darktable, RawTherapee,
  NX Studio): `prefix.nef.xmp`, `prefix.rw2.pp3`, `prefix.nef.nksc`
- Editor derivatives labeled with a suffix (Photoshop, DxO, AI denoisers):
  `prefix-Edit.tif`, `prefix-Enhanced-NR.dng`
- Sidecars of derivatives, combining both: `prefix_pr.dng.pp3`

**The core rename rule: only the prefix ever changes.** Suffix, raw
extension and extension are always preserved. All files sharing a prefix
form a *family*; renaming the master means swapping the prefix on every
family member, atomically.

A filename that starts like a known prefix but violates the rest of the
grammar (stray characters before the extension, uppercase extension,
chained extra extensions) is reported as *malformed* — distinct from files
that are simply not named canonically at all.

## Reading metadata

All metadata comes from [ExifTool](https://exiftool.org/), run as one
persistent process (`-stay_open`) so that querying thousands of files does
not pay its startup cost per file. No Python EXIF library is used: the
formats that matter most (current RAW variants, video containers) are
exactly where such libraries silently return nothing, and a silently
missing date is the failure mode stampla exists to prevent.

Queries always use `-a -G1`, because the same tag name routinely appears
several times in one file with different meanings. A video may carry a
maker-notes `CreateDate` in local wall-clock time *and* a QuickTime
`CreateDate` in UTC; without group qualification, which one is returned is
an accident of tag priority. Downstream logic therefore always sees
group-qualified tags (`Nikon:CreateDate`, `QuickTime:CreateDate`) and can
rank them deliberately.
