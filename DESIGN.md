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
primary pattern first, then any additional recognized ones, so an archive
migrating from, say, `md5:8` to `sha256:12` names remains fully classifiable
mid-transition: every file is either canonical under the primary pattern,
valid under an additional pattern (reported as pending migration — a mixed
archive is a loudly temporary state, never a comfortable one), or not
canonically named at all.

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

Queries always use `-a` with group-qualified names, because the same tag
name routinely appears several times in one file with different meanings.
A video may carry a maker-notes `CreateDate` in local wall-clock time
*and* a QuickTime `CreateDate` in UTC; without group qualification, which
one is returned is an accident of tag priority. Downstream logic therefore
always sees group-qualified tags (`MakerNotes:CreateDate`,
`QuickTime:CreateDate`) and can rank them deliberately.

## Resolving capture time

Capture time is resolved by an ordered chain of tag names per media kind;
the first entry yielding a complete timestamp wins. The defaults:

```
photo:  EXIF:DateTimeOriginal → EXIF:CreateDate → XMP:DateCreated
video:  DateTimeOriginal → CreateDate → QuickTime:CreateDate
```

An unqualified entry matches the tag in any group *except* groups the
chain names explicitly for that same tag. That one rule encodes the
QuickTime problem: most cameras write their trustworthy local wall-clock
time into maker notes and a UTC copy into the QuickTime atoms, while some
formats (BRAW) have *only* a QuickTime timestamp — which is local. So
`CreateDate` prefers any maker-notes value, and `QuickTime:CreateDate`
serves as the explicit last resort for files that offer nothing else.

Resolved values are naive local wall-clock time — what a person at the
scene would have read off a watch. Timestamps that are incomplete, zeroed
or implausible never resolve; a file without a resolvable capture time is
reported and skipped, because a wrong-but-plausible name is worse than no
rename. For sources known to store only UTC (typically phone videos), a
DST-aware conversion into a configured timezone is available and its use
is always flagged in reports.

## Families

All files sharing one name prefix form a family and are renamed as one
unit. Because the prefix embeds a content hash, it is unique per master
across the whole archive — so sidecars kept in subdirectories
(`NKSC_PARAM/<master>.nksc`) join their master's family with no directory
logic at all.

Within a family, the *master* is the unique member shaped `prefix.ext`
with a master extension (RAW formats for photo trees, video containers
for video trees). Some families legitimately have none (an orphan sidecar
whose master was deleted) or several candidates (a RAW plus a DNG
conversion deliberately named after it). Both are reported rather than
guessed at structurally; when hashes are available, the true master is
the candidate whose content matches the prefix, and same-prefix
conversions behave like derivatives — they inherit the name and are
re-prefixed with the family.

Files that are not yet named (a memory card during import) group by
directory and original base name instead: `DSC1234.NEF`, `DSC1234.xmp`
and `DSC1234.NEF.xmp` share the base `DSC1234`, and sidecar-directory
rules map `NKSC_PARAM/DSC1234.NEF.nksc` to the master's directory first.
A base extending another base with a `-`/`_` label (`DSC1234-Edit`)
merges into the shorter group only when the shorter group has a
camera-native master and the labeled group does not — editor output like
`DSC1234-Edit.tif` travels with its RAW, while `IMG_01.NEF` is never
mistaken for a derivative of `IMG.NEF`.

## Verification

Because names are pure functions of the file, verification is just
re-derivation: resolve the capture time, hash the content, rebuild the
prefix and compare. What makes verify useful is the classification of
disagreements:

| bucket | meaning |
|---|---|
| `corruption` | content hash differs on a write-once format — alarm |
| `edit-drift` | content hash differs on a format edited in place — expected; the name is stale until re-named |
| `date-mismatch` | the name's timestamp disagrees with metadata |
| `unresolved-date` | no chain entry yields a capture time — the file can never be auto-named |
| `collision` | two masters derive the same name, i.e. duplicate content |
| `ambiguous-master` | several same-prefix master candidates and content settles nothing |
| `orphan-family` | sidecars whose master is gone |
| `malformed` / `unnamed` | inventory of files outside the scheme |

