"""Unsupported containment must not release a living worker's capacity."""
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db


@pytest.mark.parametrize("alive", [True, False])
def test_windows_containment_is_fail_closed(monkeypatch, alive):
    monkeypatch.setattr(kanban_db, "_IS_WINDOWS", True)
    exists = Mock(return_value=alive)
    monkeypatch.setattr(kanban_db, "_pid_alive", exists)
    for name in ("getpgid", "getsid", "killpg", "kill"):
        monkeypatch.setattr(kanban_db.os, name, Mock(side_effect=AssertionError(name)),
                            raising=False)
    assert kanban_db._contain_spawned_worker(23456) is (not alive)
    exists.assert_called_once_with(23456)


@pytest.mark.parametrize("pid", [None, 0, 1, -1])
def test_invalid_pid_never_probes_or_signals(monkeypatch, pid):
    monkeypatch.setattr(kanban_db, "_pid_alive", Mock(side_effect=AssertionError))
    assert kanban_db._contain_spawned_worker(pid) is True
