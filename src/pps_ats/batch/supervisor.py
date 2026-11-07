"""Supervised worker processes with a per-document timeout (Milestone 5).

Each worker is a separate process that handles one document at a time and talks to the
main process over a pipe. The main process alone writes the journal, so killing a stuck
worker can never corrupt it.

The timeout counts *active* time only: the supervisor polls every ``poll_s`` seconds and,
if the wall-clock gap between two polls is much longer than that, the computer was
asleep (e.g. Windows Modern Standby, which caused the Milestone 4 stalls) and the gap is
not charged to the document. Before a worker is killed, Python's ``faulthandler`` writes
the stuck stack (code locations only, no document text) to ``diagnostics/``.
"""

from __future__ import annotations

import contextlib
import faulthandler
import importlib
import logging
import multiprocessing as mp
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from multiprocessing.connection import Connection, wait
from pathlib import Path
from typing import Any

from pps_ats.batch.worker import Job, Outcome
from pps_ats.config import Settings

log = logging.getLogger(__name__)

DEFAULT_TARGET = "pps_ats.batch.worker:process"
DIAGNOSTICS_DIR = "diagnostics"


def resolve_target(target: str) -> Callable[..., Outcome]:
    module, _, name = target.partition(":")
    return getattr(importlib.import_module(module), name)


def _child_main(
    conn: Connection,
    target: str,
    settings_json: str,
    manifest: Any,
    diag_dir: str | None,
    dump_after_s: float,
) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings.model_validate_json(settings_json)
    process = resolve_target(target)
    conn.send(("ready", None))  # handshake: the supervisor waits for this before sending jobs
    while True:
        try:
            job = conn.recv()
        except (EOFError, OSError, KeyboardInterrupt):
            return
        if job is None:
            return
        handle = None
        if diag_dir and dump_after_s > 0:
            path = Path(diag_dir) / f"stall_{job.key[-10:]}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("w", encoding="utf-8")
            faulthandler.dump_traceback_later(dump_after_s, repeat=False, file=handle)
        try:
            outcome = process(job, settings, manifest)
        finally:
            if handle is not None:
                faulthandler.cancel_dump_traceback_later()
                handle.close()
                if path.stat().st_size == 0:
                    path.unlink()
        try:
            conn.send(outcome)
        except (OSError, KeyboardInterrupt):
            return


@dataclass
class _Worker:
    proc: Any
    conn: Connection
    job: Job | None = None
    active_s: float = 0.0
    last_tick: float = 0.0
    suspended_s: float = 0.0


@dataclass
class SupervisorStats:
    timeouts: int = 0
    crashes: int = 0
    restarts: int = 0
    suspended_s: float = 0.0  # wall-clock time not charged to documents (sleep/standby)
    suspensions: int = 0
    events: list[str] = field(default_factory=list)


class WorkerStartError(RuntimeError):
    pass


