from cron.three_project_orchestrator import (
    AUTONOMOUS_DISPATCH_FLAG, atomic_write_json, campaign_dispatch_decision,
    load_snapshot, owner_overview,
)


def snapshot(*, enabled=False):
    return {
        "allocations": {"Ruta": 50, "TripTruth": 50, "MedicalBilling": 0},
        "feature_flags": {AUTONOMOUS_DISPATCH_FLAG: enabled},
        "tasks": [
            {"project": "Ruta", "task_id": "r1", "idempotency_key": "cron:r1", "origin": "cron", "status": "queued", "workdir": "/ruta"},
            {"project": "TripTruth", "task_id": "t1", "idempotency_key": "cron:t1", "origin": "cron", "status": "queued", "workdir": "/trip"},
            {"project": "MedicalBilling", "task_id": "m1", "idempotency_key": "cron:m1", "origin": "cron", "status": "queued", "workdir": "/med"},
        ],
    }


def test_flag_is_default_disabled_and_emits_nothing():
    result = campaign_dispatch_decision(snapshot())
    assert result["reason"] == "feature_disabled"
    assert result["launch_requests"] == []


def test_enabled_emits_one_structured_request_and_reserves_interactive_slot():
    result = campaign_dispatch_decision(snapshot(enabled=True))
    assert (result["slot_count"], result["interactive_reserved_slots"]) == (2, 1)
    assert len(result["launch_requests"]) == 1
    request = result["launch_requests"][0]
    assert (request["kind"], request["status"], request["origin"]) == ("campaign_launch_request", "requested", "owner_overview")
    assert request["project"] != "MedicalBilling"


def test_same_worktree_interactive_work_and_zero_share_are_excluded():
    value = snapshot(enabled=True)
    value["tasks"].append({"project": "Ruta", "origin": "desktop", "status": "running", "workdir": "/ruta", "idempotency_key": "desktop:1"})
    assert campaign_dispatch_decision(value)["selected"] == "TripTruth"


def test_fair_debt_and_idempotency_are_persisted(tmp_path):
    path = tmp_path / "overview.json"
    atomic_write_json(path, snapshot(enabled=True))
    first = owner_overview(path)["campaign_dispatch"]
    second = owner_overview(path)["campaign_dispatch"]
    persisted = load_snapshot(path)
    assert (first["selected"], second["selected"]) == ("Ruta", "TripTruth")
    requests = persisted["campaign_dispatch"]["launch_requests"]
    assert len(requests) == len({request["idempotency_key"] for request in requests}) == 2
    assert persisted["campaign_dispatch"]["fair_debt"] == {"Ruta": 0.0, "TripTruth": 0.0, "MedicalBilling": 0.0}
