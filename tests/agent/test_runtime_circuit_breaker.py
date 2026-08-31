import json
from types import SimpleNamespace

from agent import runtime_circuit_breaker as rcb


def _failure(message="boom"):
    return json.dumps({"error": message}, sort_keys=True)


def test_three_identical_consecutive_failures_trip():
    breaker = rcb.SessionCircuitBreaker()
    assert breaker.observe("terminal", _failure()) is None
    assert breaker.observe("terminal", _failure()) is None
    trip = breaker.observe("terminal", _failure())
    assert trip["kind"] == "identical_consecutive_failures"
    assert trip["count"] == 3


def test_six_failures_from_one_tool_trip_even_when_not_identical():
    breaker = rcb.SessionCircuitBreaker()
    for index in range(5):
        assert breaker.observe("terminal", _failure(str(index))) is None
        breaker.observe("read_file", json.dumps({"success": True}))
    trip = breaker.observe("terminal", _failure("fifth"))
    assert trip["kind"] == "tool_failure_limit"
    assert trip["tool"] == "terminal"
    assert trip["count"] == 6


def test_success_resets_identical_and_same_tool_failure_counters():
    breaker = rcb.SessionCircuitBreaker()
    breaker.observe("terminal", _failure())
    breaker.observe("terminal", _failure())
    breaker.observe("terminal", json.dumps({"success": True}))
    assert breaker.observe("terminal", _failure()) is None
    assert breaker.consecutive_count == 1
    assert breaker.tool_failures["terminal"] == 1


def test_harmless_diagnostic_failures_separated_by_success_do_not_pause_session():
    breaker = rcb.SessionCircuitBreaker()
    session_id = "diagnostic-session"
    for index in range(8):
        assert breaker.observe("terminal", _failure(f"expected-red-test-{index}")) is None
        assert breaker.observe("terminal", json.dumps({"success": True, "session_id": session_id})) is None
    assert breaker.tripped is None
    assert breaker.tool_failures["terminal"] == 0


def test_breakers_are_isolated_between_sessions():
    first = rcb.SessionCircuitBreaker()
    second = rcb.SessionCircuitBreaker()
    first.observe("terminal", _failure())
    first.observe("terminal", _failure())
    assert second.observe("terminal", _failure()) is None
    assert second.consecutive_count == 1
    assert first.consecutive_count == 2


def test_observe_stops_processing_after_breaker():
    breaker = rcb.SessionCircuitBreaker()
    messages = [
        {"role": "tool", "name": "terminal", "content": _failure()},
        {"role": "tool", "name": "terminal", "content": _failure()},
        {"role": "tool", "name": "terminal", "content": _failure()},
        {"role": "tool", "name": "read_file", "content": _failure("must-not-run")},
    ]
    trip = rcb.observe_tool_messages(breaker, messages, 0)
    assert trip["kind"] == "identical_consecutive_failures"
    assert breaker.tool_failures["read_file"] == 0


def test_checkpoint_shape_is_non_secret_and_continuable(tmp_path, monkeypatch):
    monkeypatch.setattr(rcb, "get_hermes_home", lambda: tmp_path)
    agent = SimpleNamespace(
        session_id="cron:job/unsafe",
        platform="cron",
        session_input_tokens=90_001,
        session_api_calls=7,
    )
    messages = [
        {"role": "user", "content": "SECRET-CONTENT"},
        {"role": "tool", "name": "terminal", "tool_call_id": "call-7", "content": "SECRET-RESULT"},
    ]
    checkpoint = rcb.persist_runtime_checkpoint(
        agent, messages, "runtime_circuit_breaker",
        {"kind": "tool_failure_limit", "tool": "terminal", "count": 6},
    )
    saved = json.loads(rcb.Path(checkpoint["checkpoint_path"]).read_text())
    assert saved["schema"] == rcb.CHECKPOINT_SCHEMA
    assert saved["continuation_required"] is True
    assert saved["safe_continuation"] == {
        "message_count": 2,
        "last_role": "tool",
        "last_tool_call_id": "call-7",
        "input_tokens": 90_001,
        "api_calls": 7,
    }
    assert "SECRET" not in json.dumps(saved)
    assert checkpoint["checkpoint_path"].endswith(".json")
