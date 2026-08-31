from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_capacity as cap


@pytest.fixture
def broker(tmp_path: Path) -> Path:
    path = tmp_path / "capacity.db"
    cap.init_db(path)
    return path


def test_twenty_thread_light_race_never_exceeds_two(broker: Path):
    def one(i: int):
        return cap.reserve(broker, board=f"b{i%5}", task_id=f"t_{i}", profile="qa")
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        leases = list(pool.map(one, range(20)))
    granted = [x for x in leases if x is not None]
    assert len(granted) == 2
    assert cap.active_counts(broker) == {"routing": 0, "light": 2, "heavy": 0}


def test_mixed_two_routing_three_light_defers_exact_third_light(broker: Path):
    specs = [
        ("r1", "default"), ("r2", "growth"),
        ("l1", "qa"), ("l2", "reviewer"), ("l3", "research"),
    ]
    result = [(task, cap.reserve(broker, board="operator", task_id=task, profile=profile)) for task, profile in specs]
    assert [task for task, lease in result if lease] == ["r1", "r2", "l1", "l2"]
    assert result[-1][1] is None
    assert cap.active_counts(broker) == {"routing": 2, "light": 2, "heavy": 0}


def test_unknown_profile_is_conservative_heavy(broker: Path):
    assert cap.class_for_profile("mystery") == "heavy"
    for i in range(3):
        assert cap.reserve(broker, board="b", task_id=f"t_{i}", profile="mystery")
    assert cap.reserve(broker, board="b", task_id="t_4", profile="mystery") is None


def test_board_first_reclaim_crash_converges_on_next_reservation(tmp_path: Path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles
    home=tmp_path/".hermes"; home.mkdir(); monkeypatch.setenv("HERMES_HOME",str(home)); monkeypatch.setattr(Path,"home",lambda:tmp_path)
    monkeypatch.setattr(profiles,"profile_exists",lambda _name:True)
    board_db=kb.init_db(); broker_db=tmp_path/"cross-db-capacity.db"; monkeypatch.setenv("HERMES_KANBAN_CAPACITY_DB",str(broker_db))
    with kb.connect(db_path=board_db) as con:
        task_id=kb.create_task(con,title="cross-db",assignee="qa"); kb.dispatch_once(con,spawn_fn=lambda *_a,**_k:999981)
        db=sqlite3.connect(broker_db); old=db.execute("SELECT lease_id FROM capacity_leases WHERE task_id=? AND state='running'",(task_id,)).fetchone()[0]; db.close()
        con.execute("UPDATE tasks SET claim_expires=1,last_heartbeat_at=1 WHERE id=?",(task_id,)); con.commit()
        with monkeypatch.context() as mp:
            mp.setattr(kb,"_capacity_release_reclaimed_task",lambda *_a,**_k:False)
            assert kb.release_stale_claims(con)==1
        assert kb.get_task(con,task_id).status=="ready"
        db=sqlite3.connect(broker_db); assert db.execute("SELECT state FROM capacity_leases WHERE lease_id=?",(old,)).fetchone()[0]=="running"; db.close()
        result=kb.dispatch_once(con,spawn_fn=lambda *_a,**_k:999982); assert result.spawned
        db=sqlite3.connect(broker_db); rows=db.execute("SELECT lease_id,state FROM capacity_leases WHERE task_id=? ORDER BY reserved_at",(task_id,)).fetchall(); db.close()
        assert rows[0]==(old,"released") and rows[-1][0]!=old and rows[-1][1]=="running"


def test_untracked_post_spawn_crash_window_holds_claim_and_capacity_until_zero_live(tmp_path: Path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles
    home=tmp_path/".hermes"; home.mkdir(); monkeypatch.setenv("HERMES_HOME",str(home)); monkeypatch.setattr(Path,"home",lambda:tmp_path)
    monkeypatch.setattr(profiles,"profile_exists",lambda _name:True)
    board_db=kb.init_db(); broker_db=tmp_path/"untracked-capacity.db"; cap.init_db(broker_db); monkeypatch.setenv("HERMES_KANBAN_CAPACITY_DB",str(broker_db))
    child=None
    with kb.connect(db_path=board_db) as con:
        task_id=kb.create_task(con,title="untracked",assignee="qa")
        lease=cap.reserve(broker_db,board=kb.get_current_board(),task_id=task_id,profile="qa"); assert lease
        claimed=kb.claim_task(con,task_id); assert claimed
        cap.bind(broker_db,lease_id=lease["lease_id"],state="claimed",run_id=claimed.current_run_id,claim_lock=claimed.claim_lock)
        env=os.environ.copy(); env.update({"HERMES_KANBAN_BOARD":kb.get_current_board(),"HERMES_KANBAN_TASK":task_id})
        child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],env=env,start_new_session=True)
        con.execute("UPDATE tasks SET claim_expires=1,last_heartbeat_at=1 WHERE id=?",(task_id,)); con.commit()
        assert kb.release_stale_claims(con)==0
        assert kb.get_task(con,task_id).status=="running"
        with pytest.raises(RuntimeError,match="live or unknown worker"):
            cap.release(broker_db,lease_id=lease["lease_id"],reason="unsafe")
        child.terminate(); child.wait(timeout=4)
        con.execute("UPDATE tasks SET claim_expires=1,last_heartbeat_at=1 WHERE id=?",(task_id,)); con.commit()
        assert kb.release_stale_claims(con)==1
        assert kb.get_task(con,task_id).status=="ready"
        assert cap.active_counts(broker_db)["light"]==0


