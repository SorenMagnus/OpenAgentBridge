# Muse & Remote Transport Integration Audit Report

**Date**: 2026-11-18  
**Scope**: Connectivity, Protocol Capabilities, and Security Gateways for Meta Muse (or Generic Orchestrators) to OpenAgent Bridge.

---

## 1. Transport Capability Audit Matrix

| Transport Mechanism | Status | Security Boundary | Recommendation |
|---|---|---|---|
| **Local File Queue (`ManualFileTransport`)** | **VERIFIED** | Local filesystem ACLs | Default air-gapped / offline protocol |
| **Local Loopback HTTP (`LocalHTTPTransport`)** | **VERIFIED** | Bound exclusively to `127.0.0.1` | Local automation & connector integration |
| **Restricted SSH (`SSHCommandWrapper`)** | **VERIFIED** | `ForceCommand` restricts strictly to `openagent` CLI; arbitrary shells blocked | Permitted when host OpenSSH is enabled |
| **Muse Outbound SSH Direct Connect** | **UNVERIFIED** | External VM to host port forward | Community cases confirm Tailscale routing, but no official Meta SLA exists; must probe per user |
| **Muse Connector (HTTP REST)** | **UNVERIFIED** | Explicit scopes & tokens | Recommended production route once connector endpoint is registered |
| **Model Context Protocol (MCP)** | **UNVERIFIED** | Tool invocation sandbox | Defer client implementation until Muse runtime capability is verified |
| **Arbitrary Administrative Shell Access** | **UNSUPPORTED** | Prohibited | Violates security model; never grant admin shell to AI agent |

---

## 2. Inbound Connection Policy

When an external orchestrator (e.g. Meta Muse) communicates with the user's host:
1. **Never grant unrestricted shell access** (`cmd.exe`, `powershell.exe`, or `/bin/bash`).
2. If using OpenSSH:
   - Configure a dedicated unprivileged user account.
   - Attach `ForceCommand openagent ...` in `sshd_config` or `authorized_keys`.
   - Disable PTY allocation, agent forwarding, and TCP port forwarding (`no-pty,no-port-forwarding,no-X11-forwarding`).
3. If using HTTP Connector:
   - Must bind only to localhost (`127.0.0.1`) or a secure tunnel overlay (Tailscale node IP).
   - Enforce idempotency keys and request validation.

---

## 3. Fallback Hierarchy

```
   ┌──────────────────────────────────────────────┐
   │ Muse Native Connector / Verified Outbound    │
   └──────────────────────┬───────────────────────┘
                          │ If unavailable
                          ▼
   ┌──────────────────────────────────────────────┐
   │ Restricted OpenSSH (`openagent` CLI only)     │
   └──────────────────────┬───────────────────────┘
                          │ If unavailable
                          ▼
   ┌──────────────────────────────────────────────┐
   │ Loopback HTTP Transport (`127.0.0.1:8765`)   │
   └──────────────────────┬───────────────────────┘
                          │ If air-gapped / offline
                          ▼
   ┌──────────────────────────────────────────────┐
   │ Manual File Queue (`transport/inbox/`)       │
   └──────────────────────────────────────────────┘
```