class Supervisor:
    def __init__(
        self,
        workers: int,
        settings: Settings,
        manifest: Any,
        timeout_s: float,
        output_dir: Path,
        target: str = DEFAULT_TARGET,
        poll_s: float = 0.5,
        suspend_gap_s: float = 15.0,
        startup_timeout_s: float = 120.0,
    ) -> None:
        self.n = max(1, workers)
        self.ctx = mp.get_context("spawn")  # same behaviour on Windows, Linux and macOS
        self.settings_json = settings.model_dump_json()
        self.manifest = manifest
        self.timeout_s = timeout_s
        self.out = output_dir
        self.target = target
        self.poll_s = poll_s
        self.suspend_gap_s = suspend_gap_s
        self.startup_timeout_s = startup_timeout_s
        self.stats = SupervisorStats()
        self.pool: list[_Worker] = []

    # ------------------------------------------------------------------ lifecycle
    def _spawn(self) -> _Worker:
        parent, child = self.ctx.Pipe()
        dump_after = self.timeout_s * 0.9 if self.timeout_s > 0 else 0.0
        proc = self.ctx.Process(
            target=_child_main,
            args=(
                child,
                self.target,
                self.settings_json,
                self.manifest,
                str(self.out / DIAGNOSTICS_DIR),
                dump_after,
            ),
            daemon=True,
        )
        proc.start()
        child.close()
        worker = _Worker(proc=proc, conn=parent)
        ready = None
        try:
            # wait for the handshake or the process exiting, whichever comes first
            if parent in wait([parent, proc.sentinel], self.startup_timeout_s):
                ready = parent.recv()
        except (EOFError, OSError):
            ready = None
        if not (isinstance(ready, tuple) and ready[0] == "ready"):
            self._kill(worker)
            raise WorkerStartError(
                f"a worker process failed to start (exit code {proc.exitcode}); "
                "run the batch from the `python -m pps_ats` command line"
            )
        return worker

    def _kill(self, w: _Worker) -> None:
        with contextlib.suppress(OSError):
            w.conn.close()
        if w.proc.is_alive():
            w.proc.terminate()
            w.proc.join(5)
            if w.proc.is_alive():
                w.proc.kill()
                w.proc.join(5)

    def _replace(self, index: int) -> None:
        self._kill(self.pool[index])
        self.pool[index] = self._spawn()
        self.stats.restarts += 1

    def close(self) -> None:
        for w in self.pool:
            try:
                if w.proc.is_alive() and w.job is None:
                    w.conn.send(None)
            except OSError:
                pass
        for w in self.pool:
            w.proc.join(2)
            self._kill(w)
        self.pool = []

    # ------------------------------------------------------------------ main loop
    def run(
        self,
        jobs: Iterator[Any],
        start: Callable[[Any], Job],
        finish: Callable[[Outcome], None],
    ) -> SupervisorStats:
        """``start(item) -> Job`` is called just before a job is sent; ``finish`` once for
        every started job (done, failed, crashed or timed out)."""
        _check_spawnable()
        self.pool = [self._spawn() for _ in range(self.n)]
        exhausted = False
        try:
            while True:
                for w in self.pool:
                    if w.job is None and not exhausted:
                        item = next(jobs, None)
                        if item is None:
                            exhausted = True
                            break
                        w.job = start(item)
                        w.active_s, w.last_tick = 0.0, time.monotonic()
                        w.conn.send(w.job)
                busy = [w for w in self.pool if w.job is not None]
                if not busy:
                    return self.stats
                ready = wait([w.conn for w in busy] + [w.proc.sentinel for w in busy], self.poll_s)
                now = time.monotonic()
                for index, w in enumerate(self.pool):
                    if w.job is None:
                        continue
                    if w.conn in ready:
                        try:
                            outcome = w.conn.recv()
                        except (EOFError, OSError):
                            outcome = None
                        if outcome is not None:
                            w.job = None
                            finish(outcome)
                            continue
                    if w.proc.sentinel in ready or not w.proc.is_alive():
                        job, w.job = w.job, None
                        self.stats.crashes += 1
                        self.stats.events.append(f"worker crashed doc={job.key[-10:]}")
                        _drop_empty_diag(self.out, job.key)
                        finish(
                            Outcome(
                                key=job.key,
                                ok=False,
                                error_type="WorkerCrashed",
                                error_message=f"worker process exited with code "
                                f"{w.proc.exitcode} while processing this document",
                                duration_ms=int(w.active_s * 1000),
                            )
                        )
                        self._replace(index)
                        continue
                    gap = now - w.last_tick
                    w.last_tick = now
                    if gap > self.suspend_gap_s:  # the computer was asleep: do not charge it
                        self.stats.suspensions += 1
                        self.stats.suspended_s += gap
                        self.stats.events.append(
                            f"suspension of {gap:.0f}s ignored for doc={w.job.key[-10:]}"
                        )
                        gap = self.poll_s
                    w.active_s += gap
                    if self.timeout_s > 0 and w.active_s > self.timeout_s:
                        job, w.job = w.job, None
                        self.stats.timeouts += 1
                        self.stats.events.append(
                            f"timeout after {w.active_s:.0f}s active doc={job.key[-10:]}"
                        )
                        self._replace(index)  # terminate the stuck worker first
                        finish(
                            Outcome(
                                key=job.key,
                                ok=False,
                                timed_out=True,
                                error_type="DocumentTimeout",
                                error_message=f"no result after {self.timeout_s:g}s of "
                                "processing; the worker was stopped",
                                duration_ms=int(w.active_s * 1000),
                                diagnostics=_diag_file(self.out, job.key),
                            )
                        )
        finally:
            self.close()


def _check_spawnable() -> None:
    """Spawned workers re-import the caller's ``__main__``. If it cannot be re-imported
    (a script piped through stdin), CPython on Windows blocks inside ``Process.start``
    instead of failing, so refuse up front."""
    main_file = getattr(sys.modules.get("__main__"), "__file__", None)
    if main_file and not Path(main_file).exists():
        raise WorkerStartError(
            f"worker processes cannot re-import the main script {main_file!r}; run "
            "`python -m pps_ats batch ...` or set [batch] document_timeout_s = 0"
        )


def _drop_empty_diag(out: Path, key: str) -> None:
    path = out / DIAGNOSTICS_DIR / f"stall_{key[-10:]}.txt"
    try:
        if path.is_file() and path.stat().st_size == 0:
            path.unlink()
    except OSError:
        pass


def _diag_file(out: Path, key: str) -> str | None:
    rel = f"{DIAGNOSTICS_DIR}/stall_{key[-10:]}.txt"
    return rel if (out / rel).is_file() else None
