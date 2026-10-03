"""Adversarial reconstruction checks. All scheduler writes use tmp_path."""
import importlib.util
import json
import multiprocessing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cron import three_project_continuous_watchdog as watchdog


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def home(tmp_path):
    (tmp_path / "cron").mkdir()
    (tmp_path / "runtime").mkdir()
    jobs = [{"id": identity, "name": name, "enabled": True, "state": "scheduled",
             "schedule": {"kind": "interval", "minutes": 60},
             "last_run_at": None, "next_run_at": None}
            for name, identity in watchdog.PROJECTS.items()]
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}))
    return tmp_path


def jobs_at(home):
    return json.loads((home / "cron" / "jobs.json").read_text())["jobs"]


def write_jobs(home, jobs):
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}))


def test_native_trigger_scopes_store_and_only_advances_one_job(home, tmp_path, monkeypatch):
    from cron import jobs

    unrelated = tmp_path / "unrelated-profile"
    monkeypatch.setenv("HERMES_HOME", str(unrelated))
    monkeypatch.setattr(jobs, "_hermes_now", lambda: NOW)
    before = jobs_at(home)
    report = watchdog.run(home, now=NOW)
    after = jobs_at(home)
    assert report["triggered"] == {"writer_id": watchdog.PROJECTS["Ruta"], "status": "scheduled"}
    assert after[0]["next_run_at"] == NOW.isoformat()
    assert after[1:] == before[1:]
    assert not unrelated.exists()


def test_pause_between_selection_and_dispatch_is_preserved(home, monkeypatch):
    from cron import jobs

    actual = watchdog.trigger

    def pause_then_trigger(identity, dry_run):
        with jobs.use_cron_store(home):
            jobs.pause_job(identity, "owner stop")
        return actual(identity, dry_run)

    monkeypatch.setattr(watchdog, "trigger", pause_then_trigger)
    report = watchdog.run(home, now=NOW)
    assert report["status"] == "suppressed"
    assert report["triggered"] is None
    assert jobs_at(home)[0]["enabled"] is False
    assert jobs_at(home)[0]["paused_reason"] == "owner stop"


def test_native_lock_held_through_recheck_and_schedule(home, monkeypatch):
    from cron import jobs

    actual = jobs.trigger_job
    seen = []

    def check_lock(identity):
        seen.append(jobs._jobs_lock_state.depth)
        return actual(identity)

    monkeypatch.setattr(jobs, "trigger_job", check_lock)
    assert watchdog.run(home, now=NOW)["triggered"]
    assert seen and all(depth >= 1 for depth in seen)


def test_dry_run_preserves_jobs_and_does_not_consume_reservation(home):
    before = (home / "cron" / "jobs.json").read_bytes()
    for _ in range(2):
        assert watchdog.run(home, now=NOW, dry_run=True)["triggered"]["status"] == "dry_run"
    assert (home / "cron" / "jobs.json").read_bytes() == before
    assert not (home / "runtime" / "three-project-watchdog.json").exists()


@pytest.mark.parametrize("filename,contents", [
    ("cron/jobs.json", "{"),
    ("cron/jobs.json", "null"),
    ("cron/jobs.json", '{"jobs":{}}'),
    ("cron/jobs.json", '{"jobs":[],"jobs":[]}'),
    ("runtime/work-allocations.json", "{"),
    ("runtime/work-allocations.json", "null"),
    ("runtime/work-allocations.json", '{"allocations":{},"tasks":"bad"}'),
    ("runtime/work-allocations.json", '{"allocations":[],"tasks":[]}'),
    ("runtime/cross-channel-work.json", '["bad"]'),
    ("runtime/three-project-watchdog.json", "null"),
    ("runtime/three-project-watchdog.json", "{}"),
])
def test_malformed_evidence_fails_closed(home, monkeypatch, filename, contents):
    (home / filename).write_text(contents)
    monkeypatch.setattr(watchdog, "trigger", pytest.fail)
    assert watchdog.run(home, now=NOW)["status"] == "blocked"


def test_duplicate_writer_ids_fail_closed(home, monkeypatch):
    jobs = jobs_at(home)
    jobs.append(dict(jobs[0]))
    write_jobs(home, jobs)
    monkeypatch.setattr(watchdog, "trigger", pytest.fail)
    assert watchdog.run(home, now=NOW)["status"] == "blocked"


@pytest.mark.parametrize("record", [
    {"project": "Ruta", "writer_id": watchdog.PROJECTS["TripTruth"], "origin": "codex", "task_id": "x", "state": "running"},
    {"project": "Ruta", "origin": "codex", "state": "running"},
    {"project": "Ruta", "origin": "codex", "task_id": "x", "state": "done", "status": "running"},
    {"project": "Ruta", "origin": [], "task_id": "x", "state": "running"},
    {"project": "Ruta", "origin": "codex", "task_id": "x", "state": []},
    {"project": "Ruta", "origin": "future_allocator", "task_id": "x", "state": "running"},
])
def test_ambiguous_allocation_fails_closed(home, monkeypatch, record):
    (home / "runtime" / "cross-channel-work.json").write_text(json.dumps([record]))
    monkeypatch.setattr(watchdog, "trigger", pytest.fail)
    assert watchdog.run(home, now=NOW)["status"] == "blocked"


