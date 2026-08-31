"""Contract tests for the owner-approved three-project no-agent watchdog."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path("/srv/hermes/state/scripts/hermes_three_project_continuous_watchdog.py")


@pytest.fixture
def watchdog():
    spec = importlib.util.spec_from_file_location("three_project_watchdog_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path):
    (tmp_path / "cron").mkdir()
    (tmp_path / "runtime").mkdir()
    return tmp_path


def stamp(dt):
    return dt.isoformat().replace("+00:00", "Z")


def write_jobs(home, now, **fields):
    jobs = []
    for name, writer_id in {"Ruta": "a2bd90ba81fa", "TripTruth": "05e000a66529", "MedicalBilling": "0a7bc9b82f7a"}.items():
        jobs.append({"id": writer_id, "name": name, "enabled": True, **fields.get(name, {})})
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}))


def test_dry_run_oldest_stale_only_triggers_one(watchdog, home, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    write_jobs(home, now,
        Ruta={"last_run_at": stamp(now - timedelta(minutes=30))},
        TripTruth={"last_run_at": stamp(now - timedelta(minutes=60))},
        MedicalBilling={"last_run_at": stamp(now - timedelta(minutes=20))},
    )
    called = []
    monkeypatch.setattr(watchdog, "trigger", lambda writer_id, dry_run: called.append((writer_id, dry_run)) or {"writer_id": writer_id})
    report = watchdog.run(home, now=now, dry_run=True)
    assert called == [("05e000a66529", True)]
    assert report["triggered"]["writer_id"] == "05e000a66529"
    assert sum(bool(v.get("triggered")) for v in report["projects"].values()) == 1


def test_recent_and_scheduled_soon_are_not_working_or_triggered(watchdog, home, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    write_jobs(home, now,
        Ruta={"last_run_at": stamp(now - timedelta(minutes=5))},
        TripTruth={"next_run_at": stamp(now + timedelta(minutes=5))},
        MedicalBilling={"state": "running", "last_run_at": stamp(now - timedelta(minutes=3))},
    )
    monkeypatch.setattr(watchdog, "trigger", pytest.fail)
    report = watchdog.run(home, now=now)
    assert [v["state"] for v in report["projects"].values()] == ["RECENTLY_DONE", "SCHEDULED_SOON", "RECENTLY_DONE"]
    assert report["triggered"] is None


def test_blocked_writer_is_never_triggered(watchdog, home, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    write_jobs(home, now, Ruta={"enabled": False}, TripTruth={"state": "paused"})
    calls = []
    monkeypatch.setattr(watchdog, "trigger", lambda writer_id, dry_run: calls.append(writer_id) or {})
    report = watchdog.run(home, now=now)
    assert report["projects"]["Ruta"]["state"] == "BLOCKED"
    assert report["projects"]["TripTruth"]["state"] == "BLOCKED"
    assert calls == ["0a7bc9b82f7a"]


def test_overlap_is_idempotent_and_does_not_trigger(watchdog, home, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    write_jobs(home, now)
    monkeypatch.setattr(watchdog, "exclusive_lock", lambda _home: __import__("contextlib").nullcontext(False))
    monkeypatch.setattr(watchdog, "trigger", pytest.fail)
    assert watchdog.run(home, now=now) == {"status": "overlap", "triggered": None, "projects": {}}


def test_idempotency_records_a_trigger_and_suppresses_the_next_tick(watchdog, home, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    write_jobs(home, now,
        TripTruth={"last_run_at": stamp(now - timedelta(minutes=2))},
        MedicalBilling={"last_run_at": stamp(now - timedelta(minutes=2))},
    )
    calls = []
    monkeypatch.setattr(watchdog, "trigger", lambda writer_id, dry_run: calls.append(writer_id) or {"writer_id": writer_id})
    watchdog.run(home, now=now)
    watchdog.run(home, now=now + timedelta(minutes=1))
    assert calls == ["a2bd90ba81fa"]  # only one writer is ever triggered per tick/run window


def test_stale_safe_classification(watchdog):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state, _ = watchdog.classify({"enabled": True, "last_run_at": stamp(now - timedelta(minutes=16))}, now, None)
    assert state == "STALE_SAFE"


def test_finished_session_and_dev_artifacts_never_count_as_working_now(watchdog):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    job = {"enabled": True, "state": "running", "last_run_at": stamp(now - timedelta(minutes=20)),
           "dev_server": "running", "lsp": "ready", "preview": "ready", "card": "ready"}
    state, _ = watchdog.classify(job, now, None)
    assert state == "STALE_SAFE"


def test_telegram_queued_suppresses_duplicate_writer(watchdog, home, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    write_jobs(home, now)
    (home / "runtime" / "work-allocations.json").write_text(json.dumps({"allocations": [
        {"project": "Ruta", "origin": "telegram", "session_id": "tg-42", "status": "queued"}
    ]}))
    calls = []
    monkeypatch.setattr(watchdog, "trigger", lambda writer_id, dry_run: calls.append(writer_id) or {})
    report = watchdog.run(home, now=now)
    assert report["projects"]["Ruta"]["state"] == "WORKING_NOW"
    assert "a2bd90ba81fa" not in calls


def test_cross_channel_codex_and_kanban_prevent_duplicate(watchdog, home, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    write_jobs(home, now, Ruta={"last_run_at": stamp(now - timedelta(minutes=2))})
    (home / "runtime" / "cross-channel-work.json").write_text(json.dumps([
        {"writer_id": "05e000a66529", "origin": "codex", "task_id": "handoff-1", "state": "running"},
        {"writer_id": "0a7bc9b82f7a", "origin": "kanban", "task_id": "card-7", "state": "claimed"},
    ]))
    monkeypatch.setattr(watchdog, "trigger", pytest.fail)
    report = watchdog.run(home, now=now)
    assert {report["projects"][name]["state"] for name in ("TripTruth", "MedicalBilling")} == {"WORKING_NOW"}


def test_unrouted_work_is_exposed_but_not_treated_as_active(watchdog, home):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    write_jobs(home, now)
    (home / "runtime" / "work-allocations.json").write_text(json.dumps([
        {"project": "Ruta", "origin": "future_allocator", "task_id": "x", "state": "done"}
    ]))
    report = watchdog.run(home, now=now, dry_run=True)
    assert watchdog.external_state("Ruta", "a2bd90ba81fa", watchdog.allocation_records(home)) == "UNROUTED"
    assert report["projects"]["Ruta"]["state"] == "STALE_SAFE"
