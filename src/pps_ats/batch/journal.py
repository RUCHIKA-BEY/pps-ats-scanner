"""Append-only processing manifest (JSONL). The latest line per key is the current state.

Each line is flushed and fsynced before the next document starts, so an interrupted run
loses at most the document in progress (left as ``processing`` and re-run next time). A
torn final line from a crash is ignored when the journal is read back.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import ValidationError

from pps_ats.schemas.batch import SubmissionRecord


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.latest: dict[str, SubmissionRecord] = {}
        self.unreadable_lines = 0
        if path.exists():
            with path.open(encoding="utf-8") as handle:
                for raw in handle:
                    if not raw.strip():
                        continue
                    try:
                        record = SubmissionRecord.model_validate_json(raw)
                    except (ValidationError, ValueError):
                        self.unreadable_lines += 1
                        continue
                    self.latest[record.key] = record

    def append(self, record: SubmissionRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.latest[record.key] = record

    def compact(self) -> None:
        """Rewrite the journal with one line per key (atomic replace)."""
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for record in self.latest.values():
                handle.write(record.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)


def write_json_atomic(path: Path, payload: str | dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
