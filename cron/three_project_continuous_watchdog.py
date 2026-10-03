"""Inactive, repository-owned reconstruction of the three-project watchdog.

This is not recovered historical source. See docs/three-project-watchdog.md.
Importing this module does not read state or start work; there is no CLI or
scheduler registration. ``run`` requires an explicit home. Only the scheduler
may execute a selected job: the watchdog merely advances its next due time.
"""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile


PROJECTS = {"Ruta": "a2bd90ba81fa", "TripTruth": "05e000a66529", "MedicalBilling": "0a7bc9b82f7a"}
ORIGINS = frozenset(("telegram", "dashboard", "desktop", "codex", "cron", "kanban"))
ACTIVE = frozenset(("queued", "running", "claimed", "pending", "handoff"))
TERMINAL = frozenset(("completed", "done", "failed", "cancelled"))
BLOCKED = frozenset(("paused", "blocked", "needs_owner", "disabled"))
RECENT = timedelta(minutes=15)
SOON = timedelta(minutes=5)
COOLDOWN = timedelta(minutes=15)
_dispatch_context = ContextVar("watchdog_dispatch_context", default=None)
_MISSING = object()


class InvalidEvidence(ValueError):
    """Evidence is unsafe to use for an automatic dispatch decision."""


def _timestamp(value):
    if not isinstance(value, str):
        raise InvalidEvidence("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidEvidence("invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidEvidence("timestamp requires timezone")
    return parsed.astimezone(timezone.utc)


def _now(value):
    value = value if value is not None else datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidEvidence("now requires timezone")
    return value.astimezone(timezone.utc)


def _json(path, *, optional=False):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InvalidEvidence("duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except FileNotFoundError:
        if optional:
            return _MISSING
        raise InvalidEvidence("required evidence missing") from None
    except (OSError, ValueError) as exc:
        raise InvalidEvidence("unreadable evidence") from exc


def _jobs(home):
    raw = _json(home / "cron" / "jobs.json")
    if not isinstance(raw, dict) or not isinstance(raw.get("jobs"), list):
        raise InvalidEvidence("invalid jobs schema")
    result = {}
    for job in raw["jobs"]:
        if not isinstance(job, dict) or not isinstance(job.get("id"), str):
            raise InvalidEvidence("invalid job identity")
        if job["id"] in result:
            raise InvalidEvidence("duplicate job identity")
        result[job["id"]] = job
    return result


def allocation_records(home):
    """Read only declared metadata snapshots; absence means no reported work."""
    records = []
    for filename in ("work-allocations.json", "cross-channel-work.json"):
        raw = _json(Path(home) / "runtime" / filename, optional=True)
        if raw is _MISSING:
            continue
        if isinstance(raw, dict):
            # The adjacent orchestrator stores records in tasks; its allocations
            # dictionary is percentages, not an allocation-record list.
            keys = [key for key in ("tasks", "allocations") if isinstance(raw.get(key), list)]
            if len(keys) != 1:
                raise InvalidEvidence("ambiguous allocation schema")
            raw = raw[keys[0]]
        if not isinstance(raw, list) or any(not isinstance(record, dict) for record in raw):
            raise InvalidEvidence("invalid allocation schema")
        records.extend(raw)
    return records


def external_state(project, writer_id, records):
    active, unrouted = False, False
    for record in records:
        by_project, by_writer = record.get("project"), record.get("writer_id")
        if by_project != project and by_writer != writer_id:
            continue
        if (by_project is not None and by_project != project) or (by_writer is not None and by_writer != writer_id):
            raise InvalidEvidence("conflicting allocation routing")
        if not isinstance(record.get("origin"), str):
            raise InvalidEvidence("invalid allocation origin")
        status, state = record.get("status"), record.get("state")
        if status is not None and state is not None and status != state:
            raise InvalidEvidence("conflicting allocation state")
        status = status if status is not None else state
        if not isinstance(status, str) or status not in ACTIVE | TERMINAL | BLOCKED:
            raise InvalidEvidence("unknown allocation state")
        if not any(isinstance(record.get(key), str) and record[key].strip()
                   for key in ("task_id", "session_id", "job_id", "idempotency_key")):
            raise InvalidEvidence("allocation lacks stable identity")
        if record.get("origin") not in ORIGINS:
            if status not in TERMINAL:
                raise InvalidEvidence("unknown origin may hold active work")
            unrouted = True
            continue
        if status in BLOCKED:
            return "BLOCKED"
        active = active or status in ACTIVE
    return "WORKING_NOW" if active else "UNROUTED" if unrouted else None


def classify(job, now, external):
    now = _now(now)
    if not isinstance(job, dict) or job.get("enabled") is not True:
        return "BLOCKED", "writer unavailable or paused"
    if job.get("state") is not None and (not isinstance(job["state"], str) or job["state"] not in {"scheduled", "running"}):
        return "BLOCKED", "writer unavailable or paused"
    if external == "BLOCKED":
        return "BLOCKED", "external work blocked"
    last = _timestamp(job["last_run_at"]) if job.get("last_run_at") is not None else None
    next_run = _timestamp(job["next_run_at"]) if job.get("next_run_at") is not None else None
    if last is not None and last > now:
        raise InvalidEvidence("last run lies in future")
    if external == "WORKING_NOW":
        return "WORKING_NOW", "routed allocation is active"
    if last is not None and now - last <= RECENT:
        return "RECENTLY_DONE", "recent scheduler completion"
    if next_run is not None and now <= next_run <= now + SOON:
        return "SCHEDULED_SOON", "scheduler already due soon"
    return "STALE_SAFE", "no recent completion or active allocation"


@contextlib.contextmanager
def exclusive_lock(home):
    """Fail closed if a cross-process lock is unavailable or already held."""
    if os.name != "posix":
        raise InvalidEvidence("cross-process watchdog lock requires POSIX")
    import fcntl

    directory = Path(home) / "runtime"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "three-project-watchdog.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _reserve(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".watchdog-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def trigger(writer_id, dry_run):
    """Conditionally schedule one known writer under the scheduler's own lock."""
    if writer_id not in PROJECTS.values():
        raise InvalidEvidence("unknown writer")
    if dry_run:
        return {"writer_id": writer_id, "status": "dry_run"}
    context = _dispatch_context.get()
    if context is None:
        raise InvalidEvidence("explicit run context required")
    home, now = context
    # Lazy import avoids reading active-profile configuration at module import.
    from cron import jobs

    with jobs.use_cron_store(home), jobs._jobs_lock():
        current = _jobs(home).get(writer_id)
        project = next(name for name, identity in PROJECTS.items() if identity == writer_id)
        external = external_state(project, writer_id, allocation_records(home))
        state, _ = classify(current, now, external)
        if state != "STALE_SAFE":
            return {"writer_id": writer_id, "status": "suppressed"}
        # trigger_job clears pause state: the reentrant native store lock must
        # remain held from this eligibility check through its write.
        if jobs.trigger_job(writer_id) is None:
            raise InvalidEvidence("writer disappeared")
    return {"writer_id": writer_id, "status": "scheduled"}


def run(home, now=None, dry_run=False):
    """Decide at most one dispatch; all malformed evidence suppresses dispatch."""
    report = {"status": "ok", "triggered": None, "projects": {}}
    try:
        home, now = Path(home).resolve(), _now(now)
        with exclusive_lock(home) as acquired:
            if not acquired:
                return {"status": "overlap", "triggered": None, "projects": {}}
            all_jobs, records = _jobs(home), allocation_records(home)
            candidates = []
            for project, writer_id in PROJECTS.items():
                job = all_jobs.get(writer_id)
                external = external_state(project, writer_id, records)
                state, reason = classify(job, now, external)
                report["projects"][project] = {"state": state, "reason": reason, "external_state": external}
                if state == "STALE_SAFE":
                    last = _timestamp(job["last_run_at"]) if job.get("last_run_at") is not None else datetime.min.replace(tzinfo=timezone.utc)
                    candidates.append((last, list(PROJECTS).index(project), project, writer_id))
            reservation_path = home / "runtime" / "three-project-watchdog.json"
            reservation = _json(reservation_path, optional=True)
            reservations = {}
            if reservation is not _MISSING:
                if not isinstance(reservation, dict) or not isinstance(reservation.get("reservations"), dict):
                    raise InvalidEvidence("invalid dispatch reservation")
                reservations = reservation["reservations"]
                cooling = set()
                for identity, reserved_at in reservations.items():
                    if identity not in PROJECTS.values():
                        raise InvalidEvidence("invalid reserved writer")
                    last_dispatch = _timestamp(reserved_at)
                    if last_dispatch > now or now - last_dispatch <= COOLDOWN:
                        cooling.add(identity)
                original_count = len(candidates)
                candidates = [candidate for candidate in candidates if candidate[3] not in cooling]
                if original_count and not candidates:
                    report["status"] = "cooldown"
            if not candidates:
                return report
            _, _, project, writer_id = min(candidates)
            if not dry_run:
                # Reserve before side effects: an ambiguous failure cannot
                # produce another dispatch on the following watchdog tick.
                reservations[writer_id] = now.isoformat()
                _reserve(reservation_path, {"reservations": reservations})
            token = _dispatch_context.set((home, now))
            try:
                try:
                    result = trigger(writer_id, dry_run)
                except Exception:
                    report["status"] = "uncertain"
                    report["reason"] = "dispatch failed; reservation retained"
                    return report
            finally:
                _dispatch_context.reset(token)
            if not isinstance(result, dict) or result.get("writer_id") != writer_id or result.get("status") not in (None, "dry_run", "scheduled", "suppressed"):
                report["status"] = "uncertain"
                report["reason"] = "unconfirmed dispatch; reservation retained"
            elif result.get("status") == "suppressed":
                report["status"] = "suppressed"
            else:
                report["triggered"] = result
                report["projects"][project]["triggered"] = True
            return report
    except (InvalidEvidence, OSError, ImportError):
        # Never include raw input or exception bodies in scheduler reports.
        report["status"] = "blocked"
        report["reason"] = "invalid evidence or unavailable dispatch storage"
        return report
