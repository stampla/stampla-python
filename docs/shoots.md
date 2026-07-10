# Shoots: filing by event, not just by month

Event and wedding photographers organize by *shoot* — one folder per
job — rather than one chronological stream. Other tools call the same
concept a *Shoot Name* (Lightroom Classic's import renaming), a *job
code* (Photo Mechanic, Rapid Photo Downloader) or a *session* (Capture
One). Stampla uses **shoot**: partly because it is the term
photographers use at import time, and partly because "job" already
means something else here (the DAM token field is IPTC's Job
Identifier).

## How it works

Put the `{shoot}` token in a tree's layout and name the shoot at
import time:

```toml
[[trees]]
path = "Events"
media = "photo"
layout = "{yyyy}/{shoot}"
```

```console
$ stampla import /Volumes/CARD --shoot "Anna Peter Wedding" --apply
  IMG_5001.CR2  ->  Events/2026/Anna_Peter_Wedding/20260516_152041_9b677b64.cr2
```

Rules, chosen so behavior stays predictable:

- **Normalization is one rule**: surrounding whitespace is trimmed and
  internal whitespace runs become `_`. Everything else must already be
  a letter, digit, `.`, `_` or `-` (max 64 characters) — anything else
  is rejected with the offending characters named, never silently
  mangled. A mangled shoot name would file photos somewhere the
  photographer would not look.
- **A `{shoot}` tree refuses to import without `--shoot`.** Guessing a
  shoot is filing files where a later import cannot find them.
- **A `--shoot` that nothing uses is an error**, not a shrug — it will
  not be silently ignored.
- Chronological and shoot-based trees coexist: only trees whose layout
  contains `{shoot}` are affected.

## Why the shoot lives in the directory, not the filename

The name grammar is `<prefix>[<suffix>][.<raw_ext>].<ext>`, and every
part has a load-bearing meaning:

- **Before the digest** (`20260516_152041_Anna_Peter_Wedding_9b677b64.cr2`)
  is rejected because parsing becomes heuristic: a shoot may itself
  contain an eight-hex-digit word, and derivative suffixes may follow
  the digest, so the parser would have to guess which token is the
  digest. A wrong guess does not fail loudly — it verifies the wrong
  bytes, which is exactly the class of silent misinformation this tool
  exists to prevent.
- **After the digest** (`…_9b677b64_Anna_Peter_Wedding.cr2`) parses
  cleanly today — but the suffix slot *means* "derivative of the
  suffix-free master". A shoot-suffixed master is grammar-legal and
  semantically an orphan: verify would (correctly, by its own rules)
  report the group as having no master. Supporting this placement
  would need a reserved shoot delimiter in the grammar (for example a
  double underscore: `…_9b677b64__Anna_Peter_Wedding.cr2`), threaded
  through parsing, group membership, master detection and rename
  rebuilding. That is a coherent possible extension — recorded here as
  a design sketch — but it changes the naming grammar, which is the
  one thing this project treats as close to immutable.
- **The directory** carries the shoot with zero grammar impact:
  verification derives names purely from file content and metadata and
  never from location, renames happen in place, and groups stay
  whole. The filename remains the same stable identity everywhere —
  a file moved between a chronological tree and a shoot tree keeps its
  name.

One practical consequence, stated honestly: with the shoot in the
directory only, the filename alone does not tell you the shoot. That
is the same trade every camera-original archive makes (the month
folder is not in the filename either), and the identity is what makes
the file findable by content regardless of folder.
