# D-35: Team-lead approval for tickets raised by supervised users

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

**Context.** Two new engineers join the AI Platform team:
- Tom (U007), a Data Engineer;
- Jerry (U008), an AI Engineer.

Their team leads are U002 (Engineering Manager) and U005 (System Administrator). Tickets
they raise must be approved by one of those two before they are opened.

Until now, opening a ticket was not a sensitive action: anyone holding `ticket:create`
opened one at once. Approvals were granted by a **permission**: any holder of
`vpn:approve` who is not the requester (D-31). "One of *this person's* team leads" is a
**relationship**, not a permission.

**Decision.**
- **Team leads are data on the user.** `users.team_leads` holds the IDs whose approval that
  user's tickets need (migration 0010). The seed validates that every lead exists and that
  nobody leads themselves. The brief's six users have none.
- **The tool declares the rule, the data decides who it applies to.**
  `create_support_ticket` has `lead_approval = True`.
  - For a requester with team leads, the call becomes a **pending action**.
  - For everyone else it runs at once, as before.
- **The pending action names its approvers.** `pending_actions.approver_ids` records the
  requester's leads **when the action is proposed**. Approval then requires all of:
  - being one of those IDs, instead of holding a permission;
  - being a different person from the requester;
  - an unexpired action;
  - the matching action hash.

  The action still runs once, under a row lock. Everything else in D-31 is unchanged. A
  permission cannot stand in for the relationship, and the relationship needs no
  permission. Actions approved by permission (VPN profiles) leave `approver_ids` empty.
- **What the lead approves is what gets stored.** The requester's own words are added to
  the ticket details before the action hash is computed, not at execution when the request
  is no longer at hand.
- **Visibility.** `GET /api/actions` lists an action to its requester and its named
  approvers. A teammate who is not a lead does not see it.
- **The users.**
  - Tom: `docs:ai_platform`, `ticket:create`.
  - Jerry: `docs:ai_platform`, `server:read`, `ticket:create`, `kb:write:ai_platform`.
  - Both are in the existing `ai_platform` department, and neither is an evaluation actor.

**Alternatives considered.**
- **A new `ticket:approve` permission.** Rejected: it grants approval over *everyone's*
  tickets. The requirement is about *these* people's leads.
- **Making ticket creation sensitive for everyone.** Rejected: it changes the brief's
  behaviour for all users.
- **Resolving the leads at approval time.** Rejected in favour of a snapshot at proposal
  time. A change of team lead should not silently change who may approve an action
  already waiting.

**Consequences.**
- This is attribute-based authorization next to the role-based permissions, which the
  brief allows ("role-based or attribute-based authorization").
- A pending ticket is not deduplicated until it executes. Two identical requests make two
  pending actions, and the ticket handler's 10-minute idempotency applies at execution.
- The chat path pauses and resumes exactly as for a VPN profile. The interrupt now carries
  the tool name, which it previously assumed was `create_vpn_profile`.

**Tests.**
- Unit:
  - `test_named_approvers_replace_the_permission_check`;
  - `test_a_pending_ticket_is_summarised_by_what_it_opens`;
  - the seed checks for unknown and self-referencing leads.
- Integration (security): `test_a_ticket_from_a_supervised_user_waits_for_one_of_their_team_leads`.
  A colleague, a teammate and the requester are all refused; U005 approves; a second
  approval is refused.
- Regression: `test_a_ticket_from_a_user_without_team_leads_still_opens_at_once`.
- Chat path: `test_a_supervised_user_raising_a_ticket_in_chat_gets_a_pending_ticket`.
