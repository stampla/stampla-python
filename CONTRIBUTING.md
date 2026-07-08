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
