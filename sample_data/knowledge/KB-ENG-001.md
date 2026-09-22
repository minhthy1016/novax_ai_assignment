---
document_id: KB-ENG-001
title: Production Deployment Procedure
department: engineering
classification: internal
updated_at: 2026-08-30
---

1. Confirm CI checks and code review are complete.
2. Create a release tag and obtain Engineering Manager approval.
3. Deploy to staging and run smoke tests.
4. Schedule production deployment in the approved window: Tuesday or Thursday, 21:00-23:00 MYT.
5. Monitor error rate, p95 latency and queue depth for 30 minutes.
6. Roll back if error rate exceeds 2% for five consecutive minutes.
7. Record the release and outcome in the change log.
