---
document_id: KB-ENG-002
title: Payment API Incident - 14 August 2026
department: engineering
classification: internal
updated_at: 2026-08-18
---

Impact: Payment confirmation was delayed for 18 minutes. About 7.4% of checkout attempts were affected; no duplicate charges were confirmed.

Root cause: A connection-pool limit was not updated after worker scaling.

Resolution: The pool limit was increased and stale workers were restarted.

Actions: Add pool saturation alerts, include the setting in load tests, and complete the runbook update by 30 September 2026.