def test_stale_native_reclaim_releases_old_lease_and_retry_gets_new_identity(tmp_path: Path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles
    home = tmp_path / ".hermes"; home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home)); monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    board_db = kb.init_db(); broker_db = tmp_path / "reclaim-capacity.db"
    monkeypatch.setenv("HERMES_KANBAN_CAPACITY_DB", str(broker_db))
    with kb.connect(db_path=board_db) as con:
        task_id = kb.create_task(con, title="reclaim", assignee="qa")
        first = kb.dispatch_once(con, spawn_fn=lambda *_args, **_kwargs: 999999)
        assert first.spawned
        con_cap=sqlite3.connect(broker_db); con_cap.row_factory=sqlite3.Row
        old=dict(con_cap.execute("SELECT * FROM capacity_leases WHERE task_id=? AND state!='released'", (task_id,)).fetchone()); con_cap.close()
        assert old["state"] == "running"
        con.execute("UPDATE tasks SET claim_expires=1, last_heartbeat_at=1 WHERE id=?", (task_id,)); con.commit()
        assert kb.release_stale_claims(con) == 1
        con_cap=sqlite3.connect(broker_db); active=con_cap.execute("SELECT COUNT(*) FROM capacity_leases WHERE task_id=? AND state!='released'", (task_id,)).fetchone()[0]; con_cap.close()
        assert active == 0
        second = kb.dispatch_once(con, spawn_fn=lambda *_args, **_kwargs: 999998)
        assert second.spawned
        con_cap=sqlite3.connect(broker_db); con_cap.row_factory=sqlite3.Row
        new=dict(con_cap.execute("SELECT * FROM capacity_leases WHERE task_id=? AND state!='released'", (task_id,)).fetchone()); con_cap.close()
        assert new["lease_id"] != old["lease_id"]


