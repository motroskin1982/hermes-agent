"""Fail-closed resolver for compact, project-scoped system-prompt memory.

Project memory is read-only at runtime.  A registry maps an exact messaging lane
or a workdir prefix to one project; ambiguous and unknown contexts inject nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from tools.threat_patterns import scan_for_threats

DEFAULT_MAX_CHARS = 1_200
MAX_PROJECT_MEMORY_CHARS = 1_200
_SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class ProjectMemoryResolution:
    slug: Optional[str]
    prompt_block: str
    reason: str


def _normalized_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    return str(Path(value).expanduser().resolve(strict=False)).rstrip("/")


def _read_registry(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    projects = parsed.get("projects") if isinstance(parsed, dict) else None
    return projects if isinstance(projects, dict) else {}


def _lane_matches(entry: dict[str, Any], *, platform: object, chat_id: object, thread_id: object) -> bool:
    if not (platform and chat_id):
        return False
    for lane in entry.get("lanes", []):
        if not isinstance(lane, dict):
            continue
        if str(lane.get("platform", "")) != str(platform):
            continue
        if str(lane.get("chat_id", "")) != str(chat_id):
            continue
        expected_thread = lane.get("thread_id")
        normalized_expected = str(expected_thread) if expected_thread is not None else None
        normalized_actual = str(thread_id) if thread_id is not None else None
        if normalized_expected != normalized_actual:
            continue
        return True
    return False


def _workdir_matches(entry: dict[str, Any], workdir: str) -> bool:
    if not workdir:
        return False
    for prefix in entry.get("workdir_prefixes", []):
        normalized = _normalized_path(prefix)
        if normalized and (workdir == normalized or workdir.startswith(normalized + "/")):
            return True
    return False


def _safe_slug(value: object) -> bool:
    return isinstance(value, str) and bool(_SAFE_SLUG_RE.fullmatch(value))


def _select_slug(
    projects: dict[str, Any], *, platform: object, chat_id: object, thread_id: object, workdir: str
) -> tuple[Optional[str], str]:
    valid = {slug: entry for slug, entry in projects.items() if _safe_slug(slug) and isinstance(entry, dict)}
    lane_hits = [slug for slug, entry in valid.items() if _lane_matches(entry, platform=platform, chat_id=chat_id, thread_id=thread_id)]
    if len(lane_hits) == 1:
        return lane_hits[0], "exact_lane"
    if len(lane_hits) > 1:
        return None, "ambiguous_lane"
    cwd_hits = [slug for slug, entry in valid.items() if _workdir_matches(entry, workdir)]
    if len(cwd_hits) == 1:
        return cwd_hits[0], "workdir"
    return None, "ambiguous_workdir" if cwd_hits else "unmatched"


def _prompt_for_slug(slug: str, memory_root: Path, max_chars: int, reason: str) -> ProjectMemoryResolution:
    if not _safe_slug(slug):
        return ProjectMemoryResolution(None, "", "invalid_slug")
    try:
        root = memory_root.resolve(strict=True)
        memory_path = (root / slug / "MEMORY.md").resolve(strict=True)
        if not memory_path.is_relative_to(root):
            return ProjectMemoryResolution(None, "", "outside_memory_root")
        text = memory_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ProjectMemoryResolution(slug, "", "missing_memory_file")
    effective_max = min(max(0, int(max_chars)), MAX_PROJECT_MEMORY_CHARS)
    if not text:
        return ProjectMemoryResolution(slug, "", "empty_memory_file")
    if len(text) > effective_max:
        return ProjectMemoryResolution(slug, "", "oversized_memory_file")
    findings = scan_for_threats(text, scope="strict")
    if findings:
        return ProjectMemoryResolution(slug, f"[BLOCKED: Project memory '{slug}' contained threat pattern(s): {', '.join(findings)}. Removed from system prompt.]", "blocked_threat")
    return ProjectMemoryResolution(slug, f"PROJECT MEMORY — {slug}\n{text}", reason)


def resolve_project_memory(
    *,
    registry_path: Path,
    platform: object = None,
    chat_id: object = None,
    thread_id: object = None,
    workdir: object = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> ProjectMemoryResolution:
    """Resolve one safe project block or return an empty block.

    Exact lane identity takes precedence over cwd.  There is deliberately no
    fuzzy project-name matching and no merge across projects.
    """
    projects = _read_registry(registry_path)
    slug, reason = _select_slug(
        projects, platform=platform, chat_id=chat_id, thread_id=thread_id,
        workdir=_normalized_path(workdir),
    )
    if slug is None:
        return ProjectMemoryResolution(None, "", reason)

    return _prompt_for_slug(slug, registry_path.parent, max_chars, reason)


def resolve_project_memory_from_projects_db(*, workdir: object, memory_root: Path, db_path: Path, max_chars: int = DEFAULT_MAX_CHARS) -> ProjectMemoryResolution:
    """Resolve one project from Hermes' native per-profile Projects DB.

    The database is opened read-only and only the session-local cwd participates;
    the profile-global active-project pointer is intentionally ignored.
    """
    cwd = _normalized_path(workdir)
    if not cwd or not db_path.is_file():
        return ProjectMemoryResolution(None, "", "unmatched")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        from hermes_cli.projects_db import normalize_slug, project_for_path
        try:
            project = project_for_path(conn, cwd)
        finally:
            conn.close()
        if project is None:
            return ProjectMemoryResolution(None, "", "unmatched")
        slug = normalize_slug(project.slug)
        if not slug:
            return ProjectMemoryResolution(None, "", "invalid_slug")
    except Exception:
        return ProjectMemoryResolution(None, "", "projects_db_unavailable")
    return _prompt_for_slug(slug, memory_root, max_chars, "projects_db")
