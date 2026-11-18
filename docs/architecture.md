# OpenAgent Bridge — Architecture Specification

## Overview

OpenAgent Bridge is a lightweight local supervisor that manages external Coding Agents (OpenAI Codex CLI, Claude Code fallback) within isolated Git worktrees, governed by deterministic verifiers and strict safety policies.

```
                  ┌────────────────────────────┐
                  │ User / External Controller │
                  │ (GoalSpec, Constraints)    │
                  └──────────────┬─────────────┘
                                 │ TaskSpec JSON
                                 ▼
┌─────────────────────── OpenAgent Bridge ────────────────────────┐
│                                                                 │
│  CLI Intake ──► Idempotency & DB ──► Git Worktree Isolation     │
│                                                │                │
│                                                ▼                │
│  Evaluation & Diff ◄── Deterministic ◄── Agent Execution        │
│          │             Verifier          (Codex CLI exec)       │
│          ▼                                                      │
│    Review Required (Human Approval Gate)                        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Key Architectural Principles

1. **Separation of Control and Execution Planes**:
   External callers (such as Muse or user scripts) submit intent (`TaskSpec`). Bridge decides where, how, with what budget, and under which sandbox to run.
2. **Immutability of `original_goal`**:
   The user's original goal is never rewritten or mutated by the agent.
3. **Deterministic Ground Truth**:
   The Agent's text output ("I fixed the bug", "All tests pass") is treated as untrusted metadata. Only actual command execution exit codes and filesystem checks determine pass/fail.
4. **Git Worktree Isolation**:
   The main repository worktree is never modified directly. Every task executes in its own isolated worktree (`.openagent/worktrees/<task-id>`) on a dedicated branch (`openagent/<task-id>`).
5. **No Shell Injections**:
   All subprocess invocations use explicit `argv: list[str]` and `shell=False`.
6. **Crash Resilient SQLite WAL**:
   Single-source of truth state stored in a local SQLite database using Write-Ahead Logging.
