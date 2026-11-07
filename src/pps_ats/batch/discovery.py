"""Find every submission under an input folder. Nothing is skipped silently: OS/lock files
and links are returned separately so the summary can list them."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from pps_ats.config import BatchSettings

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Submission:
    path: Path
    rel_path: str  # POSIX-style, relative to the input folder
    key: str
    size_bytes: int
    mtime_ns: int

    @property
    def file_name(self) -> str:
        return self.path.name

    @property
    def short_id(self) -> str:
        """Log-safe identifier (no file name, which may contain a student's name)."""
        return self.key.rsplit("__", 1)[-1]


def make_key(rel_path: str) -> str:
    """Unique, filesystem-safe and stable: readable stem + hash of the relative path, so equal
    file names in different folders never collide."""
    digest = hashlib.sha1(rel_path.casefold().encode("utf-8")).hexdigest()[:10]
    stem = _UNSAFE.sub("_", Path(rel_path).stem).strip("._")[:60] or "file"
    return f"{stem}__{digest}"


def discover(input_dir: Path, cfg: BatchSettings) -> tuple[list[Submission], list[str]]:
    """Return (submissions, ignored relative paths with reasons), sorted by path."""
    root = input_dir.resolve()
    found: list[Submission] = []
    ignored: list[str] = []
    names = {n.casefold() for n in cfg.ignore_names}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if name.casefold() in names or any(name.startswith(p) for p in cfg.ignore_prefixes):
                ignored.append(f"{rel} (system/lock file)")
                continue
            if path.is_symlink():
                ignored.append(f"{rel} (symbolic link not followed)")
                continue
            stat = path.stat()
            found.append(Submission(path, rel, make_key(rel), stat.st_size, stat.st_mtime_ns))
    found.sort(key=lambda s: s.rel_path.casefold())
    return found, ignored
