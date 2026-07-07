# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
  per-family-atomic engine; whole families outside DAM-managed trees,
  DAM-unaware members inside them; extension-case fixes.
- `organize` — report-only triage for messy trees: proposals, duplicate
  clusters, already-archived detection, flagged mtime-dated proposals.
- `undo` — revert any journaled apply run.
- Naming patterns with per-extension digest sources: whole-file digests
  by default, image-data digests (ExifTool `ImageDataHash`) for formats
  that DAMs edit in place, so their names never drift. Additional
  recognized patterns keep an archive classifiable during a migration.
