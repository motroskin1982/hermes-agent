from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from cron.worktree_admission import normalize_worktree, try_acquire_worktree


def _hold(worktree: str, lock_root: str, ready, release) -> None:
    lock = try_acquire_worktree(worktree, lock_root=Path(lock_root))
    assert lock is not None
    ready.set()
    release.wait(10)
    lock.release()


def test_same_normalized_worktree_is_busy_then_released(tmp_path):
    worktree = tmp_path / "project"
    worktree.mkdir()
    lock_root = tmp_path / "locks"
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_hold, args=(str(worktree), str(lock_root), ready, release))
    process.start()
    try:
        assert ready.wait(10)
        alias = worktree / "child" / ".."
        assert normalize_worktree(alias) == normalize_worktree(worktree)
        assert try_acquire_worktree(alias, lock_root=lock_root) is None
        unrelated = tmp_path / "other"
        unrelated.mkdir()
        other_lock = try_acquire_worktree(unrelated, lock_root=lock_root)
        assert other_lock is not None
        other_lock.release()
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0
    reacquired = try_acquire_worktree(worktree, lock_root=lock_root)
    assert reacquired is not None
    reacquired.release()


@pytest.mark.parametrize("failure", [RuntimeError("error"), KeyboardInterrupt()])
def test_context_manager_releases_on_error_and_cancel(tmp_path, failure):
    worktree = tmp_path / "project"
    worktree.mkdir()
    lock_root = tmp_path / "locks"
    with pytest.raises(type(failure)):
        lock = try_acquire_worktree(worktree, lock_root=lock_root)
        assert lock is not None
        with lock:
            raise failure
    reacquired = try_acquire_worktree(worktree, lock_root=lock_root)
    assert reacquired is not None
    reacquired.release()