"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stampla import __version__
from stampla.apply import ApplyResult, undo_journal
from stampla.config import Config, ConfigError, load_config
from stampla.dam import InjectOptions, run_inject
from stampla.exiftool import ExifToolError
from stampla.importer import ImportVerdict, apply_import, build_plan, verdict_of
from stampla.journal import FamilyMove, Journal, list_journals
from stampla.organize import run_organize
from stampla.renamer import RenameOptions, run_rename
from stampla.report import Bucket, Finding, Report
from stampla.verify import VerifyOptions, run_verify


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stampla",
        description="Deterministic, verifiable naming for photo and video archives.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, help="TOML configuration file")
    common.add_argument("--root", type=Path, help="archive root (overrides the config)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--workers", type=int, help="parallel hashing processes")

    applying = argparse.ArgumentParser(add_help=False)
    applying.add_argument(
        "--apply",
        action="store_true",
        help="make the planned changes; without this flag they are only shown",
    )
    hashing = argparse.ArgumentParser(add_help=False)
    hashing.add_argument(
        "--full",
        action="store_true",
        help="re-hash everything, ignoring the manifest cache",
    )
    hashing.add_argument(
        "--no-manifest",
        action="store_true",
        help="neither read nor update the per-machine manifest",
    )

    verify = subparsers.add_parser(
        "verify",
        parents=[common, hashing],
        help="recompute names from metadata and content, report what disagrees",
    )
    verify.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="limit verification to these paths (default: all configured trees)",
    )
    verify.add_argument(
        "--skip-hash",
        action="store_true",
        help="check capture times only; much faster, but misses content changes",
    )

    import_cmd = subparsers.add_parser(
        "import",
        parents=[common, applying],
        help="copy a memory card into the archive, named on arrival",
    )
    import_cmd.add_argument("card", type=Path, help="card or directory to import from")

    inject = subparsers.add_parser(
        "inject",
        parents=[common, applying, hashing],
        help="write computed names into the DAM's rename token for stale-named masters",
    )
    inject.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="limit injection to these paths (default: all DAM-managed trees)",
    )

    rename = subparsers.add_parser(
        "rename",
        parents=[common, applying, hashing],
        help="rename files whose derived name differs, through the journaled engine",
    )
    rename.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="limit renaming to these paths (default: all configured trees)",
    )

    organize = subparsers.add_parser(
        "organize",
        parents=[common],
        help="triage a messy tree: propose names, flag duplicates; never renames",
    )
    organize.add_argument("path", type=Path, help="messy directory to analyze")

    undo = subparsers.add_parser(
        "undo",
        help="revert a journaled apply run; without arguments, lists journals",
    )
    undo.add_argument("journal", nargs="?", type=Path, help="journal file to revert")
    undo.add_argument("--latest", action="store_true", help="revert the most recent journal")
    undo.add_argument("--json", action="store_true", help="machine-readable output")

    resume = subparsers.add_parser(
        "resume",
        help="finish an interrupted journaled apply run",
    )
    resume.add_argument("journal", type=Path, help="journal file to resume")
    resume.add_argument("--json", action="store_true", help="machine-readable output")
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
        if args.command == "resume":
            return _run_resume_command(args)
        if args.command == "import":
            return _run_import_command(args)
        if args.command == "inject":
            return _run_inject_command(args)
        if args.command == "rename":
            return _run_rename_command(args)
        if args.command == "organize":
            return _run_organize_command(args)
        return _run_verify_command(args)
    except (ConfigError, ExifToolError, ValueError, OSError) as error:
        print(f"stampla: {error}", file=sys.stderr)
        return 2


