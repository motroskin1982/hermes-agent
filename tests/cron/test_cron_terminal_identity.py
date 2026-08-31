from __future__ import annotations

import contextvars

from tools.environments.local import (
    ATTESTED_CRON_JOB_ID_ENV,
    ATTESTED_CRON_SESSION_ID_ENV,
    _make_run_env,
    _sanitize_subprocess_env,
    reset_attested_cron_identity,
    set_attested_cron_identity,
)


def _identity(env):
    return env.get(ATTESTED_CRON_JOB_ID_ENV), env.get(ATTESTED_CRON_SESSION_ID_ENV)


def test_no_attested_context_propagates_outside_cron(monkeypatch):
    monkeypatch.setenv(ATTESTED_CRON_JOB_ID_ENV, "foreign")
    monkeypatch.setenv(ATTESTED_CRON_SESSION_ID_ENV, "foreign")
    assert _identity(_make_run_env({})) == (None, None)
    inherited = {
        ATTESTED_CRON_JOB_ID_ENV: "foreign",
        ATTESTED_CRON_SESSION_ID_ENV: "foreign",
    }
    assert _identity(_sanitize_subprocess_env(inherited, {})) == (None, None)


def test_attested_context_isolated_per_context_and_passed_to_terminal_env():
    def build(job_id, session_id):
        tokens = set_attested_cron_identity(job_id, session_id)
        try:
            foreground = _make_run_env({})
            background = _sanitize_subprocess_env({}, {})
            return _identity(foreground), _identity(background)
        finally:
            reset_attested_cron_identity(tokens)

    first = contextvars.Context().run(build, "a2bd90ba81fa", "cron_a2bd90ba81fa_20260722_180000")
    second = contextvars.Context().run(build, "05e000a66529", "cron_05e000a66529_20260722_180001")
    assert first == (("a2bd90ba81fa", "cron_a2bd90ba81fa_20260722_180000"),) * 2
    assert second == (("05e000a66529", "cron_05e000a66529_20260722_180001"),) * 2
    assert _identity(_make_run_env({})) == (None, None)
