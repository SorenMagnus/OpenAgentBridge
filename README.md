# OpenAgent Bridge

> An orchestration bridge that coordinates AI coding agents with structured
> task management, verification, and human-in-the-loop approval workflows.

## Runtime

OpenAgent Bridge stores tasks, leases, runs and approvals in SQLite. Workers
execute agents in task-specific Git worktrees, verify results and record evidence.

## Requirements

- Python ≥ 3.11
- [psutil](https://pypi.org/project/psutil/) (runtime)
- [PyYAML](https://pypi.org/project/PyYAML/) (for config loading)

## Installation

```bash
# Clone the repo
git clone <repo-url> && cd OpenAgentBridge

# Install in editable mode with dev dependencies
pip install -e ".[dev]"
```

## Configuration

```bash
# Copy the example config and edit it
cp config.example.yaml ~/.openagent/config.yaml

# Or point to a custom location
export OPENAGENT_CONFIG=/path/to/my/config.yaml
```

See [`config.example.yaml`](config.example.yaml) for all available options with
inline documentation.

## Environment Variables

```bash
cp .env.example .env
# Then fill in your API keys
```

## CLI usage

```bash
openagent doctor --json
openagent project add /path/to/project
openagent task submit examples/task.example.json --project /path/to/project
openagent task report TASK_ID
openagent events TASK_ID
openagent serve --host 127.0.0.1 --port 8765
```

CLI submission runs synchronously. HTTP submissions use the worker queue.
Set worker.concurrency to 1 (default) or 2. Administrator limits under autonomy
and runner bound each task's requested resources.

## HTTP access and delivery

With authentication enabled, supply a Bearer token or X-API-Key. Map the token
to an ACL user ID with `auth.http_token_users` and allow HTTP for that user.
This keeps credentials out of new task owner IDs. Existing `auth.api_tokens`
entries resolve to ACL IDs equal to the token or prefixed with `http:`.
Keep the configuration private. Project access permits submission. Task results are
visible to the owner and admins. Missing or unmapped credentials return 401.
Inaccessible objects return 404.

Under `/api/v1/tasks/{id}`, use the task URL for status, `/events` for history,
`/report` for verification and `/files/{path}` for bounded UTF-8 previews.
Use `/artifacts` to list registered deliveries and `/artifacts/{artifact_id}`
for binary GET, HEAD and single byte-range requests. Register a regular file
inside the task worktree with `openagent task publish-artifact TASK_ID FILE`.
Publication copies and hashes the bytes. The task report includes available
model and usage data for each run; older or unreported values are null.

## Evaluate prompt guidance

Run this workflow in an isolated data directory. The first and repair calls
share a task contract. Approved guidance is added to that contract.

```bash
openagent prompt bootstrap
openagent prompt propose 2 guidance.txt
openagent prompt inputs 2 cases.json
openagent prompt evaluate 2 cases.json recordings.json
openagent prompt approve 2 --approved-by OPERATOR
```

Use the prompts from `inputs` to collect independent outputs for both versions.
For each version and case ID, record `prompt_sha256` and `output`. Evaluation
replays those outputs against validation and holdout assertions. Promotion
requires at least 20 cases, a validation gain of 0.03, a holdout drop of at
most 0.01, passing critical and security cases, and operator approval.

## Review a task result

The operator can plan a fast-forward into a clean, checked-out target.
Approval binds the target branch and both commit hashes.

```bash
openagent-merge TASK_ID --target main
openagent-merge TASK_ID --target main --request-approval
openagent approve APPROVAL_ID
openagent-merge TASK_ID --target main --execute
```

## Upgrade an existing service

1. Test with a private configuration, temporary database, separate storage,
   unused loopback port and fixture repository.
2. After review and merge, record the service commit, interpreter, launch
   arguments, config and startup method. Check the checkout is clean.
3. Record the pause state, pause new claims and wait for active tasks, leases
   and subprocesses to finish. Stop the idle service while claims are paused.
4. Back up configuration and SQLite with its online backup API or a cold copy
   after all writers have exited. Retain the old executable and commit.
5. Fast-forward to the reviewed release, install with the service interpreter
   and restart using the original configuration. Verify the commit, /health,
   authenticated reads and queue state, then restore the original pause state.
6. If a check fails, stop the new instance before releasing the queue. Restore
   the old release, configuration and matching database backup, restart and
   repeat the same checks.

## License

MIT
