"""Exercise the actual attribution workflow against a synthetic fork history."""
import os
from pathlib import Path
import subprocess

import yaml


def test_maintenance_base_excludes_inherited_authors_but_checks_new_humans(tmp_path):
    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/contributor-check.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["check-attribution"]["steps"]
    check = next(step["run"] for step in steps if "run" in step)
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid",
               GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="inherited@example.invalid")

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, env=env,
                              capture_output=True, text=True, check=True)

    git("init", "-q")
    git("commit", "--allow-empty", "-qm", "upstream")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("commit", "--allow-empty", "-qm", "fork history")
    git("update-ref", "refs/remotes/origin/maintenance", "HEAD")
    env["GIT_AUTHOR_EMAIL"] = "codex@localhost"
    git("commit", "--allow-empty", "-qm", "automation repair")
    env["PR_BASE_REF"] = "maintenance"
    result = subprocess.run(["bash", "-e", "-c", check], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    env["GIT_AUTHOR_EMAIL"] = "new-human@example.invalid"
    git("commit", "--allow-empty", "-qm", "new contribution")
    result = subprocess.run(["bash", "-e", "-c", check], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "new-human@example.invalid" in result.stdout
    assert "inherited@example.invalid" not in result.stdout
