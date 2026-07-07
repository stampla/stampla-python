# stampla

Deterministic, verifiable naming for photo and video archives.

stampla names every file in an archive after what it is and when it
happened:

```
20260703_150727_9b677b64.nef
└─ capture time ─┘└ hash ┘
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

## Requirements

- Python 3.11+
- [ExifTool](https://exiftool.org/) on `PATH`

No Python package dependencies.

## License

[MIT](LICENSE)
