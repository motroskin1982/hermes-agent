from __future__ import annotations

import ast
from pathlib import Path


RUN_PY = Path(__file__).resolve().parents[2] / "gateway" / "run.py"


def test_run_sync_declares_session_id_nonlocal_when_rotating_session():
    """A conditional session rotation must not shadow the outer session_id.

    Regression: assigning ``session_id`` inside ``run_sync`` without a
    ``nonlocal`` declaration made every gateway turn fail before the model call
    with UnboundLocalError.
    """
    tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_run_agent_inner":
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == "run_sync":
                    target = child
                    break
    assert target is not None

    assigned_session_id = any(
        isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == "session_id"
        for node in ast.walk(target)
    )
    declared_nonlocal = {
        name
        for node in target.body
        if isinstance(node, ast.Nonlocal)
        for name in node.names
    }
    assert not assigned_session_id or "session_id" in declared_nonlocal
