"""Command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stampla import __version__
from stampla.config import Config, ConfigError, load_config
from stampla.exiftool import ExifToolError
from stampla.verify import VerifyOptions, run_verify


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stampla",
        description="Deterministic, verifiable naming for photo and video archives.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    verify = subparsers.add_parser(
        "verify",
        help="recompute names from metadata and content, report what disagrees",
    )
    verify.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="limit verification to these paths (default: all configured trees)",
    )
    verify.add_argument("--config", type=Path, help="TOML configuration file")
    verify.add_argument("--root", type=Path, help="archive root (overrides the config)")
    verify.add_argument("--json", action="store_true", help="machine-readable output")
    verify.add_argument(
        "--skip-hash",
        action="store_true",
        help="check capture times only; much faster, but misses content changes",
    )
    verify.add_argument("--workers", type=int, help="parallel hashing processes")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        return _run_verify_command(args)
    except (ConfigError, ExifToolError, ValueError) as error:
        print(f"stampla: {error}", file=sys.stderr)
        return 2


def _run_verify_command(args: argparse.Namespace) -> int:
    config = load_config(args.config) if args.config else Config()
    root = args.root or (Path(config.root) if config.root else None)
    if root is None:
        raise ConfigError("no archive root: set 'root' in the config or pass --root")
    options = VerifyOptions(skip_hash=args.skip_hash, workers=args.workers)
    report = run_verify(config, root, args.paths, options)
    print(report.to_json() if args.json else report.render_text())
    return 1 if report.has_findings else 0
