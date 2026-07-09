# Stampla

[![CI](https://github.com/stampla/stampla/actions/workflows/ci.yml/badge.svg)](https://github.com/stampla/stampla/actions/workflows/ci.yml)

Deterministic, verifiable naming for photo and video archives.

The name is Stampla in prose; the command, package, and all
identifiers are `stampla`, and it is never two words.

`stampla` names every file in an archive after what it is and when it
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
file is re-hashed at its destination and compared with the digest read
from the card, so a transfer error cannot slip through. Nothing changes
without `--apply`, and every apply is journaled and revertable with
`stampla undo`.

**After `--apply`, exit code 0 means the card is fully accounted for** —
every file was either copied and verified, already sits in the archive
with byte-identical content, or is explicitly listed as ignored (hidden
paths, your `[import]` ignore globs, skipped JPEG twins — review that
list once; anything you ignore by policy exists only on the card).
Anything else (unresolvable capture time, a same-name file in the
archive with *different* content, a family only partially present)
exits 1 and blocks the "safe to format" verdict. Re-running `import`
after an import is therefore the pre-format check. With `--json` the
verdict is structural, not just an exit code: the envelope carries
`verdict.safe_to_format` with the imported / already-imported /
ignored counts.

Directories after the card path narrow the import to just those
batches (`stampla import /Volumes/CARD /Volumes/CARD/keepers`) —
made for importing triaged groups out of `organize`. A selective run
never clears the card for formatting: the verdict speaks for the whole
card, and only a full import can issue it.

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
between full runs. `--json` emits the report machine-readably, and
`--json-stream` emits NDJSON — progress events while the command runs,
the result as the last line. The exit code is `0` when clean, `1` with
findings, `2` on errors.

Hashes and resolved capture times are cached in a per-machine manifest
(`.stampla/manifest-<machine>.tsv` under the archive root), so repeat
runs only re-examine new and touched files — `verify`, `rename` and
`inject` all use it. A cached entry is trusted only while the file's
size and mtime are unchanged (any metadata write invalidates it), and
cached dates are additionally keyed to the date chain and timezone, so
editing either re-resolves everything. `--full` re-reads and re-hashes
everything regardless — run it periodically, since silent corruption
that preserves size and mtime is exactly what the cache cannot see.
`--no-manifest` disables the cache entirely. If you sync the archive
between machines, exclude `.stampla/` from the sync.

### inject

A DAM (e.g. Adobe Lightroom Classic) must rename the masters it manages
itself, or its catalog loses track of them — but it cannot compute
content hashes. `stampla inject` bridges the two: it finds masters
whose names have gone stale, writes each one's freshly computed name
into a metadata field the DAM exposes as a filename template token, and
the DAM does the renaming.

```console
$ stampla inject --config archive.toml --apply
...
3 token(s) written. In the DAM: Read Metadata from Files on the affected
folders, then rename with the token template (Lightroom Classic: the
{Job Identifier} filename token).
```

The token lands in the master's `.xmp` sidecar for RAW files, or inside
the file itself for embedded-metadata formats (JPEG, DNG, TIFF). A RAW
without a sidecar is reported as `needs-sidecar` — `stampla` never
fabricates sidecars, because a DAM reading a minimal script-made file
could wipe catalog-side metadata. Save metadata from the DAM first,
then rerun.

### rename

Direct renames, for what no DAM manages: whole families in unmanaged
trees (fixing a wrong capture date renames the master and every sidecar
atomically), and — in DAM-managed trees — only the members the DAM does
not know about, while `inject` handles the master. Also fixes names that
differ from canonical only by extension case (`.FP2` → `.fp2`).

```console
$ stampla rename --config archive.toml
  /archive/Video/2025/2025-06/20250615_140910_388ec696.mov  would become 20250615_160910_388ec696.mov

dry run: 1 rename(s) planned; pass --apply to execute
```

Every apply is validated as a whole first, journaled before the first
change, applied atomically per family, and revertable with
`stampla undo`.

### organize

Triage for the messy tree every archive drags along. Runs the import
planning over it and reports, without ever renaming: what each group
would be named and where it would go, what is already in the archive,
which groups duplicate each other, what could only be dated from file
modification time (proposed, but flagged — mtime is hearsay), and what
is unresolvable. There is no `--apply`; import confirmed batches with
`stampla import`.

## Status

Early development; command surface complete, release preparation in
progress. See [CHANGELOG.md](CHANGELOG.md).

Safety first: every command is a dry run unless explicitly applied, and a
file whose capture time cannot be resolved is reported, never renamed.
Long runs show a live progress line on the terminal, and interrupting
one (Ctrl-C) is safe: planning stops cleanly, and an interrupted apply
is the journal's own case — finish it with `resume` or revert it with
`undo`.
Applies are validated as a whole before anything is touched, guarded by a
per-archive lock, journaled to `~/.stampla/journals/` before the first
change, applied atomically per file family, finishable after interruption
with `stampla resume`, and revertable with `stampla undo` — which,
for imported copies, re-verifies each file's digest and refuses to delete
anything edited since. `stampla journals` lists every recorded run
with its originating command and status (pending, partial, complete,
undone), optionally narrowed to one archive with `--config`/`--root`.
Nothing is ever overwritten. Exit codes everywhere:
`0` clean, `1` findings needing attention, `2` errors.

## Configuration

An archive is described by a TOML file; every setting has a sensible
default. See [docs/examples.md](docs/examples.md) for six complete
setups — from a single folder of photos to a Lightroom workflow and a
naming-scheme migration — and [examples/config.toml](examples/config.toml)
for every option annotated:

| section | what it configures |
|---|---|
| `[[trees]]` | archive subtrees, their media kind and directory layout |
| `[pattern]` | the naming pattern: datetime format, digest, slice length — per-format image-data hashing, plus additional patterns recognized during a migration |
| `[dates]` | metadata fields tried in order to resolve capture time, and the timezone for UTC-only sources |
| `[extensions]` | which extensions are RAW masters, and which formats are edited in place |
| `[[sidecar_dirs]]` | sidecars kept in subdirectories beside their masters |
| `excludes` | glob patterns never to touch |
| `[import]` | card files to ignore (camera housekeeping, `*.jpg` for RAW-only shooters) and the RAW+JPEG twin policy |
| `[dam]` | hand off renaming of DAM-managed masters via a metadata token |

## Requirements

- Python 3.11+
- [ExifTool](https://exiftool.org/) on `PATH`

No Python package dependencies (on Windows, `tzdata` is pulled in for the
timezone database).

## License

[MIT](LICENSE)