@pytest.mark.parametrize("field,value", [
    ("last_run_at", (NOW + timedelta(seconds=1)).isoformat()),
    ("last_run_at", "2026-01-01T00:00:00"),
    ("last_run_at", "not a date"),
    ("next_run_at", "2026-01-01T00:01:00"),
    ("next_run_at", {}),
])
def test_invalid_temporal_evidence_fails_closed(home, monkeypatch, field, value):
    jobs = jobs_at(home)
    jobs[0][field] = value
    write_jobs(home, jobs)
    monkeypatch.setattr(watchdog, "trigger", pytest.fail)
    assert watchdog.run(home, now=NOW)["status"] == "blocked"


def test_naive_clock_fails_closed(home, monkeypatch):
    monkeypatch.setattr(watchdog, "trigger", pytest.fail)
    assert watchdog.run(home, now=NOW.replace(tzinfo=None))["status"] == "blocked"


def _hold_lock(home, ready, release):
    with watchdog.exclusive_lock(home) as acquired:
        ready.put(acquired)
        release.wait(10)


def test_real_cross_process_overlap(home, monkeypatch):
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Queue(), ctx.Event()
    process = ctx.Process(target=_hold_lock, args=(home, ready, release))
    process.start()
    try:
        assert ready.get(timeout=10) is True
        monkeypatch.setattr(watchdog, "trigger", pytest.fail)
        assert watchdog.run(home, now=NOW) == {"status": "overlap", "triggered": None, "projects": {}}
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert process.exitcode == 0


def test_lock_failure_cannot_dispatch(home, monkeypatch):
    def fail(_home):
        raise OSError("private path must not be reported")

    monkeypatch.setattr(watchdog, "exclusive_lock", fail)
    monkeypatch.setattr(watchdog, "trigger", pytest.fail)
    report = watchdog.run(home, now=NOW)
    assert report["status"] == "blocked"
    assert "private path" not in str(report)


def test_unsupported_platform_lock_fails_closed(home, monkeypatch):
    monkeypatch.setattr(watchdog.os, "name", "nt")
    with pytest.raises(watchdog.InvalidEvidence, match="requires POSIX"):
        with watchdog.exclusive_lock(home):
            pytest.fail("unsupported lock acquired")


def test_reservation_failure_cannot_dispatch(home, monkeypatch):
    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(watchdog, "_reserve", fail)
    monkeypatch.setattr(watchdog, "trigger", pytest.fail)
    assert watchdog.run(home, now=NOW)["status"] == "blocked"


def test_uncertain_dispatch_persists_cooldown_across_module_reload(home, monkeypatch):
    jobs = jobs_at(home)
    for job in jobs[1:]:
        job["last_run_at"] = NOW.isoformat()
    write_jobs(home, jobs)
    calls = []

    def fail(identity, dry_run):
        calls.append(identity)
        raise RuntimeError("private failure detail")

    monkeypatch.setattr(watchdog, "trigger", fail)
    report = watchdog.run(home, now=NOW)
    assert report["status"] == "uncertain"
    assert "private failure detail" not in str(report)
    spec = importlib.util.spec_from_file_location("fresh_watchdog", watchdog.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    monkeypatch.setattr(fresh, "trigger", pytest.fail)
    assert fresh.run(home, now=NOW + timedelta(minutes=1))["status"] == "cooldown"
    assert len(calls) == 1


def test_clock_rollback_preserves_cooldown(home, monkeypatch):
    jobs = jobs_at(home)
    for job in jobs[1:]:
        job["enabled"] = False
    write_jobs(home, jobs)
    monkeypatch.setattr(watchdog, "trigger", lambda *_: {"status": "scheduled"})
    watchdog.run(home, now=NOW)
    monkeypatch.setattr(watchdog, "trigger", pytest.fail)
    assert watchdog.run(home, now=NOW - timedelta(seconds=1))["status"] == "cooldown"


def test_cooldown_does_not_block_other_projects(home, monkeypatch):
    calls = []
    monkeypatch.setattr(watchdog, "trigger", lambda identity, dry_run: calls.append(identity) or {"writer_id": identity})
    watchdog.run(home, now=NOW)
    watchdog.run(home, now=NOW + timedelta(minutes=1))
    assert calls == [watchdog.PROJECTS["Ruta"], watchdog.PROJECTS["TripTruth"]]


@pytest.mark.parametrize("result", [None, {}, [], {"status": "failed"}, {"writer_id": watchdog.PROJECTS["Ruta"], "status": "failed"}, {"writer_id": watchdog.PROJECTS["Ruta"], "status": []}])
def test_unconfirmed_trigger_never_claims_success(home, monkeypatch, result):
    monkeypatch.setattr(watchdog, "trigger", lambda *_: result)
    report = watchdog.run(home, now=NOW)
    assert report["status"] == "uncertain"
    assert report["triggered"] is None
    assert (home / "runtime" / "three-project-watchdog.json").exists()


def test_import_does_not_read_or_write_state(monkeypatch):
    def fail(*_args, **_kwargs):
        pytest.fail("state I/O at module import")

    monkeypatch.setattr(Path, "read_text", fail)
    monkeypatch.setattr(Path, "open", fail)
    monkeypatch.setattr(Path, "mkdir", fail)
    spec = importlib.util.spec_from_file_location("inert_watchdog", watchdog.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)


def test_trigger_requires_explicit_run_context():
    with pytest.raises(watchdog.InvalidEvidence, match="explicit run context"):
        watchdog.trigger(watchdog.PROJECTS["Ruta"], False)
