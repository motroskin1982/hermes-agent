from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli


def _complete_worker_env(sid: str) -> dict[str, str]:
    return {
        "HERMES_KANBAN_CAMPAIGN": "campaign-test",
        "HERMES_CONTROLLER_ASSIGNMENT": "pw_test",
        "HERMES_KANBAN_BOARD": "operator",
        "HERMES_KANBAN_TASK": "t_test",
        "HERMES_PROFILE": "terra",
        "HERMES_KANBAN_RUN_ID": "7",
        "HERMES_KANBAN_CLAIM_LOCK": "claim-test",
        "HERMES_KANBAN_LAUNCH_ID": "launch_test",
        "HERMES_KANBAN_WORKER_SESSION_ID": sid,
        "HERMES_SESSION_ID": sid,
    }


def test_kanban_worker_session_id_is_exact_and_fresh() -> None:
    sid = "ks_" + "a" * 32
    resolved, resumed = cli._resolve_cli_session_identity(resume=None, worker_session_id=sid)
    assert (resolved, resumed) == (sid, False)


@pytest.mark.parametrize("sid", ["", "ks_short", "other_" + "a" * 32, "ks_" + "g" * 32, "../ks_" + "a" * 32])
def test_malformed_kanban_worker_session_id_is_rejected(sid: str) -> None:
    with pytest.raises(ValueError, match="kanban worker session"):
        cli._resolve_cli_session_identity(resume=None, worker_session_id=sid)


def test_resume_and_kanban_worker_session_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="cannot resume"):
        cli._resolve_cli_session_identity(resume="old-session", worker_session_id="ks_" + "b" * 32)


def test_complete_kanban_launch_identity_is_required() -> None:
    sid = "ks_" + "a" * 32
    metadata = cli._resolve_kanban_worker_launch_identity(_complete_worker_env(sid))
    assert metadata["worker_session_id"] == sid
    assert metadata["launch_id"] == "launch_test"
    assert metadata["task_id"] == "t_test"


def test_incomplete_or_mismatched_kanban_launch_identity_is_rejected() -> None:
    sid = "ks_" + "a" * 32
    missing = _complete_worker_env(sid); missing.pop("HERMES_KANBAN_LAUNCH_ID")
    with pytest.raises(ValueError, match="incomplete kanban worker launch identity"):
        cli._resolve_kanban_worker_launch_identity(missing)
    mismatched = _complete_worker_env(sid); mismatched["HERMES_SESSION_ID"] = "ks_" + "b" * 32
    with pytest.raises(ValueError, match="session id mismatch"):
        cli._resolve_kanban_worker_launch_identity(mismatched)


def test_duplicate_active_kanban_worker_session_is_rejected(tmp_path, monkeypatch) -> None:
    from hermes_cli import active_sessions

    monkeypatch.setattr(active_sessions, "get_hermes_home", lambda: tmp_path)
    cfg = {}
    sid = "ks_" + "d" * 32
    first, message = active_sessions.try_acquire_active_session(
        session_id=sid, surface="kanban-worker", config=cfg,
        metadata={"require_unique_session_id": True},
    )
    assert first is not None and message is None
    try:
        second, duplicate_message = active_sessions.try_acquire_active_session(
            session_id=sid, surface="kanban-worker", config=cfg,
            metadata={"require_unique_session_id": True},
        )
        assert second is None
        assert "already active" in (duplicate_message or "").lower()
    finally:
        first.release()


def test_exclusive_lease_persists_exact_launch_metadata(tmp_path, monkeypatch) -> None:
    from hermes_cli import active_sessions

    monkeypatch.setattr(active_sessions, "get_hermes_home", lambda: tmp_path)
    sid = "ks_" + "e" * 32
    metadata = {
        "exclusive_session_id": True,
        "campaign_id": "campaign-test", "assignment_id": "pw_test",
        "board": "operator", "task_id": "t_test", "profile": "terra",
        "run_id": "7", "claim_lock": "claim-test", "launch_id": "launch_test",
    }
    lease, message = active_sessions.try_acquire_active_session(
        session_id=sid, surface="kanban-worker", config={}, metadata=metadata,
    )
    assert lease is not None and lease.enabled and message is None
    try:
        entries = active_sessions._read_entries(active_sessions._state_path())
        match = next(item for item in entries if item["session_id"] == sid)
        assert match["metadata"]["launch_id"] == "launch_test"
        assert match["metadata"]["task_id"] == "t_test"
    finally:
        lease.release()