def test_native_dispatch_and_completion_share_broker(tmp_path: Path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles

    home = tmp_path / ".hermes"; home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    board_db = kb.init_db()
    broker_db = tmp_path / "native-capacity.db"
    cap.init_db(broker_db)
    monkeypatch.setenv("HERMES_KANBAN_CAPACITY_DB", str(broker_db))
    with kb.connect(db_path=board_db) as con:
        tasks = [kb.create_task(con, title=f"light-{i}", assignee=p) for i, p in enumerate(("qa", "reviewer", "research"))]
        first = kb.dispatch_once(con, spawn_fn=lambda _task, _workspace: 999999)
        assert len(first.spawned) == 2
        deferred = kb.get_task(con, tasks[2])
        assert deferred is not None and deferred.status == "ready"
        assert cap.active_counts(broker_db)["light"] == 2
        assert kb.complete_task(con, tasks[0], summary="done")
        second = kb.dispatch_once(con, spawn_fn=lambda _task, _workspace: 999998)
        assert [item[0] for item in second.spawned] == [tasks[2]]
        assert cap.active_counts(broker_db)["light"] == 2


def test_terminal_claimed_without_pid_waits_for_exact_zero_live_proof(broker: Path):
    lease=cap.reserve(broker,board="b",task_id="t_terminal_unknown",profile="qa"); assert lease
    cap.bind(broker,lease_id=lease["lease_id"],state="claimed",run_id=1,claim_lock="claim")
    assert cap.mark_terminal_by_task(broker,board="b",task_id="t_terminal_unknown",reason="fast-complete")==1
    assert cap.reconcile_terminal_exits(broker)==0
    assert cap.reconcile_terminal_exits(broker,no_worker_proof_fn=lambda _b,_t:False)==0
    assert cap.active_counts(broker)["light"]==1
    assert cap.reconcile_terminal_exits(broker,no_worker_proof_fn=lambda b,t:(b,t)==("b","t_terminal_unknown"))==1
    assert cap.active_counts(broker)["light"]==0


def test_claimed_without_pid_requires_explicit_no_worker_proof(broker: Path):
    lease = cap.reserve(broker, board="b", task_id="t_claimed", profile="qa")
    assert lease
    cap.bind(broker, lease_id=lease["lease_id"], state="claimed", run_id=1, claim_lock="claim")
    with pytest.raises(RuntimeError, match="live or unknown worker"):
        cap.release(broker, lease_id=lease["lease_id"], reason="unsafe-default")
    assert cap.release(
        broker, lease_id=lease["lease_id"], reason="pre-spawn-contained",
        no_worker_proven=True,
    )


def test_live_worker_cannot_be_released_by_lease_or_task(broker: Path):
    lease = cap.reserve(broker, board="b", task_id="t_live", profile="qa")
    assert lease
    cap.bind(
        broker, lease_id=lease["lease_id"], state="running", run_id=1,
        claim_lock="claim", worker_pid=os.getpid(), launch_id="launch",
    )
    with pytest.raises(RuntimeError, match="live or unknown worker"):
        cap.release(broker, lease_id=lease["lease_id"], reason="unsafe")
    with pytest.raises(RuntimeError, match="live or unknown worker"):
        cap.release_by_task(broker, board="b", task_id="t_live", reason="unsafe")
    assert cap.active_counts(broker)["light"] == 1


def test_bind_rejects_conflicting_runtime_identity(broker: Path):
    lease = cap.reserve(broker, board="b", task_id="t_bind", profile="qa")
    assert lease
    first = cap.bind(
        broker, lease_id=lease["lease_id"], state="claimed", run_id=7,
        claim_lock="claim-a", launch_id="launch-a",
    )
    assert first["run_id"] == 7
    assert cap.bind(
        broker, lease_id=lease["lease_id"], state="claimed", run_id=7,
        claim_lock="claim-a", launch_id="launch-a",
    )["run_id"] == 7
    with pytest.raises(RuntimeError, match="lease runtime identity mismatch:run_id"):
        cap.bind(broker, lease_id=lease["lease_id"], state="running", run_id=8)
    with pytest.raises(RuntimeError, match="lease runtime identity mismatch:claim_lock"):
        cap.bind(broker, lease_id=lease["lease_id"], state="running", claim_lock="claim-b")


def test_terminal_operations_and_completion_retry_converge_capacity(tmp_path: Path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles
    home=tmp_path/".hermes"; home.mkdir(); monkeypatch.setenv("HERMES_HOME",str(home)); monkeypatch.setattr(Path,"home",lambda:tmp_path)
    monkeypatch.setattr(profiles,"profile_exists",lambda _name:True)
    board_db=kb.init_db(); broker_db=tmp_path/"terminal-ops-capacity.db"; monkeypatch.setenv("HERMES_KANBAN_CAPACITY_DB",str(broker_db))
    with kb.connect(db_path=board_db) as con:
        complete_id=kb.create_task(con,title="complete",assignee="qa"); kb.dispatch_once(con,spawn_fn=lambda *_a,**_k:999991)
        monkeypatch.delenv("HERMES_KANBAN_CAPACITY_DB")
        assert kb.complete_task(con,complete_id,result="done")
        monkeypatch.setenv("HERMES_KANBAN_CAPACITY_DB",str(broker_db))
        assert kb.complete_task(con,complete_id,result="done") is False
        db=sqlite3.connect(broker_db); assert db.execute("SELECT state FROM capacity_leases WHERE task_id=?",(complete_id,)).fetchone()[0]=="terminal_pending_exit"; db.close()

        block_id=kb.create_task(con,title="block",assignee="qa"); kb.dispatch_once(con,spawn_fn=lambda *_a,**_k:999992)
        assert kb.block_task(con,block_id,reason="wait")
        db=sqlite3.connect(broker_db); assert db.execute("SELECT state FROM capacity_leases WHERE task_id=?",(block_id,)).fetchone()[0]=="terminal_pending_exit"; db.close()
        archive_id=kb.create_task(con,title="archive",assignee="qa"); kb.dispatch_once(con,spawn_fn=lambda *_a,**_k:999993)
        assert kb.archive_task(con,archive_id)
        db=sqlite3.connect(broker_db); assert db.execute("SELECT state FROM capacity_leases WHERE task_id=?",(archive_id,)).fetchone()[0]=="terminal_pending_exit"; db.close()


def test_release_reuses_capacity_and_exact_retry_is_idempotent(broker: Path):
    first = cap.reserve(broker, board="b", task_id="t_one", profile="qa")
    assert first
    assert cap.reserve(broker, board="b", task_id="t_one", profile="qa") == first
    with pytest.raises(RuntimeError, match="lease identity mismatch"):
        cap.reserve(broker, board="b", task_id="t_one", profile="default")
    cap.release(broker, lease_id=first["lease_id"], reason="terminal")
    second = cap.reserve(broker, board="b", task_id="t_two", profile="qa")
    assert second and second["lease_id"] != first["lease_id"]