Only the master of each family is hashed and dated — sidecars and
derivatives inherit the master's prefix by definition, so their names are
right exactly when their master's is. Ambiguous families (a RAW plus a
conversion carrying the same prefix) are settled by evidence: the
candidate whose content hash matches the prefix is the master.

## The hash manifest

Hashing a large archive is minutes of work; doing it on every verify run
would discourage running verify at all. The manifest caches digests
per machine — `.stampla/manifest-<machine>.tsv` under the archive
root — keyed by relative path and vouched for by size and mtime. Any
mtime or size change invalidates the entry; there is no clock heuristic
and no grace window.

Design choices, deliberately boring:

- **One file per machine.** Machines that sync an archive never write to
  each other's manifest, so sync conflicts are structurally impossible.
  The directory should still be excluded from sync: each machine's
  "when did I last verify this here" is meaningful only locally.
- **TSV without quoting.** Tabs and newlines are rejected in paths rather
  than escaped — they do not occur in real archives, and a format without
  an escaping layer can be processed with `cut` and `awk` safely.
- **Growable rows.** Readers ignore extra columns and skip short or
  unparsable rows, so columns can be appended without a migration; the
  worst case is a re-hash.
- **Honest trust boundary.** The cache cannot detect corruption that
  preserves both size and mtime. `--full` bypasses it entirely and should
  be run periodically; the manifest makes the *routine* case fast, it
  does not replace the deep check.

## Renaming safely

Everything that writes goes through one engine, with protections in a
fixed order:

1. **Global validation before any I/O.** Every source must exist, no two
   renames may share a source or target, no target may already exist,
   and every path must stay inside the archive root. One problem
   anywhere means nothing is touched — a plan is valid as a whole or not
   at all.
2. **Write-ahead journal.** The complete plan is persisted to
   `~/.stampla/journals/` — outside the archive — before the first
   rename. As each family completes, its key is appended to a done-log;
   appends are cheap and crash-safe.
3. **Per-family atomicity.** A family's renames either all happen or the
   already-done ones are reverted on the spot; a failed family never
   leaves a master separated from its sidecars. Other families proceed.
4. **Resume and undo.** Re-running an interrupted journal skips families
   already in the done-log. `stampla undo` reverts a journal's done
   families in reverse order, with the same no-clobber rules.

Renames never overwrite. An existing target is a refusal and a report,
not a replacement — duplicate content is a finding for a human, not a
conflict for the tool to resolve.

## The embedded-token circularity

For a master whose metadata lives inside the file (JPEG, DNG, TIFF),
writing the DAM rename token changes the file's content — so a name
embedding the content hash can never match the file that carries its own
name. There is no fixpoint: injecting `date_H₀` produces content with
hash `H₁`, injecting `date_H₁` produces `H₂`, forever.

stampla resolves this by separating *naming* from *injecting*:

- **Import names files before any DAM sees them.** A scanned TIFF or a
  JPEG-only archive gets a name matching its exact bytes, untouched.
  The name stays hash-true until the file is first edited — the normal
  mutable-format contract.
- **Derivatives are never injected.** A DNG or TIFF sharing a RAW's
  prefix is a family member, renamed by the tool with its master.
- **DAM-managed standalone embedded masters accept one generation of
  drift.** The injected name reflects the content just before the token
  write. Once the DAM has renamed the file, name and stored token are
  equal — that is the *convergence marker*. Inject treats such masters
  as done rather than chasing its own tail, while a changed capture
  date still re-triggers (dates are not part of the circularity).
- **Files not managed by any DAM should be renamed directly** — no
  token, no modification, an exact hash. The token flow exists only
  because a DAM must rename its own files.
