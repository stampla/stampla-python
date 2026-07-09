# Contributing

## Development setup

```console
$ python3 -m venv .venv
$ .venv/bin/pip install -e ".[dev]"
```

[ExifTool](https://exiftool.org/) must be on `PATH`; integration tests
skip without it, but CI always runs them.

## Checks

Everything CI runs, locally:

```console
$ ruff format --check .
$ ruff check .
$ mypy
$ pytest --cov=stampla
```

All four must pass; coverage is gated at 90%.

## Expectations

- Every change ships with tests and any needed documentation in the same
  commit. End-to-end tests build real archives in `tmp_path` and go
  through the public CLI.
- Safety invariants are not configurable: dry run by default, never
  overwrite, never produce a name from a partial date, journal before
  changing anything. Treat a change that would weaken one as a design
  discussion, not a patch.
- Conventional Commits (`feat:`, `fix:`, `docs:`, `ci:`, `chore:`).
- Naming: Stampla in prose; `stampla` for the command, the
  package and all identifiers — never two words.
- README links must be absolute URLs: PyPI renders the README without
  rewriting relative links.

## Releasing

Releases are deliberate and manual; automation takes over at the tag:

1. Set `__version__` in `src/stampla/__init__.py`.
2. Retitle the changelog's `[Unreleased]` section to `[X.Y.Z] - date`.
3. Commit as `chore: release X.Y.Z`, tag `vX.Y.Z`, push both.

The tag triggers `release.yml`: full checks, build, publish to PyPI via
the trusted publisher, and a GitHub release whose notes are that
version's changelog section — the changelog is the single source of
release prose.
