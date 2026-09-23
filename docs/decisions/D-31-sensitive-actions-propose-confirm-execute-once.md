# D-31: Sensitive actions: propose, confirm, execute once

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

- A sensitive tool never executes on the requester's turn. It becomes a **pending action**
  with a stable id and an **action hash** over the exact validated arguments plus requester.
- Approval requires the approve permission, a **different person**, an unexpired action, and
  the **matching hash** in the request body, so an approver can only confirm what was
  proposed.
- Execution is guarded by the pending row's status under a row lock: a second approval
  reports "already executed" instead of creating a second profile.
- The requester's permission is re-checked at execution: losing `vpn:create` while waiting
  rejects the action.
