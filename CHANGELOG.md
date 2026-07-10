# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-07-11

### Changed

- Naming patterns validate what the archive's promises require:
  `datetime_format` must use each of `%Y %m %d %H %M %S` exactly once,
  most significant first (sorting names must sort by capture time), and
  separators and format literals must be filename-safe on every
  platform (`:`, Windows-reserved characters and control characters are
  rejected).
- The 31-character prefix cap now applies only while a `[dam]` section
  is configured — it exists to fit rename tokens into the 32-character
  IPTC TransmissionReference field. Without DAM integration, longer
  prefixes such as `sha256` at 22 characters are legal.

## [0.2.0] - 2026-07-10

### Changed

- Renamed the central concept: files sharing a name prefix form a
  *group* (previously *family*). User-facing output, documentation and
  the API follow (`Group`, `GroupMove`, `stampla.group`); in JSON
  output the `orphan-family` bucket is now `orphan-group` and the
  summary key `families` is now `groups`. ExifTool's namespaces are
  always spelled out as *tag groups* to keep the two apart.

## [0.1.0] - 2026-07-09

### Added

- Finding severities — every bucket is classified `alarm`, `attention`,
  `expected` or `safe`; the exit-code rule is stated once in terms of
  them, and JSON findings carry the severity.
- Machine-readable finding data: where a detail line contains values
  (expected vs. actual timestamps and digests, the dating tag, a pending
  token, why a card file was ignored), the same values ride along in a
  `data` object in JSON output.
- Structural verdicts: import's safe-to-format decision, with imported /
  already-imported / ignored counts, and undo/resume group counts are
  part of the JSON envelope instead of text-only lines.
- Progress and cancellation: long operations report per-file and
  per-group progress and can be cancelled at safe points; the CLI shows
  a live progress line on a terminal, and Ctrl-C exits cleanly with a
  pointer to `resume`.
- `history` — list every journaled apply run with its originating
  command and status (pending, partial, complete, undone), optionally
  narrowed to one archive; journals record their provenance.
- Selective import: directories after the card path narrow the run to
  those batches; a selective run never issues the safe-to-format
  verdict.
- Versioned JSON envelope (`format: 1`): bucket counts nested under
  `summary.buckets`, the archive root stated once with root-relative
  paths beneath it.
- `--json-stream` — NDJSON output: progress events while a command
  runs, the result envelope as the final line.

- `verify` — recompute every name from metadata and content; findings
  classified by meaning (corruption vs expected drift vs date mismatch vs
  pending migration), with a per-machine manifest so repeat runs only hash
  new and touched files.
- `import` — copy a memory card into the archive, named on arrival; card
  files are never modified; every copy is re-hashed at its destination.
  Exit code 0 certifies the card is fully accounted for ("safe to
  format"), with content-compared duplicate detection, ignore globs and
  an optional RAW+JPEG twin policy.
- `inject` — write computed names into a DAM's rename token
  (Lightroom Classic: IPTC Job Identifier) so the DAM renames the masters
  it manages without losing track of them.
- `rename` — direct renames through a validated, write-ahead-journaled,
  per-group-atomic engine; whole groups outside DAM-managed trees,
  DAM-unaware members inside them; extension-case fixes.
- `organize` — report-only triage for messy trees: proposals, duplicate
  clusters, already-archived detection, flagged mtime-dated proposals.
- `undo` — revert any journaled apply run.
- Naming patterns with per-extension digest sources: whole-file digests
  by default, image-data digests (ExifTool `ImageDataHash`) for formats
  that DAMs edit in place, so their names never drift. Additional
  recognized patterns keep an archive classifiable during a migration.
