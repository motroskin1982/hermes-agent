"""Cross-process admission locks for cron jobs that mutate a worktree."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from hermes_constants import get_hermes_home

try:
    import fcntl
except ImportError:  # pragma: no cover - cron worktree admission is POSIX-only today
    fcntl = None  # type: ignore[assignment]


def normalize_worktree(worktree: str | os.PathLike[str]) -> str:
    return os.path.normcase(str(Path(worktree).expanduser().resolve(strict=False)))


@dataclass
class WorktreeAdmission:
    normalized_worktree: str
    lock_path: Path
    _handle: IO[str]
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()

    def __enter__(self) -> "WorktreeAdmission":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def try_acquire_worktree(
    worktree: str | os.PathLike[str], *, lock_root: Path | None = None
) -> WorktreeAdmission | None:
    """Acquire a non-blocking per-worktree lock, or return None when busy."""
    if fcntl is None:
        raise RuntimeError("cron worktree admission locking is unavailable")
    normalized = normalize_worktree(worktree)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    root = lock_root or (get_hermes_home() / "runtime" / "cron-worktree-locks")
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / f"{digest}.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        handle.seek(0)
        handle.truncate()
        json.dump({"normalized_worktree": normalized, "pid": os.getpid()}, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        return WorktreeAdmission(normalized, lock_path, handle)
    except BaseException:
        if not handle.closed:
            handle.close()
        raise