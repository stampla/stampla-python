"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stampla import __version__
from stampla.apply import undo_journal
from stampla.config import Config, ConfigError, load_config
from stampla.dam import InjectOptions, run_inject
from stampla.exiftool import ExifToolError
from stampla.importer import apply_import, build_plan
from stampla.journal import Journal, list_journals
from stampla.report import Bucket
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
    verify.add_argument(
        "--full",
        action="store_true",
        help="re-hash everything, ignoring the manifest cache",
    )
    verify.add_argument(
        "--no-manifest",
        action="store_true",
        help="neither read nor update the per-machine manifest",
    )
    verify.add_argument("--workers", type=int, help="parallel hashing processes")

    import_cmd = subparsers.add_parser(
        "import",
        help="copy a memory card into the archive, named on arrival",
    )
    import_cmd.add_argument("card", type=Path, help="card or directory to import from")
    import_cmd.add_argument("--config", type=Path, help="TOML configuration file")
    import_cmd.add_argument("--root", type=Path, help="archive root (overrides the config)")
    import_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    import_cmd.add_argument(
        "--apply",
        action="store_true",
        help="actually copy; without this flag the plan is only shown",
    )
    import_cmd.add_argument("--workers", type=int, help="parallel hashing processes")

    inject = subparsers.add_parser(
        "inject",
        help="write computed names into the DAM's rename token for stale-named masters",
    )
    inject.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="limit injection to these paths (default: all DAM-managed trees)",
    )
    inject.add_argument("--config", type=Path, help="TOML configuration file")
    inject.add_argument("--root", type=Path, help="archive root (overrides the config)")
    inject.add_argument("--json", action="store_true", help="machine-readable output")
    inject.add_argument(
        "--apply",
        action="store_true",
        help="actually write tokens; without this flag intended writes are only shown",
    )
    inject.add_argument("--workers", type=int, help="parallel hashing processes")

    undo = subparsers.add_parser(
        "undo",
        help="revert a journaled apply run (most recent by default)",
    )
    undo.add_argument("journal", nargs="?", type=Path, help="journal file to revert")
    undo.add_argument("--list", action="store_true", help="list available journals")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "undo":
            return _run_undo_command(args)
        if args.command == "import":
            return _run_import_command(args)
        if args.command == "inject":
            return _run_inject_command(args)
        return _run_verify_command(args)
    except (ConfigError, ExifToolError, ValueError, OSError) as error:
        print(f"stampla: {error}", file=sys.stderr)
        return 2


def _run_undo_command(args: argparse.Namespace) -> int:
    if args.list:
        for path in list_journals():
            print(path)
        return 0
    journal_path = args.journal
    if journal_path is None:
        journals = list_journals()
        if not journals:
            raise ValueError("no journals found; nothing to undo")
        journal_path = journals[-1]
    result = undo_journal(Journal.load(journal_path))
    print(
        f"undo {journal_path.name}: {len(result.applied)} family(ies) reverted,"
        f" {len(result.skipped)} not applied, {len(result.failed)} failed"
    )
    for key, error in result.failed:
        print(f"  FAILED {key}: {error}", file=sys.stderr)
    return 0 if result.ok else 1


def _run_import_command(args: argparse.Namespace) -> int:
    config, root = _config_and_root(args)
    plan = build_plan(config, root.resolve(), args.card, workers=args.workers)
    report = apply_import(plan, root.resolve()) if args.apply else plan.report

    if args.json:
        payload = json.loads(report.to_json())
        payload["applied"] = args.apply
        payload["planned"] = [
            {
                "family": move.key,
                "copies": [[str(r.old), str(r.new)] for r in move.renames],
            }
            for move in plan.moves
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if not args.apply:
            for move in plan.moves:
                for rename in move.renames:
                    print(f"  {rename.old}  ->  {rename.new}")
            if plan.moves:
                print()
        print(report.render_text())
        if not args.apply and plan.moves:
            print(f"\ndry run: {len(plan.moves)} group(s) would be imported; pass --apply to copy")
        elif not report.has_problems:
            already = sum(1 for f in report.findings if f.bucket is Bucket.ALREADY_IMPORTED)
            ignored = sum(1 for f in report.findings if f.bucket is Bucket.IGNORED)
            skipped = f", {ignored} file(s) ignored (listed above)" if ignored else ""
            print(
                f"\ncard fully accounted for: {report.ok} group(s) imported and verified,"
                f" {already} already in the archive{skipped} — safe to format"
            )
    return 1 if report.has_problems else 0


def _run_inject_command(args: argparse.Namespace) -> int:
    config, root = _config_and_root(args)
    options = InjectOptions(apply=args.apply, workers=args.workers)
    report = run_inject(config, root.resolve(), tuple(args.paths), options)
    print(report.to_json() if args.json else report.render_text())
    if not args.json:
        written = sum(1 for f in report.findings if f.bucket is Bucket.TOKEN_WRITTEN)
        pending = sum(1 for f in report.findings if f.bucket is Bucket.TOKEN_PENDING)
        if pending:
            print(f"\ndry run: {pending} token(s) would be written; pass --apply to write")
        if written:
            print(
                f"\n{written} token(s) written. In the DAM: Read Metadata from Files"
                " on the affected folders, then rename with the token template"
                " (Lightroom Classic: the {Job Identifier} filename token)."
            )
    return 1 if report.has_problems else 0


def _config_and_root(args: argparse.Namespace) -> tuple[Config, Path]:
    config = load_config(args.config) if args.config else Config()
    root = args.root or (Path(config.root) if config.root else None)
    if root is None:
        raise ConfigError("no archive root: set 'root' in the config or pass --root")
    return config, root


def _run_verify_command(args: argparse.Namespace) -> int:
    config, root = _config_and_root(args)
    options = VerifyOptions(
        skip_hash=args.skip_hash,
        workers=args.workers,
        full=args.full,
        use_manifest=not args.no_manifest,
    )
    report = run_verify(config, root, args.paths, options)
    print(report.to_json() if args.json else report.render_text())
    return 1 if report.has_findings else 0
