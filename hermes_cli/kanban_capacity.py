"""Optional shared Kanban worker-capacity broker.

The broker is fail-closed and transactional. It has no effect unless a caller
explicitly invokes it; native dispatcher integration is gated separately.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

DEFAULT_LIMITS = {"routing": 2, "light": 2, "heavy": 3}
ACTIVE_STATES = ("reserved", "claimed", "spawned", "running", "terminal_pending_exit")
ROUTING_PROFILES = {"default", "growth"}
LIGHT_PROFILES = {"qa", "reviewer", "research"}


def class_for_profile(profile: str | None) -> str:
    name = (profile or "").strip().lower()
    if name in ROUTING_PROFILES:
        return "routing"
    if name in LIGHT_PROFILES:
        return "light"
    return "heavy"


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(path)
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS capacity_leases (
          lease_id TEXT PRIMARY KEY,
          board TEXT NOT NULL,
          task_id TEXT NOT NULL,
          assignment_id TEXT,
          profile TEXT NOT NULL,
          concurrency_class TEXT NOT NULL CHECK(concurrency_class IN ('routing','light','heavy')),
          state TEXT NOT NULL CHECK(state IN ('reserved','claimed','spawned','running','terminal_pending_exit','released','failed_closed')),
          run_id INTEGER,
          claim_lock TEXT,
          worker_pid INTEGER,
          worker_start_ticks INTEGER,
          worker_boot_id TEXT,
          launch_id TEXT,
          reserved_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          released_at INTEGER,
          reason TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS capacity_active_task
          ON capacity_leases(board,task_id)
          WHERE state IN ('reserved','claimed','spawned','running','terminal_pending_exit');
        CREATE INDEX IF NOT EXISTS capacity_active_class
          ON capacity_leases(concurrency_class,state);
        """)
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(capacity_leases)")}
        if "assignment_id" not in columns:
            con.execute("ALTER TABLE capacity_leases ADD COLUMN assignment_id TEXT")
    finally:
        con.close()
    path.chmod(0o600)


def configured_path() -> Path | None:
    raw = os.environ.get("HERMES_KANBAN_CAPACITY_DB", "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def bind(
    path: Path, *, lease_id: str, state: str, run_id: int | None = None,
    claim_lock: str | None = None, worker_pid: int | None = None,
    launch_id: str | None = None,
) -> dict[str, Any]:
    if state not in {"claimed", "spawned", "running"}:
        raise ValueError("invalid active lease state")
    worker_start_ticks = None
    worker_boot_id = None
    if worker_pid:
        try:
            stat_text = Path(f"/proc/{int(worker_pid)}/stat").read_text(encoding="utf-8")
            tail = stat_text[stat_text.rfind(")") + 2:].split()
            worker_start_ticks = int(tail[19])
            worker_boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        except (OSError, ValueError, IndexError):
            pass
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM capacity_leases WHERE lease_id=?", (lease_id,)).fetchone()
        if not row or row["state"] not in ACTIVE_STATES:
            raise RuntimeError("active lease missing")
        if row["state"] == "terminal_pending_exit":
            raise RuntimeError("terminal lease cannot be rebound")
        proposed = {
            "run_id": run_id, "claim_lock": claim_lock, "worker_pid": worker_pid,
            "worker_start_ticks": worker_start_ticks, "worker_boot_id": worker_boot_id,
            "launch_id": launch_id,
        }
        for field, value in proposed.items():
            if value is not None and row[field] is not None and str(row[field]) != str(value):
                raise RuntimeError(f"lease runtime identity mismatch:{field}")
        rank = {"reserved": 0, "claimed": 1, "spawned": 2, "running": 3}
        effective_state = row["state"] if rank[row["state"]] > rank[state] else state
        con.execute(
            """UPDATE capacity_leases SET state=?,run_id=COALESCE(?,run_id),claim_lock=COALESCE(?,claim_lock),
                 worker_pid=COALESCE(?,worker_pid),worker_start_ticks=COALESCE(?,worker_start_ticks),
                 worker_boot_id=COALESCE(?,worker_boot_id),launch_id=COALESCE(?,launch_id),updated_at=? WHERE lease_id=?""",
            (effective_state, run_id, claim_lock, worker_pid, worker_start_ticks, worker_boot_id, launch_id, int(time.time()), lease_id),
        )
        updated = con.execute("SELECT * FROM capacity_leases WHERE lease_id=?", (lease_id,)).fetchone()
        con.commit()
        return dict(updated)
    except Exception:
        con.rollback(); raise
    finally:
        con.close()


