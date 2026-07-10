"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from stampla import __version__
from stampla.apply import ApplyResult, undo_journal
from stampla.config import Config, ConfigError, load_config
from stampla.dam import InjectOptions, run_inject
from stampla.exiftool import ExifToolError
from stampla.importer import ImportVerdict, apply_import, build_plan, verdict_of
from stampla.journal import GroupMove, Journal, journal_summaries, list_journals
from stampla.organize import run_organize
from stampla.progress import Monitor, ProgressEvent
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
    common.add_argument(
        "--json-stream",
        action="store_true",
        help="NDJSON on stdout: progress events while running, the result envelope last",
    )
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
    import_cmd.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="limit the import to these directories on the card"
        " (a selective run never clears the card for formatting)",
    )
    import_cmd.add_argument(
        "--shoot",
        help="shoot/job name for this card: fills the {shoot} layout token"
        " in tree layouts; whitespace becomes underscores",
    )

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

    history = subparsers.add_parser(
        "history",
        help="list journaled apply runs with their status",
    )
    history.add_argument("--config", type=Path, help="TOML configuration file")
    history.add_argument(
        "--root", type=Path, help="only runs against this archive root (overrides the config)"
    )
    history.add_argument("--json", action="store_true", help="machine-readable output")

    undo = subparsers.add_parser(
        "undo",
        help="revert a journaled apply run; without arguments, lists journals",
    )
    undo.add_argument("journal", nargs="?", type=Path, help="journal file to revert")
    undo.add_argument("--latest", action="store_true", help="revert the most recent journal")
    undo.add_argument("--json", action="store_true", help="machine-readable output")
    undo.add_argument(
        "--json-stream",
        action="store_true",
        help="NDJSON on stdout: progress events while running, the result envelope last",
    )

    resume = subparsers.add_parser(
        "resume",
        help="finish an interrupted journaled apply run",
    )
    resume.add_argument("journal", type=Path, help="journal file to resume")
    resume.add_argument("--json", action="store_true", help="machine-readable output")
    resume.add_argument(
        "--json-stream",
        action="store_true",
        help="NDJSON on stdout: progress events while running, the result envelope last",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "history":
            return _run_history_command(args)
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
    except KeyboardInterrupt:
        print(
            "\nstampla: interrupted — a journaled apply can be finished"
            " with the resume command or reverted with undo",
            file=sys.stderr,
        )
        return 130


def _throttled(handler: Callable[[ProgressEvent], None]) -> Callable[[ProgressEvent], None]:
    """At most ten events a second — a fast phase would otherwise emit
    thousands — but a phase's final event (done == total) always lands."""
    last = 0.0

    def wrapped(event: ProgressEvent) -> None:
        nonlocal last
        now = time.monotonic()
        if now - last < 0.1 and event.done != event.total:
            return
        last = now
        handler(event)

    return wrapped


@contextmanager
def _progress(args: argparse.Namespace) -> Iterator[Monitor]:
    """Progress: NDJSON events on stdout with ``--json-stream``,
    otherwise a live line on stderr when it is a terminal."""
    if getattr(args, "json_stream", False):

        def emit(event: ProgressEvent) -> None:
            line = {
                "event": "progress",
                "phase": event.phase,
                "done": event.done,
                "total": event.total,
                "path": str(event.path) if event.path is not None else None,
            }
            print(json.dumps(line, ensure_ascii=False), flush=True)

        yield Monitor(callback=_throttled(emit))
        return
    if not sys.stderr.isatty():
        yield Monitor()
        return

    def render(event: ProgressEvent) -> None:
        total = f"/{event.total}" if event.total else ""
        name = f"  {event.path.name}" if event.path is not None else ""
        print(
            f"\r\x1b[2K  {event.phase}: {event.done}{total}{name}",
            end="",
            file=sys.stderr,
            flush=True,
        )

    try:
        yield Monitor(callback=_throttled(render))
    finally:
        print("\r\x1b[2K", end="", file=sys.stderr, flush=True)


def _emit(
    args: argparse.Namespace,
    command: str,
    report: Report,
    plan: tuple[GroupMove, ...] = (),
    applied: bool | None = None,
    text_extra: list[str] | None = None,
    verdict: ImportVerdict | None = None,
    result: ApplyResult | None = None,
    root: Path | None = None,
) -> None:
    """One output shape for every command.

    JSON envelope, format 1: ``{format, command, applied, root, summary,
    findings, hints, plan, verdict, result}``. ``applied`` is null for
    read-only commands, ``verdict`` is import's safe-to-format decision
    (null elsewhere and on dry runs), ``result`` counts a journaled
    run's groups (undo and resume). The archive root is stated once
    and every path under it is root-relative; paths outside it (card
    files) stay absolute. Text output prints the plan (dry runs), the
    report, then any command-specific lines.

    With ``--json-stream`` the same envelope is the final NDJSON line,
    tagged ``"event": "result"``, after the progress events.
    """
    stream = getattr(args, "json_stream", False)
    if args.json or stream:

        def render(path: Path) -> str:
            if root is not None and path.is_relative_to(root):
                return str(path.relative_to(root))
            return str(path)

        payload: dict[str, object] = {
            **({"event": "result"} if stream else {}),
            "format": 1,
            "command": command,
            "applied": applied,
            "root": str(root) if root is not None else None,
        }
        payload.update(json.loads(report.to_json(root)))
        payload["plan"] = [
            {
                "key": move.key,
                "changes": [[render(r.old), render(r.new)] for r in move.renames],
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
        print(json.dumps(payload, indent=None if stream else 2, ensure_ascii=False))
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


def _run_history_command(args: argparse.Namespace) -> int:
    root: Path | None = None
    if args.root is not None:
        root = args.root
    elif args.config is not None:
        config = load_config(args.config)
        root = Path(config.root) if config.root else None
    summaries = journal_summaries(root=root.resolve() if root else None)
    if args.json:
        payload = {
            "format": 1,
            "command": "history",
            "history": [
                {
                    "path": str(s.path),
                    "root": str(s.root),
                    "kind": s.kind,
                    "command": s.command,
                    "created_at": s.created_at,
                    "groups": s.groups,
                    "status": s.status,
                }
                for s in summaries
            ],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if not summaries:
        print("no journals found")
        return 0
    for s in summaries:
        origin = s.command or s.kind
        print(f"{s.path}  {s.created_at}  {origin}  {s.status}  {s.groups} group(s)  {s.root}")
    return 0


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
    with _progress(args) as monitor:
        result = undo_journal(journal, monitor=monitor)
    report = _journal_result_report(result)
    _emit(
        args,
        "undo",
        report,
        plan=journal.moves,
        applied=True,
        text_extra=[
            f"\nundo {journal_path.name}: {len(result.applied)} group(s) reverted,"
            f" {len(result.skipped)} not applied, {len(result.failed)} failed"
        ],
        result=result,
        root=journal.root.resolve(),
    )
    return 0 if result.ok else 1


def _run_resume_command(args: argparse.Namespace) -> int:
    from stampla.apply import apply_plan

    journal = Journal.load(args.journal)
    _print_journal_header("resume", journal)
    with _progress(args) as monitor:
        result = apply_plan(journal, monitor=monitor)
    report = _journal_result_report(result)
    _emit(
        args,
        "resume",
        report,
        plan=journal.moves,
        applied=True,
        text_extra=[
            f"\nresume {args.journal.name}: {len(result.applied)} group(s) applied,"
            f" {len(result.skipped)} already done, {len(result.failed)} failed"
        ],
        result=result,
        root=journal.root.resolve(),
    )
    return 0 if result.ok else 1


def _print_journal_header(action: str, journal: Journal) -> None:
    print(
        f"{action}: {journal.kind} journal with {len(journal.moves)} group(s) under {journal.root}",
        file=sys.stderr,
    )


def _run_import_command(args: argparse.Namespace) -> int:
    config, root = _config_and_root(args)
    with _progress(args) as monitor:
        plan = build_plan(
            config,
            root.resolve(),
            args.card,
            workers=args.workers,
            monitor=monitor,
            only=tuple(args.paths),
            shoot=args.shoot,
        )
        report = apply_import(plan, root.resolve(), monitor=monitor) if args.apply else plan.report
    verdict = verdict_of(report, applied=args.apply, whole_card=not args.paths)

    extra: list[str] = []
    if not args.apply and plan.moves:
        extra.append(
            f"\ndry run: {len(plan.moves)} group(s) would be imported; pass --apply to copy"
        )
    elif args.apply and args.paths and not report.has_problems:
        extra.append(
            "\nselection imported and verified; the rest of the card was not"
            " examined — run a full import before formatting"
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
        root=root.resolve(),
    )
    return 1 if report.has_problems else 0


def _run_organize_command(args: argparse.Namespace) -> int:
    config, root = _config_and_root(args)
    with _progress(args) as monitor:
        report, plan = run_organize(
            config, root.resolve(), args.path, workers=args.workers, monitor=monitor
        )
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
        root=root.resolve(),
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
    with _progress(args) as monitor:
        report, moves = run_rename(config, root.resolve(), tuple(args.paths), options, monitor)
    extra: list[str] = []
    if not args.apply and moves:
        total = sum(len(m.renames) for m in moves)
        extra.append(f"\ndry run: {total} rename(s) planned; pass --apply to execute")
    _emit(
        args,
        "rename",
        report,
        plan=moves,
        applied=args.apply,
        text_extra=extra,
        root=root.resolve(),
    )
    return 1 if report.has_problems else 0


def _run_inject_command(args: argparse.Namespace) -> int:
    config, root = _config_and_root(args)
    options = InjectOptions(
        apply=args.apply,
        workers=args.workers,
        full=args.full,
        use_manifest=not args.no_manifest,
    )
    with _progress(args) as monitor:
        report = run_inject(config, root.resolve(), tuple(args.paths), options, monitor)
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
    _emit(args, "inject", report, applied=args.apply, text_extra=extra, root=root.resolve())
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
    with _progress(args) as monitor:
        report = run_verify(config, root, args.paths, options, monitor)
    _emit(args, "verify", report, root=root.resolve())
    return 1 if report.has_problems else 0
