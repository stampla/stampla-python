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

## Status

Early development. The first release will provide:

- `import` — pull files off a memory card into a date-organized archive,
  named on arrival
- `verify` — re-derive every name and report mismatches, classified as
  corruption, legitimate edit, or naming drift
- `organize` — audit a messy directory tree and produce a triage report,
  without touching anything
- integration with DAM tools (e.g. Adobe Lightroom Classic) so managed
  masters are renamed by the DAM itself via a metadata token

Safety first: every command is a dry run unless explicitly applied, renames
are journaled and undoable, and a file whose capture time cannot be resolved
is reported, never renamed.

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
