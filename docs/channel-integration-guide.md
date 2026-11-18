# OpenAgent Bridge — Multi-Platform Channel Integration Guide

This guide documents how to connect external communication platforms (Telegram, Discord, Feishu, Slack, and HTTP Webhooks) to OpenAgent Bridge.

---

## 1. Architectural Overview

```
User / Mobile Bot / External Agent
               │
               ▼
┌───────────────────────────────┐
│     Channel Adapter Layer     │
│ (Telegram / Discord / HTTP)   │
└──────────────┬────────────────┘
               │  UnifiedTaskRequest
               ▼
┌───────────────────────────────┐
│       Bridge Core (RBAC)      │
│   (Authentication & Queue)    │
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│  Autonomous Engineering Loop  │
│  (Codex / Claude / Runner)    │
└──────────────┬────────────────┘
               │  Deterministic Verification & Events
               ▼
┌───────────────────────────────┐
│   Channel Router Feedback     │
│ (Real-time Async Notifications)
└───────────────────────────────┘
```

---

## 2. Telegram Bot Channel

### 2.1 Verification & Network Check
The host's network connectivity to `https://api.telegram.org` was audited and verified (HTTP 200). If operating in regions requiring a proxy, configure `channels.telegram.proxy_url` (e.g. `http://127.0.0.1:7890`).

### 2.2 Configuration (`config.yaml`)
```yaml
channels:
  telegram:
    enabled: true
    bot_token: "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
    allowed_user_ids:
      - 123456789     # Your Telegram numerical user ID
    allowed_chat_ids:
      - 123456789     # Private chat ID or authorized group ID
    poll_interval_seconds: 1.0
    proxy_url: ""     # Optional: "http://127.0.0.1:7890"

auth:
  enabled: true
  users:
    - id: "telegram:123456789"
      name: "Owner Mobile"
      role: "admin"
      allowed_channels: ["telegram"]
      allowed_projects: ["*"]
```

### 2.3 Bot Commands
- `/start` or `/help` — View available commands and connectivity status.
- `/projects` — List registered code repositories you have access to.
- `/task <project_id> <goal>` — Dispatch an engineering task directly from your phone.
- `/status <task_id>` — Query live task state, iteration count, and active branch.
- `/report <task_id>` — Review deterministic test pass/fail breakdown.
- `/cancel <task_id>` — Abort task execution gracefully.
- `/approve <approval_id>` — Authorize sensitive operations (main branch merge / deploy).
- `/reject <approval_id>` — Reject sensitive operations.

---

## 3. Universal HTTP / REST Channel

The HTTP Channel allows any AI assistant, Web UI, or remote server to dispatch tasks over JSON.

### 3.1 Configuration
```yaml
channels:
  http:
    enabled: true
    host: "127.0.0.1"
    port: 8765

auth:
  api_tokens:
    - "sk-bridge-secret-token"
```

### 3.2 Key Endpoints
- `GET /health` — Service health check (unauthenticated).
- `GET /api/v1/projects` — List registered projects. Header: `Authorization: Bearer <token>`.
- `POST /api/v1/tasks` — Submit task.
  ```json
  {
    "project_id": "demo_repo",
    "goal": "Implement user authentication with JWT",
    "acceptance": {
      "commands": [["pytest", "tests/test_auth.py"]]
    }
  }
  ```
- `GET /api/v1/tasks/<task_id>` — Retrieve task runtime state.
- `GET /api/v1/tasks/<task_id>/events` — Query event sequence log.
- `POST /api/v1/tasks/<task_id>/cancel` — Cancel task.
- `POST /api/v1/approvals/<approval_id>/resolve` — Approve (`{"approved": true}`) or reject.

---

## 4. Discord, Feishu, and Slack Skeletons

- **Discord**: Implemented in `src/openagent_bridge/channels/discord_bot.py`. Connects via Discord Gateway WebSocket or Interactions Webhook.
- **Feishu (Lark)**: Implemented in `src/openagent_bridge/channels/feishu_bot.py`. Subscribes to `im.message.receive_v1` events and renders Feishu Interactive Cards.
- **Slack**: Implemented in `src/openagent_bridge/channels/slack_bot.py`. Uses Slack Socket Mode or Events API to process `@openagent` mentions and slash commands.