def _emit(
    args: argparse.Namespace,
    command: str,
    report: Report,
    plan: tuple[FamilyMove, ...] = (),
    applied: bool | None = None,
    text_extra: list[str] | None = None,
    verdict: ImportVerdict | None = None,
    result: ApplyResult | None = None,
) -> None:
    """One output shape for every command.

    JSON envelope: ``{command, applied, plan, summary, findings, hints,
    verdict, result}``; ``applied`` is null for read-only commands,
    ``verdict`` is import's safe-to-format decision (null elsewhere and
    on dry runs), ``result`` counts a journaled run's families (undo and
    resume). Text output prints the plan (dry runs), the report, then
    any command-specific lines.
    """
    if args.json:
        payload = json.loads(report.to_json())
        payload["command"] = command
        payload["applied"] = applied
        payload["plan"] = [
            {
                "key": move.key,
                "changes": [[str(r.old), str(r.new)] for r in move.renames],
            }
            for move in plan
        ]
        payload["verdict"] = (
            {
                "safe_to_format": verdict.safe_to_format,
                "imported": verdict.imported,
                "already_imported": verdict.already_imported,
                "ignored": verdict.ignored,
            }
            if verdict is not None
            else None
        )
        payload["result"] = (
            {
                "applied": len(result.applied),
                "skipped": len(result.skipped),
                "failed": len(result.failed),
            }
            if result is not None
            else None
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if applied is False:
        for move in plan:
            for rename in move.renames:
                print(f"  {rename.old}  ->  {rename.new}")
        if plan:
            print()
    print(report.render_text())
    for line in text_extra or []:
        print(line)


def _journal_result_report(result: object) -> Report:
    from stampla.apply import ApplyResult

    assert isinstance(result, ApplyResult)
    report = Report(ok=len(result.applied) + len(result.skipped))
    for key, error in result.failed:
        report.add(Finding(Bucket.APPLY_FAILED, Path(key), error))
    return report


def _run_undo_command(args: argparse.Namespace) -> int:
    journal_path = args.journal
    if journal_path is None and args.latest:
        journals = list_journals()
        if not journals:
            raise ValueError("no journals found; nothing to undo")
        journal_path = journals[-1]
    if journal_path is None:
        journals = list_journals()
        if not journals:
            print("no journals found")
            return 0
        for path in journals:
            print(path)
        print("\npass a journal path (or --latest) to undo one", file=sys.stderr)
        return 0
    journal = Journal.load(journal_path)
    _print_journal_header("undo", journal)
    result = undo_journal(journal)
    report = _journal_result_report(result)
    _emit(
        args,
        "undo",
        report,
        plan=journal.moves,
        applied=True,
        text_extra=[
            f"\nundo {journal_path.name}: {len(result.applied)} family(ies) reverted,"
            f" {len(result.skipped)} not applied, {len(result.failed)} failed"
        ],
        result=result,
    )
    return 0 if result.ok else 1


def _run_resume_command(args: argparse.Namespace) -> int:
    from stampla.apply import apply_plan

    journal = Journal.load(args.journal)
    _print_journal_header("resume", journal)
    result = apply_plan(journal)
    report = _journal_result_report(result)
    _emit(
        args,
        "resume",
        report,
        plan=journal.moves,
        applied=True,
        text_extra=[
            f"\nresume {args.journal.name}: {len(result.applied)} family(ies) applied,"
            f" {len(result.skipped)} already done, {len(result.failed)} failed"
        ],
        result=result,
    )
    return 0 if result.ok else 1


def _print_journal_header(action: str, journal: Journal) -> None:
    print(
        f"{action}: {journal.kind} journal with {len(journal.moves)} family(ies)"
        f" under {journal.root}",
        file=sys.stderr,
    )


def _run_import_command(args: argparse.Namespace) -> int:
    config, root = _config_and_root(args)
    plan = build_plan(config, root.resolve(), args.card, workers=args.workers)
    report = apply_import(plan, root.resolve()) if args.apply else plan.report
    verdict = verdict_of(report, applied=args.apply)

    extra: list[str] = []
    if not args.apply and plan.moves:
        extra.append(
            f"\ndry run: {len(plan.moves)} group(s) would be imported; pass --apply to copy"
        )
    elif verdict is not None and verdict.safe_to_format:
        skipped = f", {verdict.ignored} file(s) ignored (listed above)" if verdict.ignored else ""
        extra.append(
            f"\ncard fully accounted for: {verdict.imported} group(s) imported and verified,"
            f" {verdict.already_imported} already in the archive{skipped} — safe to format"
        )
    _emit(
        args,
        "import",
        report,
        plan=plan.moves,
        applied=args.apply,
        text_extra=extra,
        verdict=verdict,
    )
    return 1 if report.has_problems else 0


def _run_organize_command(args: argparse.Namespace) -> int:
    config, root = _config_and_root(args)
    report, plan = run_organize(config, root.resolve(), args.path, workers=args.workers)
    _emit(
        args,
        "organize",
        report,
        plan=plan.moves,
        applied=False,
        text_extra=[
            f"\n{len(plan.moves)} group(s) look importable; organize never renames —"
            " import confirmed batches with: stampla import <path> --apply"
        ],
    )
    return 1 if report.has_problems else 0


def _run_rename_command(args: argparse.Namespace) -> int:
    config, root = _config_and_root(args)
    options = RenameOptions(
        apply=args.apply,
        workers=args.workers,
        full=args.full,
        use_manifest=not args.no_manifest,
    )
    report, moves = run_rename(config, root.resolve(), tuple(args.paths), options)
    extra: list[str] = []
    if not args.apply and moves:
        total = sum(len(m.renames) for m in moves)
        extra.append(f"\ndry run: {total} rename(s) planned; pass --apply to execute")
    _emit(args, "rename", report, plan=moves, applied=args.apply, text_extra=extra)
    return 1 if report.has_problems else 0


def _run_inject_command(args: argparse.Namespace) -> int:
    config, root = _config_and_root(args)
    options = InjectOptions(
        apply=args.apply,
        workers=args.workers,
        full=args.full,
        use_manifest=not args.no_manifest,
    )
    report = run_inject(config, root.resolve(), tuple(args.paths), options)
    extra: list[str] = []
    written = sum(1 for f in report.findings if f.bucket is Bucket.TOKEN_WRITTEN)
    pending = sum(1 for f in report.findings if f.bucket is Bucket.TOKEN_PENDING)
    if pending:
        extra.append(f"\ndry run: {pending} token(s) would be written; pass --apply to write")
    if written:
        extra.append(
            f"\n{written} token(s) written. In the DAM: Read Metadata from Files"
            " on the affected folders, then rename with the token template"
            " (Lightroom Classic: the {Job Identifier} filename token)."
        )
    _emit(args, "inject", report, applied=args.apply, text_extra=extra)
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
    _emit(args, "verify", report)
    return 1 if report.has_problems else 0