def rebind_task_identity(
    path: Path, *, lease_id: str, board: str, assignment_id: str, task_id: str,
) -> dict[str, Any]:
    """Atomically replace a controller pending ID with its materialized board task ID."""
    if not all((lease_id, board, assignment_id, task_id)):
        raise ValueError("complete lease rebind identity required")
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM capacity_leases WHERE lease_id=?", (lease_id,)).fetchone()
        if not row or row["state"] not in ACTIVE_STATES:
            raise RuntimeError("active lease missing")
        if (row["board"], row["assignment_id"], row["task_id"]) != (board, assignment_id, assignment_id):
            if (row["board"], row["assignment_id"], row["task_id"]) == (board, assignment_id, task_id):
                con.commit(); return dict(row)
            raise RuntimeError("lease rebind identity mismatch")
        con.execute(
            "UPDATE capacity_leases SET task_id=?,updated_at=? WHERE lease_id=? AND task_id=? AND assignment_id=?",
            (task_id, int(time.time()), lease_id, assignment_id, assignment_id),
        )
        updated = con.execute("SELECT * FROM capacity_leases WHERE lease_id=?", (lease_id,)).fetchone()
        con.commit(); return dict(updated)
    except Exception:
        con.rollback(); raise
    finally:
        con.close()


def mark_terminal_by_task(path: Path, *, board: str, task_id: str, reason: str) -> int:
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute(
            """UPDATE capacity_leases SET state='terminal_pending_exit',updated_at=?,reason=?
               WHERE board=? AND task_id=? AND state IN ('reserved','claimed','spawned','running')""",
            (int(time.time()), reason[:300], board, task_id),
        )
        con.commit(); return int(cur.rowcount)
    finally:
        con.close()


def _recorded_process_alive(row: sqlite3.Row) -> bool:
    pid = int(row["worker_pid"] or 0)
    if pid <= 1:
        return False
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        tail = stat_text[stat_text.rfind(")") + 2:].split()
        if row["worker_start_ticks"] is not None and int(tail[19]) != int(row["worker_start_ticks"]):
            return False
        if row["worker_boot_id"]:
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
            if boot != row["worker_boot_id"]:
                return False
        return True
    except (OSError, ValueError, IndexError):
        return False


def _release_permitted(row: sqlite3.Row, *, no_worker_proven: bool = False) -> bool:
    state = str(row["state"])
    if state == "reserved" and not row["worker_pid"]:
        return True
    if state in {"claimed", "spawned", "running", "terminal_pending_exit"} and not row["worker_pid"]:
        return bool(no_worker_proven)
    return not _recorded_process_alive(row)


def reconcile_terminal_exits(path: Path, *, no_worker_proof_fn=None) -> int:
    con = _connect(path)
    try:
        rows = con.execute("SELECT * FROM capacity_leases WHERE state='terminal_pending_exit'").fetchall()
        released = 0
        for row in rows:
            no_worker_proven = False
            if not row["worker_pid"] and no_worker_proof_fn is not None:
                try:
                    no_worker_proven = bool(no_worker_proof_fn(str(row["board"]), str(row["task_id"])))
                except Exception:
                    no_worker_proven = False
            if _release_permitted(row, no_worker_proven=no_worker_proven):
                con.execute("BEGIN IMMEDIATE")
                cur = con.execute(
                    """UPDATE capacity_leases SET state='released',released_at=?,updated_at=?
                       WHERE lease_id=? AND state='terminal_pending_exit'""",
                    (int(time.time()), int(time.time()), row["lease_id"]),
                )
                con.commit(); released += int(cur.rowcount)
        return released
    finally:
        con.close()


