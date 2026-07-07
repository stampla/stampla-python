# stampla

[![CI](https://github.com/stampla/stampla/actions/workflows/ci.yml/badge.svg)](https://github.com/stampla/stampla/actions/workflows/ci.yml)

Deterministic, verifiable naming for photo and video archives.

stampla names every file in an archive after what it is and when it
happened:

```
20260703_150727_9b677b64.nef
└──────┬──────┘ └──┬───┘
  capture time    hash
```

The name encodes the capture time (local wall clock, from camera metadata via
[ExifTool](https://exiftool.org/)) and a slice of the file's content hash.
Because the name is derived purely from the file itself, it is reproducible:
the archive can be re-verified at any time, corruption can be told apart from
legitimate edits, and duplicates identify themselves.

RAW files travel with their families — XMP/PP3 sidecars, editor derivatives,
even sidecars kept in separate subdirectories — which always share the master's
name prefix and are renamed together, atomically.

## Usage

### import

Copy a memory card into the archive, named on arrival:

```console
$ stampla import /Volumes/CARD --config archive.toml
  /Volumes/CARD/DSC_0001.NEF  ->  /archive/Photos/2026/2026-07/20260701_100000_9b677b64.nef
  /Volumes/CARD/DSC_0001.xmp  ->  /archive/Photos/2026/2026-07/20260701_100000_9b677b64.xmp

dry run: 1 group(s) would be imported; pass --apply to copy
```

Masters travel with their sidecars and labeled derivatives as one group.
Import **copies** — files on the card are never modified or removed, so
the card stays a backup until you format it in the camera. Every copied
master is re-hashed at its destination and compared with the digest read
from the card, so a transfer error cannot slip through. Files whose
capture time cannot be resolved, and groups whose target names already
exist (a re-inserted card), are reported and skipped without blocking
the rest. Nothing changes without `--apply`, and every apply is
journaled and revertable with `stampla undo`.

### verify

Recompute every name from metadata and content, and report what disagrees:

```console
$ stampla verify --config archive.toml
scanned 147717 files in 73140 families: 73088 ok, 76 findings

date-mismatch (24):
  Wideo/2025/2025-06/20250615_140910_388ec696.mov  name says 20250615_140910, metadata says 20250615_160910 (MakerNotes:DateTimeOriginal)
  ...
```

Findings are classified by what they *mean*, not just what differs: a
content change in a format that is edited in place (DNG, TIFF, sidecars)
is expected drift, while the same change in a write-once camera format is
a corruption alarm. Other buckets cover date mismatches, unresolvable
capture times, name collisions (duplicate content), malformed and unnamed
files, orphaned sidecars and masters that exist in several formats.

`--skip-hash` checks capture times only — fast enough for a whole archive
between full runs. `--json` emits the report machine-readably; the exit
code is `0` when clean, `1` with findings, `2` on errors.

Hashes are cached in a per-machine manifest
(`.stampla/manifest-<machine>.tsv` under the archive root), so repeat
runs only hash new and touched files. A cached digest is trusted only
while the file's size and mtime are unchanged; `--full` re-hashes
everything regardless — run it periodically, since silent corruption that
preserves size and mtime is exactly what the cache cannot see.
`--no-manifest` disables the cache entirely. If you sync the archive
between machines, exclude `.stampla/` from the sync.

## Status

Early development. Planned next:

- `import` — pull files off a memory card into a date-organized archive,
  named on arrival
- `organize` — audit a messy directory tree and produce a triage report,
  without touching anything
- integration with DAM tools (e.g. Adobe Lightroom Classic) so managed
  masters are renamed by the DAM itself via a metadata token

Safety first: every command is a dry run unless explicitly applied, and a
file whose capture time cannot be resolved is reported, never renamed.
Renames are validated as a whole before anything is touched, journaled to
`~/.stampla/journals/` before the first change, applied atomically per
file family, resumable after interruption, and revertable with
`stampla undo`. Nothing is ever overwritten.

## Configuration

An archive is described by a TOML file (see
[examples/config.toml](examples/config.toml)); every setting has a
sensible default:

| section | what it configures |
|---|---|
| `[[trees]]` | archive subtrees, their media kind and directory layout |
| `[pattern]` | the naming pattern: datetime format, digest, slice length — plus legacy patterns still recognized during a migration |
| `[dates]` | metadata fields tried in order to resolve capture time, and the timezone for UTC-only sources |
| `[extensions]` | which extensions are RAW masters, and which formats are edited in place |
| `[[sidecar_dirs]]` | sidecars kept in subdirectories beside their masters |
| `excludes` | glob patterns never to touch |
| `[dam]` | hand off renaming of DAM-managed masters via a metadata token |

## Requirements

- Python 3.11+
- [ExifTool](https://exiftool.org/) on `PATH`

No Python package dependencies (on Windows, `tzdata` is pulled in for the
timezone database).

## License

[MIT](LICENSE)
