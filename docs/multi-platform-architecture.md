# OpenAgent Bridge — Universal Multi-Platform Architecture

## 1. Upgrade Objectives & Non-Negotiables

OpenAgent Bridge has been upgraded from a local runner to a **Universal Multi-Platform Autonomous Harness**. It operates independently of any proprietary frontend (such as Muse), exposing standardized ingress protocols that any chat platform or AI assistant can interact with.

### Preserved Core Closed Loops:
1. **Meta-Prompt Loop**: Automatic prompt rendering, offline benchmark evaluation, and strict promotion gates without altering the immutable `original_goal`.
2. **Engineering Loop**: Sandboxed execution in Git Linked Worktrees (`.openagent/worktrees/<task-id>`), deterministic acceptance verification, structured failure signature computation, stall detection, bounded repair attempts, and safe rollback.
3. **Self-Evolution Loop**: Continuous reflection on task historical metrics (success rates, common failure signatures, bottleneck frequencies), rule optimization proposal generation, safety boundary validation, and strict human approval gating before harness updates are applied.

---

## 2. Multi-Platform System Architecture

```
                  ┌──────────────────────────────────────────────┐
                  │ Users, Mobile Bots, Remote AI Assistants     │
                  └───────┬──────────────────────────────┬───────┘
                          │ (Telegram, Discord, Slack)   │ (REST/HTTP)
                          ▼                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        Channel Adapter Layer                           │
│  • TelegramBotAdapter  • HTTPChannelAdapter  • Skeletons (Discord/Lark)│
└─────────────────────────────────┬──────────────────────────────────────┘
                                  │ UnifiedTaskRequest
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        OpenAgent Bridge Core                           │
│  • RBAC Access Control Engine (User/Project Whitelisting)              │
│  • Persistent SQLite Database (Tasks, Projects, Events, Approvals)     │
│  • Atomic Task Queue with Heartbeat Leases & Crash Recovery            │
└─────────────────────────────────┬──────────────────────────────────────┘
                                  │ Claimed TaskSpec
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│                 Autonomous Engineering Repair Loop                     │
│  • Git Linked Worktrees (.openagent/worktrees/<task-id>)               │
│  • Subprocess Runner (shell=False, process-tree cleanup, redaction)    │
│  • Codex / Claude Code Execution Adapters                              │
│  • Deterministic Verifier (Acceptance criteria, forbidden paths)       │
│  • Progress Evaluator (Stall detection on 2 consecutive signatures)    │
│  • Hard Budget Bounds (Wall-clock, iterations, invocations)            │
└─────────────────────────────────┬──────────────────────────────────────┘
                                  │ Verification & State Changes
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        Channel Feedback Router                         │
│  • Outbound Async Notifications back to caller's original chat channel │
│  • Real-time Task Status, Failure Reports, and Delivery Cards          │
│  • Interactive Approval Prompts for Sensitive Actions                  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Security & Multi-Tenant Isolation Guarantees

1. **Authentication Gate**: Every inbound message must authenticate against known user IDs or whitelisted chat IDs. Unrecognized users receive an explicit permission denial and are barred from submitting tasks.
2. **Zero Shell Exposure**: Chat messages are never interpreted as raw shell commands. They are parsed into structured `UnifiedTaskRequest` objects that only specify the target project and engineering goal.
3. **Project Authorization**: Users can only access projects explicitly listed in their `allowed_projects` profile.
4. **Approval Gate for High-Risk Actions**: Actions such as `MERGE_MAIN`, `DEPLOY_PROD`, and `HARNESS_SELF_UPDATE` cannot proceed autonomously. They require explicit human approval via `/approve <id>`.
5. **Kill Switch & Crash Safety**: The physical `STOP` kill switch pauses execution immediately. Stalled or crashed worker tasks with expired leases are recovered automatically by `openagent recover`.
