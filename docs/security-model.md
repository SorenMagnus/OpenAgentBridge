# OpenAgent Bridge — Security Model

## Threat Model and Defense in Depth

OpenAgent Bridge assumes that any AI Coding Agent might experience hallucinations, prompt injection, or generate hazardous code/commands. Therefore, safety guarantees are enforced **outside the model runtime**, at the OS, Git, and Python verification layers.

## Permission Tiers

| Tier | Name | Default Policy | Enforced By |
|---|---|---|---|
| **L0** | Observe | Allowed | Read-only inspection of metadata and logs |
| **L1** | Task Worktree Write | Allowed | Git linked worktree (`openagent/<task-id>`) |
| **L2** | Controlled Network | Denied by default | Task policy / sandbox policy |
| **L3** | Main Branch Modification | Human Approval Required | Policy gate, no auto-merge |
| **L4** | Production Deploy / Secrets | Forbidden | Hard policy block |

## Enforced Security Controls

1. **Forbidden Path Scanning**:
   Any modification touching `.env*`, `*.pem`, `*.key`, `~/.ssh/`, `secrets/`, or `credentials/` immediately fails the verification check and blocks task progression.
2. **Dangerous Command Blacklist**:
   Destructive commands (`rm -rf /`, `del /s`, `format`, `diskpart`, `sudo`, `runas`, `git push --force`, `git reset --hard`) are intercepted and rejected prior to execution.
3. **Secret Redaction**:
   Console logs and artifact files pass through regex redaction patterns targeting API keys (`sk-...`), bearer tokens, and private key PEM blocks.
   *(Note: Redaction is an additional layer of defense and does not substitute for strict access controls).*
4. **Local Kill Switch**:
   Placing a `STOP` file inside the data directory (`<data_dir>/STOP`) immediately prevents claiming new tasks and pauses active loops.
5. **No Blind Shell Execution**:
   All commands are represented as structured `argv: list[str]` vectors. No shell string interpolation is permitted.
