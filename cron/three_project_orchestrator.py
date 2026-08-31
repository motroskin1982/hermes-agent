"""Metadata-only three-project Control Room allocation and accounting contract.

This module deliberately does not start work.  Producers ingest authoritative
IDs from their own registries; the watchdog reads the resulting JSON snapshot.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PROJECTS = ("Ruta", "TripTruth", "MedicalBilling")
ORIGINS = frozenset(("telegram", "dashboard", "desktop", "codex", "cron", "kanban"))
ACTIVE = frozenset(("queued", "running", "claimed", "pending", "handoff"))
TERMINAL = frozenset(("completed", "failed", "cancelled"))
DEFAULT_ALLOCATIONS = {name: 100 / len(PROJECTS) for name in PROJECTS}
AUTONOMOUS_DISPATCH_FLAG = "owner_overview_autonomous_campaign_dispatch"
CAMPAIGN_SLOT_COUNT = 2
INTERACTIVE_RESERVED_SLOTS = 1


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_allocations(value: dict[str, Any]) -> dict[str, float]:
    """Normalize bounded numeric shares, preserving the exact 100 invariant."""
    if set(value) != set(PROJECTS):
        raise ValueError("allocations must name exactly Ruta, TripTruth, and MedicalBilling")
    try:
        result = {name: float(value[name]) for name in PROJECTS}
    except (TypeError, ValueError) as exc:
        raise ValueError("allocations must be numeric") from exc
    if any(not 0 <= share <= 100 for share in result.values()):
        raise ValueError("each allocation must be between 0 and 100")
    if round(sum(result.values()), 8) != 100:
        raise ValueError("allocations must total exactly 100")
    return result


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    allocations = data.get("allocations", DEFAULT_ALLOCATIONS)
    try:
        allocations = validate_allocations(allocations)
    except ValueError:
        allocations = dict(DEFAULT_ALLOCATIONS)
    return {"schema_version": 1, "updated_at": data.get("updated_at") or utcnow(),
            "allocations": allocations, "tasks": list(data.get("tasks") or []),
            "dispatch_counts": dict(data.get("dispatch_counts") or {}),
            "feature_flags": dict(data.get("feature_flags") or {}),
            "campaign_dispatch": dict(data.get("campaign_dispatch") or {}),
            "capacity": dict(data.get("capacity") or {"gateway_mode": "single", "verified_agents": 1})}


def save_allocations(path: Path, allocations: dict[str, Any]) -> dict[str, Any]:
    snapshot = load_snapshot(path)
    snapshot["allocations"] = validate_allocations(allocations)
    snapshot["updated_at"] = utcnow()
    atomic_write_json(path, snapshot)
    return snapshot


def capacity(snapshot: dict[str, Any]) -> dict[str, Any]:
    raw = snapshot.get("capacity", {})
    verified = max(1, int(raw.get("verified_agents", 1)))
    single = raw.get("gateway_mode", "single") == "single"
    actual = 1 if single else verified
    return {"gateway_mode": raw.get("gateway_mode", "single"), "actual_agent_capacity": actual,
            "parallel_agents": actual > 1}


def route(record: dict[str, Any], project_registry: dict[str, Iterable[str]]) -> str:
    """Route only against authoritative workdir/project identifiers, never text."""
    explicit = record.get("project")
    if explicit in PROJECTS:
        return explicit
    evidence = {str(record.get("workdir", "")), str(record.get("project_id", ""))}
    matches = [name for name in PROJECTS if evidence & set(map(str, project_registry.get(name, ()) ))]
    return matches[0] if len(matches) == 1 else "UNROUTED"


def ingest(snapshot: dict[str, Any], record: dict[str, Any], project_registry: dict[str, Iterable[str]]) -> dict[str, Any]:
    """Upsert by source + stable identity; owner priority and origin are retained."""
    origin = str(record.get("origin", "")).lower()
    identity = str(record.get("idempotency_key") or record.get("task_id") or record.get("job_id") or record.get("session_id") or "")
    if origin not in ORIGINS or not identity:
        raise ValueError("origin and one stable task/job/session identity are required")
    item = {key: record.get(key) for key in ("task_id", "job_id", "session_id", "origin", "chat_id", "thread_id", "workdir", "project_id", "owner_priority", "status", "state", "started_at", "updated_at", "evidence")}
    item["origin"], item["idempotency_key"] = origin, f"{origin}:{identity}"
    item["project"] = route(record, project_registry)
    item["status"] = str(item["status"] or item["state"] or "queued").lower()
    tasks = snapshot.setdefault("tasks", [])
    for index, old in enumerate(tasks):
        if old.get("idempotency_key") == item["idempotency_key"]:
            tasks[index] = {**old, **item}; break
    else:
        tasks.append(item)
    snapshot["updated_at"] = utcnow()
    return item


def next_eligible(snapshot: dict[str, Any], project: str) -> dict[str, Any] | None:
    eligible = [task for task in snapshot.get("tasks", []) if task.get("project") == project and task.get("status") in ACTIVE]
    return sorted(eligible, key=lambda task: (-int(task.get("owner_priority") or 0), str(task.get("updated_at") or ""), task["idempotency_key"]))[0] if eligible else None


def choose_next(snapshot: dict[str, Any]) -> str | None:
    """Deterministic weighted fair selection; any positive-share backlog cannot starve."""
    shares = validate_allocations(snapshot["allocations"])
    counts = snapshot.setdefault("dispatch_counts", {})
    candidates = [name for name in PROJECTS if shares[name] > 0 and next_eligible(snapshot, name)]
    if not candidates:
        return None
    return min(candidates, key=lambda name: (float(counts.get(name, 0)) / shares[name], PROJECTS.index(name)))


def dispatch_plan(snapshot: dict[str, Any]) -> dict[str, Any]:
    cap = capacity(snapshot)
    selected = choose_next(snapshot)
    lanes = []
    if selected:
        task = next_eligible(snapshot, selected)
        assert task is not None
        lanes.append({"project": selected, "task_id": task.get("task_id") or task["idempotency_key"], "mode": "writer"})
    # Additional capacity never makes a second mutable writer in a worktree.
    if cap["actual_agent_capacity"] > 1:
        for name in PROJECTS:
            if name != selected:
                candidate = next_eligible(snapshot, name)
                if candidate:
                    lanes.append({"project": name, "task_id": candidate.get("task_id"), "mode": "read_only_qa_or_separate_worktree"})
                    if len(lanes) == cap["actual_agent_capacity"]:
                        break
    return {"capacity": cap, "selected": selected, "lanes": lanes}


def _campaign_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    state = snapshot.setdefault("campaign_dispatch", {})
    state.setdefault("fair_debt", {name: 0.0 for name in PROJECTS})
    state.setdefault("launch_requests", [])
    return state


def _request_key(task: dict[str, Any]) -> str:
    identity = str(task.get("idempotency_key") or task.get("task_id") or "")
    return "campaign-launch:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def campaign_dispatch_decision(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Persist one fail-closed owner-overview request without launching work."""
    enabled = snapshot.get("feature_flags", {}).get(AUTONOMOUS_DISPATCH_FLAG) is True
    state = _campaign_state(snapshot)
    result: dict[str, Any] = {
        "enabled": enabled, "flag": AUTONOMOUS_DISPATCH_FLAG,
        "slot_count": CAMPAIGN_SLOT_COUNT, "interactive_reserved_slots": INTERACTIVE_RESERVED_SLOTS,
        "launch_requests": [],
    }
    if not enabled:
        result["reason"] = "feature_disabled"
        return result
    shares = validate_allocations(snapshot["allocations"])
    outstanding = {request.get("idempotency_key") for request in state["launch_requests"]
                   if request.get("status") in {"requested", "accepted", "running"}}
    interactive_workdirs = {str(task.get("workdir")) for task in snapshot.get("tasks", [])
                            if task.get("status") in ACTIVE and task.get("origin") != "cron"
                            and task.get("workdir")}
    candidates: dict[str, dict[str, Any]] = {}
    for project in PROJECTS:
        if shares[project] <= 0:
            continue
        task = next_eligible(snapshot, project)
        if not task or str(task.get("workdir") or "") in interactive_workdirs:
            continue
        if _request_key(task) not in outstanding:
            candidates[project] = task
    if not candidates:
        result["reason"] = "no_eligible_work"
        return result
    debt = state["fair_debt"]
    for project in PROJECTS:
        debt[project] = float(debt.get(project, 0.0)) + shares[project]
    selected = min(candidates, key=lambda project: (-float(debt[project]), PROJECTS.index(project)))
    debt[selected] -= 100.0
    task = candidates[selected]
    request = {
        "kind": "campaign_launch_request", "schema_version": 1,
        "idempotency_key": _request_key(task), "status": "requested",
        "project": selected, "task_id": task.get("task_id") or task.get("idempotency_key"),
        "workdir": task.get("workdir"), "mode": "writer", "origin": "owner_overview", "slot": 1,
        "facts": {"allocation_percent": shares[selected],
                  "owner_priority": int(task.get("owner_priority") or 0),
                  "source_idempotency_key": task.get("idempotency_key")},
    }
    state["launch_requests"].append(request)
    state["last_decision"] = {"selected": selected, "idempotency_key": request["idempotency_key"]}
    snapshot["updated_at"] = utcnow()
    result.update({"selected": selected, "launch_requests": [request]})
    return result