def release_by_task(
    path: Path, *, board: str, task_id: str, reason: str,
    no_worker_proven: bool = False, include_reserved: bool = True,
) -> int:
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        states = "'reserved','claimed','spawned','running','terminal_pending_exit'" if include_reserved else "'claimed','spawned','running','terminal_pending_exit'"
        rows = con.execute(
            f"SELECT * FROM capacity_leases WHERE board=? AND task_id=? AND state IN ({states})",
            (board, task_id),
        ).fetchall()
        if any(not _release_permitted(row, no_worker_proven=no_worker_proven) for row in rows):
            raise RuntimeError("live or unknown worker prevents capacity release")
        now = int(time.time())
        cur = con.execute(
            f"""UPDATE capacity_leases SET state='released',released_at=?,updated_at=?,reason=?
               WHERE board=? AND task_id=? AND state IN ({states})""",
            (now, now, reason[:300], board, task_id),
        )
        con.commit()
        return int(cur.rowcount)
    finally:
        con.close()


def _lease_id(board: str, task_id: str, reserved_at: int) -> str:
    raw = f"{board}\0{task_id}\0{reserved_at}\0{time.time_ns()}".encode()
    return "lease_" + hashlib.sha256(raw).hexdigest()[:20]


def reserve(
    path: Path, *, board: str, task_id: str, profile: str,
    concurrency_class: str | None = None, limits: dict[str, int] | None = None,
    assignment_id: str | None = None,
) -> dict[str, Any] | None:
    if not board or not task_id or not profile:
        raise ValueError("board/task/profile required")
    cls = concurrency_class or class_for_profile(profile)
    caps = dict(DEFAULT_LIMITS if limits is None else limits)
    if set(caps) != set(DEFAULT_LIMITS) or cls not in caps or any(int(v) < 0 for v in caps.values()):
        raise ValueError("invalid capacity limits/class")
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT * FROM capacity_leases WHERE board=? AND task_id=? AND state IN ('reserved','claimed','spawned','running','terminal_pending_exit')",
            (board, task_id),
        ).fetchone()
        if existing:
            if (existing["profile"], existing["concurrency_class"], existing["assignment_id"]) != (profile, cls, assignment_id):
                raise RuntimeError("lease identity mismatch")
            con.commit()
            return dict(existing)
        count = int(con.execute(
            "SELECT COUNT(*) FROM capacity_leases WHERE concurrency_class=? AND state IN ('reserved','claimed','spawned','running','terminal_pending_exit')",
            (cls,),
        ).fetchone()[0])
        if count >= int(caps[cls]):
            con.commit()
            return None
        now = int(time.time())
        lease_id = _lease_id(board, task_id, now)
        con.execute(
            """INSERT INTO capacity_leases(
                 lease_id,board,task_id,assignment_id,profile,concurrency_class,state,reserved_at,updated_at
               ) VALUES(?,?,?,?,?,?,'reserved',?,?)""",
            (lease_id, board, task_id, assignment_id, profile, cls, now, now),
        )
        row = con.execute("SELECT * FROM capacity_leases WHERE lease_id=?", (lease_id,)).fetchone()
        con.commit()
        return dict(row)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def release(path: Path, *, lease_id: str, reason: str, no_worker_proven: bool = False) -> bool:
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM capacity_leases WHERE lease_id=? AND state IN ('reserved','claimed','spawned','running','terminal_pending_exit')",
            (lease_id,),
        ).fetchone()
        if row and not _release_permitted(row, no_worker_proven=no_worker_proven):
            raise RuntimeError("live or unknown worker prevents capacity release")
        now = int(time.time())
        cur = con.execute(
            """UPDATE capacity_leases SET state='released',released_at=?,updated_at=?,reason=?
               WHERE lease_id=? AND state IN ('reserved','claimed','spawned','running','terminal_pending_exit')""",
            (now, now, reason[:300], lease_id),
        )
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()


def active_counts(path: Path) -> dict[str, int]:
    result = {key: 0 for key in DEFAULT_LIMITS}
    con = _connect(path)
    try:
        for row in con.execute(
            "SELECT concurrency_class,COUNT(*) AS n FROM capacity_leases WHERE state IN ('reserved','claimed','spawned','running','terminal_pending_exit') GROUP BY concurrency_class"
        ):
            result[str(row["concurrency_class"])] = int(row["n"])
        return result
    finally:
        con.close()
