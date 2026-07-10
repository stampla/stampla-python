"""Progress reporting and cooperative cancellation for long operations.

Verifying or importing a large archive is minutes of hashing and
metadata reading; a caller (the CLI on a terminal, a GUI, a script)
needs to see it moving and be able to stop it. Both travel in one
object: a :class:`Monitor` carries an optional event callback and an
optional should-cancel probe through an operation.

Events are coarse where work is batched (date resolution) and per-item
where it is long (hashing, copying, renaming). ``total`` is ``0`` when
the amount of work is not known up front (scanning). Counters restart
per phase and per batch — events describe motion, not an overall
percentage.

Cancellation is cooperative and always lands at a safe point: between
files while planning, between groups while applying. An interrupted
apply is exactly the journal's interruption case — finish it with the
resume command, or revert it with undo.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: phases a monitor may see, in the order operations usually run them
PHASES = ("scan", "dates", "hash", "rename", "copy", "verify-copies", "tokens", "undo")


class Cancelled(Exception):
    """The monitor asked the running operation to stop."""


@dataclass(frozen=True)
class ProgressEvent:
    phase: str
    done: int
    #: 0 means the total is not known up front
    total: int
    path: Path | None = None


@dataclass(frozen=True)
class Monitor:
    """A progress callback and a cancellation probe; both optional.

    The default ``Monitor()`` is inert, so operations take
    ``monitor: Monitor | None = None`` and behave identically without
    one. The callback runs on the operation's thread — a GUI hands the
    event over to its own loop; it must not block.
    """

    callback: Callable[[ProgressEvent], None] | None = None
    should_cancel: Callable[[], bool] | None = None

    def emit(self, phase: str, done: int, total: int, path: Path | None = None) -> None:
        if self.callback is not None:
            self.callback(ProgressEvent(phase, done, total, path))

    def check(self) -> None:
        """Raise :class:`Cancelled` if the caller asked to stop."""
        if self.should_cancel is not None and self.should_cancel():
            raise Cancelled("operation cancelled")

    def step(self, phase: str, done: int, total: int, path: Path | None = None) -> None:
        """One unit of work finished: check for cancellation, then emit."""
        self.check()
        self.emit(phase, done, total, path)
