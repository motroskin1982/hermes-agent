# Repository-local watchdog reconstruction

This module is a new implementation reconstructed from the ten retained
`tests/cron/test_three_project_continuous_watchdog.py` contracts. The historical
host-local script was unavailable. This is not recovered source, and passing
these tests is not approval to activate multi-project automation.

The Nova release prerequisite is a clean-checkout test suite with a real,
portable implementation, without a dependency on `/srv/hermes/state/scripts`.
Only the original fixture's source path changes; all ten assertions remain.

## Contract and reconstruction choices

The retained contracts require one oldest-stale writer per invocation; stable
Ruta, TripTruth, MedicalBilling report ordering; paused/disabled suppression;
recent and soon-scheduled suppression; nonblocking overlap; persistent duplicate
suppression; and routed Telegram/Codex/Kanban active-work suppression. Scheduler
`state=running`, previews, LSPs and ready cards alone do not prove active work.
Terminal records from unknown allocation origins are reported as UNROUTED, not
active work; unknown-origin nonterminal records block dispatch.

The exact boundaries were not preserved in those tests. This implementation
chooses an inclusive 15-minute recent window, inclusive 5-minute future schedule
window, and inclusive 15-minute per-writer dispatch cooldown. Never-run writers sort
before dated writers; ties follow project order. These are reconstruction
choices, not historical behavior claims. Adjacent orchestrator origin and active
state vocabularies are retained. Its `tasks` array and the historical
`allocations` array are accepted; its percentage `allocations` object is never
interpreted as work records.

All timestamps must include a timezone. Future completion timestamps, duplicate
job IDs, conflicting project/writer routing, conflicting allocation states,
missing stable allocation identity, invalid JSON/schema, or unreadable storage
suppress dispatch. Missing optional allocation files mean no reported external
work. Missing jobs block their writers. Unknown scheduler states block their
writers. Allocation records do not expire by inference: a reported active
record suppresses the corresponding writer until its producer reconciles it.

## Mutation and failure boundaries

There is no CLI, import-time I/O, installation, timer, or scheduler registration.
`run(home, ...)` requires an explicit store. Importing the module cannot start
work. Dry runs produce a proposed selection without changing jobs or consuming
cooldown; they may create the runtime directory and lock file.

A nonblocking POSIX OS lock serializes watchdog invocations (unsupported platforms
fail closed). An atomic fsynced per-writer
reservation is written before dispatch; failure to reserve prevents dispatch.
An uncertain scheduling failure retains that reservation, suppressing another
attempt throughout the cooldown. This is bounded duplicate suppression, not
exactly-once execution or a distributed transaction. After cooldown expires,
fresh job/allocation evidence determines eligibility again.

The real trigger uses the scheduler's context-local store and native job lock
for reread, eligibility check, and scheduling. It never calls the scheduler's
reenabling trigger outside that guard, so an owner pause before the critical
section stays paused; a later pause serializes after it. Scheduling only
advances the due time. The watchdog does not execute an agent, resume a session,
send messages, obtain credentials, or alter a schedule definition.

External work snapshots are metadata evidence, not an atomic reservation shared
with their producers. This implementation cannot guarantee exclusion against a
new external producer starting work concurrently after the snapshot check.
Any future live activation needs an approved common writer-claim protocol,
current project/job mappings, freshness policy, operational thresholds, and
separate owner authorization. No runtime activation is part of this repair.

## Acceptance evidence

The existing ten tests prove the retained behavior. The additional
`test_three_project_watchdog_safety.py` tests exercise a real temporary scheduler
store, pause between selection and dispatch, native lock coverage, subprocess
overlap, durable cooldown after reimport, temporal/schema/routing failures,
reservation failure, inert imports, and dry-run isolation. All scheduler effects
are confined to pytest temporary homes.

Run both files using the repository test environment, then run the normal full
core CI. Full CI and independent exact-SHA review remain release gates. Restoring
this dependency alone does not prove the Nova profile repair or deployment.