def test_persisted_session_collision_is_rejected_without_recovery_authority() -> None:
    from hermes_cli.cli_agent_setup_mixin import _reject_unauthorized_persisted_worker_session

    sid = "ks_" + "f" * 32
    launch = cli._resolve_kanban_worker_launch_identity(_complete_worker_env(sid))
    instance = SimpleNamespace(
        _kanban_worker_launch=launch,
        _session_db=SimpleNamespace(get_session=lambda _sid: {"id": sid}),
    )
    with pytest.raises(RuntimeError, match="persisted session collision"):
        _reject_unauthorized_persisted_worker_session(instance)


def test_real_agent_startup_publishes_exact_handshake(monkeypatch) -> None:
    from hermes_cli import active_sessions
    from hermes_cli.cli_agent_setup_mixin import _finalize_kanban_worker_agent_handshake

    sid = "ks_" + "f" * 32
    launch = cli._resolve_kanban_worker_launch_identity(_complete_worker_env(sid))
    assert launch is not None
    monkeypatch.setenv("HERMES_SESSION_ID", sid)
    captured = {}
    monkeypatch.setattr(
        active_sessions, "transfer_active_session",
        lambda lease, *, session_id, metadata: captured.update(
            lease=lease, session_id=session_id, metadata=metadata
        ) or True,
    )
    lease = SimpleNamespace(enabled=True)
    instance = SimpleNamespace(
        session_id=sid,
        agent=SimpleNamespace(session_id=sid, compression_in_place=False),
        _kanban_worker_launch=launch,
        _active_session_lease=lease,
    )
    _finalize_kanban_worker_agent_handshake(instance)
    assert instance.agent.compression_in_place is True
    assert captured["session_id"] == sid
    assert captured["metadata"]["agent_initialized"] is True
    assert captured["metadata"]["launch_id"] == "launch_test"


def test_agent_startup_handshake_rejects_session_drift(monkeypatch) -> None:
    from hermes_cli.cli_agent_setup_mixin import _finalize_kanban_worker_agent_handshake

    sid = "ks_" + "f" * 32
    launch = cli._resolve_kanban_worker_launch_identity(_complete_worker_env(sid))
    monkeypatch.setenv("HERMES_SESSION_ID", sid)
    instance = SimpleNamespace(
        session_id=sid,
        agent=SimpleNamespace(session_id="wrong", compression_in_place=False),
        _kanban_worker_launch=launch,
        _active_session_lease=SimpleNamespace(enabled=True),
    )
    with pytest.raises(RuntimeError, match="agent session identity drift"):
        _finalize_kanban_worker_agent_handshake(instance)


def test_real_hermes_cli_to_aiagent_startup_uses_exact_worker_session(tmp_path) -> None:
    sid = "ks_" + "c" * 32
    env = os.environ.copy()
    env.update(_complete_worker_env(sid))
    env["HERMES_HOME"] = str(tmp_path)
    script = r'''
import json
from pathlib import Path
import cli
c=cli.HermesCLI(provider="ollama", model="smoke-model", api_key="local-smoke", base_url="http://127.0.0.1:9")
c._claim_active_session(surface="kanban-worker")
ok=c._init_agent(runtime_override={"provider":"ollama","api_key":"local-smoke","base_url":"http://127.0.0.1:9","api_mode":None,"command":None,"args":[],"credential_pool":None})
registry=json.loads((Path(__import__('os').environ['HERMES_HOME'])/'runtime'/'active_sessions.json').read_text())
entry=next(x for x in registry['entries'] if x['session_id']==c.session_id)
print(json.dumps({"ok":ok,"cli":c.session_id,"agent":c.agent.session_id,"in_place":c.agent.compression_in_place,"handshake":getattr(c,'_kanban_agent_handshake',False),"metadata":entry['metadata']}))
c._release_active_session()
'''
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(Path(cli.__file__).parent), env=env, text=True, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert (payload["ok"], payload["cli"], payload["agent"], payload["in_place"], payload["handshake"]) == (True, sid, sid, True, True)
    assert payload["metadata"]["agent_initialized"] is True
    assert payload["metadata"]["launch_id"] == "launch_test"
    assert payload["metadata"]["task_id"] == "t_test"


def test_active_session_registry_failure_is_fail_closed_for_kanban_worker(monkeypatch) -> None:
    from hermes_cli import active_sessions

    def boom(**_kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(active_sessions, "try_acquire_active_session", boom)
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SESSION_ID", "ks_" + "c" * 32)
    instance = object.__new__(cli.HermesCLI)
    instance._active_session_lease = None
    instance.session_id = "ks_" + "c" * 32
    instance.config = {}
    assert instance._claim_active_session(stderr=True) is False
