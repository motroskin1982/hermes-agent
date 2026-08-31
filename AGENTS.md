# Hermes Worker Instructions

Use these rules for every Hermes worktree task.

## Safety and ownership

- Work only inside the assigned Kanban workspace and task scope.
- One task has one writer. Never create a detached session, bypass Kanban, or
  resume a session unless the task explicitly authorizes the exact persisted
  identity.
- Do not touch production services, credentials, authentication, jobs,
  deployments, or unrelated files without explicit task authorization.
- Preserve existing staged changes. Stop and report a concrete blocker rather
  than guessing or broadening scope.

## Execution

- Read the task first. Inspect only the files and lines needed for that task.
- Prefer the smallest atomic change. Keep tests focused and report actual
  command output.
- Before completion, report changed files, tests, diff/status, rollback, risks,
  and one next safe task. Do not self-approve review or QA.

## Kanban lifecycle

- Use the injected task/run/claim context and lifecycle tools.
- A context pause, provider failure, missing identity, workspace mismatch, or
  live-writer conflict is a blocker: preserve evidence and stop. Never work
  around it with a manual `hermes --resume`.

## Loading deeper guidance

- Do not preload broad repository architecture or historical guidance.
- Load targeted source documentation only when the assigned task identifies it
  as necessary. The prior full repository guide is recoverable from Git history
  if a separately scoped architecture task needs it.
