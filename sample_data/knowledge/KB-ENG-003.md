---
document_id: KB-ENG-003
title: Incident Response Runbook
department: engineering
classification: internal
updated_at: 2026-09-05
source: candidate-added
---

# Incident Response Runbook

This runbook describes how Engineering responds to production incidents, from the first alert to the post-incident review. It applies to every service owned by Engineering.

## Severity classification

Incidents are classified as soon as they are acknowledged. The severity can be raised or lowered at any time as more is learned.

- SEV1: a customer-facing service is unavailable, or payments are failing for more than 5% of attempts. An incident commander must be assigned within 10 minutes.
- SEV2: a customer-facing service is degraded but usable, for example elevated latency or intermittent errors. An incident commander must be assigned within 30 minutes.
- SEV3: an internal tool or a non-critical background job is affected. No incident commander is required; the owning team handles it during working hours.

## Roles

The incident commander coordinates the response and makes the final call on mitigation. The incident commander does not debug; they keep the team focused and decide when to escalate.

The communications lead posts status updates to the status page and the internal incident channel. For SEV1 incidents the status page is updated every 30 minutes. For SEV2 incidents it is updated every 60 minutes.

The scribe keeps a timestamped log of decisions and actions in the incident document. The log is the primary input for the post-incident review.

## Response phases

### Detect

Most incidents are detected by alerts on error rate, p95 latency or queue depth. Anyone who notices customer impact without an alert should open an incident directly; a missing alert is itself a finding for the review.

### Triage

During triage, confirm the impact, assign the severity and page the owning team. Check recent deployments first: a deployment in the previous two hours is the most common cause of incidents.

### Mitigate

Prefer the fastest safe mitigation over a root-cause fix. Rolling back the most recent deployment is the default mitigation when a deployment is suspected. Feature flags may be switched off without approval during a SEV1 or SEV2 incident.

### Resolve

An incident is resolved when customer impact has ended and metrics have been stable for 30 minutes. The incident commander declares resolution in the incident channel.

## Service playbooks

### Payment API

Check connection-pool saturation on the payment database first. If saturation stays above 90% for 10 minutes, escalate to the database on-call engineer. Scaling payment workers without raising the pool limit makes saturation worse, as the August 2026 incident showed.

Roll back a payment API deployment if the checkout error rate exceeds 1% for three consecutive minutes. Payment rollbacks need no additional approval during an incident.

### Search API

Check the search index replication lag first. If cluster CPU stays above 80% for 15 minutes, escalate to the database on-call engineer. Search degradation is usually SEV2 because checkout still works without search.

Roll back a search API deployment if the query error rate exceeds 5% for ten consecutive minutes.

## Post-incident review

A blameless post-incident review is held within five business days for every SEV1 and SEV2 incident. The review records the timeline, the root cause, what went well, and action items with owners and due dates. Action items from SEV1 reviews are tracked weekly by the Engineering Manager until closed.