def owner_overview(path: Path) -> dict[str, Any]:
    """Read/update only the owner-overview snapshot and persist decisions."""
    snapshot = load_snapshot(path)
    decision = campaign_dispatch_decision(snapshot)
    if decision["enabled"] and decision["launch_requests"]:
        atomic_write_json(path, snapshot)
    overview = control_room(snapshot)
    overview["campaign_dispatch"] = decision
    return overview


def control_room(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Compact backend contract for Control Room and watchdog consumers."""
    cap = capacity(snapshot)
    projects = {}
    for name in PROJECTS:
        tasks = [task for task in snapshot["tasks"] if task.get("project") == name]
        active = next((task for task in tasks if task.get("status") == "running"), None)
        projects[name] = {"effective_share": snapshot["allocations"][name], "actual_agent_capacity": cap["actual_agent_capacity"],
            "working_now": active, "next_eligible_work": next_eligible(snapshot, name),
            "queued": [t for t in tasks if t.get("status") in {"queued", "pending", "claimed"}],
            "recent": [t for t in tasks if t.get("status") in TERMINAL], "blocked": [t for t in tasks if t.get("status") in {"blocked", "needs_owner"}],
            "milestone": {"value": None, "source": "UNKNOWN", "freshness": "UNKNOWN"},
            "difficulties": [], "open_questions": [], "needs_alexander": []}
    return {"updated_at": snapshot["updated_at"], "capacity": cap, "projects": projects,
            "unrouted": [t for t in snapshot["tasks"] if t.get("project") == "UNROUTED"], "dispatch": dispatch_plan(snapshot)}
