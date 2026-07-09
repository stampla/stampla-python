# Stampla

[![CI](https://github.com/stampla/stampla/actions/workflows/ci.yml/badge.svg)](https://github.com/stampla/stampla/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/stampla)](https://pypi.org/project/stampla/)

Deterministic, verifiable naming for photo and video archives.

`stampla` renames every photo and video to an identity derived
purely from the file itself — the capture time plus a slice of the
content hash:

```
20260703_150727_9b677b64.nef
└──────┬──────┘ └──┬───┘
  capture time    hash
```

That name is a stable, unique identifier with properties a filename
usually doesn't have:

- **Chronological by construction.** Sorting by name is sorting by
  capture time, across every camera, phone and scanner — no rolling
  counters, no `IMG_0001 (2).jpg`.
- **Verifiable.** Because the name is reproducible from the file, the
  whole archive can be re-checked at any time: corruption is told apart
  from legitimate edits, and duplicates identify themselves.
- **Family-safe.** RAW masters travel with their XMP/PP3 sidecars and
  editor derivatives — everything shares the master's prefix and is
  renamed together, atomically.

## Install

```console
$ pip install stampla
```

Python 3.11+ and [ExifTool](https://exiftool.org/) on `PATH`; no other
dependencies.

## Usage

| command | what it does |
|---|---|
| `import` | copy a memory card into the archive, named on arrival; exit 0 certifies the card is fully accounted for — safe to format |
| `verify` | recompute every name and report what disagrees, classified by meaning (corruption vs. expected drift vs. date mismatch) |
| `rename` | bring stale names in line, atomically per file family |
| `inject` | let a DAM (Lightroom Classic) rename the masters it manages, via a metadata token |
| `organize` | report-only triage for messy trees: proposals, duplicates, undatable files |
| `history` / `undo` / `resume` | every apply is journaled: list runs, revert them, finish interrupted ones |

Safety first: every command is a dry run unless `--apply`. Applies are
validated as a whole before anything is touched, journaled before the
first change, applied atomically per family, resumable after
interruption and revertable — and a file whose capture time cannot be
resolved is reported, never renamed. Nothing is ever overwritten.

See the
[command guide](https://github.com/stampla/stampla/blob/main/docs/commands.md)
for details, output formats and the exit-code contract, and
[DESIGN.md](https://github.com/stampla/stampla/blob/main/DESIGN.md)
for why it works this way.

## Configuration

An archive is described by a TOML file; every setting has a sensible
default. See
[six complete example setups](https://github.com/stampla/stampla/blob/main/docs/examples.md)
— from a single folder of photos to a Lightroom workflow and a
naming-scheme migration — and the
[annotated example config](https://github.com/stampla/stampla/blob/main/examples/config.toml)
for every option.

## License

[MIT](https://github.com/stampla/stampla/blob/main/LICENSE)
