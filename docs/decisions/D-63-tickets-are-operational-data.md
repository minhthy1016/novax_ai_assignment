# D-63: A ticket is operational data — readable by a tool, never from the documents

*Decision record. System overview: [`../../README.md`](../../README.md) · engineering architecture: [`../../architecture.md`](../../architecture.md) · index: [`README.md`](README.md).*

**Context.** Found while using the console: the assistant could open a ticket, but the
ticket then existed nowhere a person could see it — the reply was "Ticket INC-1051 is open
with severity medium", with no title and no details, and asking *"how should ticket INC-1051
be solved?"* was routed to **knowledge**, where the model answered from the generic severity
guide and attributed a severity to a ticket the document has never heard of. A real citation
on a claim the source does not support is the worst kind of wrong answer.

**Decision.**

- **`get_support_ticket(ticket_id)`** is a fifth tool: typed id (`INC-\d{1,10}`), so a model
  cannot turn it into a wildcard or a path.
- **A record named in a message is a tool call, not a question for the documents.** The
  router prompt says so, with worked examples, because the documents describe procedures in
  general and contain no tickets: answering from them describes a ticket nobody wrote.
- **Visibility is a record-level rule, not a permission.** Holding `ticket:create` means you
  may raise tickets, not read everyone's. A ticket is visible to the person who raised it
  and to their department — the people who would work it. Handlers can now raise
  `ToolDenied` for this class of refusal, which is audited as a denial like any other.
- **`create_support_ticket` returns what was stored** (title, severity, status, details),
  not just an id, and the reply renders it — a person must be able to see what they filed.
- **The requester's own words are kept.** The model writes `details` from the conversation
  and will flesh out "create a ticket for a post-incident review" into a paragraph that
  reads like fact. The verbatim request is appended (`Raised from: "…"`), never replaced, so
  whoever works the ticket can compare the summary with what was actually asked.
- **`GET /api/tickets`** applies the same rule, so the console can list tickets without
  inventing a second policy.

**Consequences.** Tickets stay out of the knowledge index entirely: they are not documents,
they have no classification and no version, and indexing them would put one department's
operational text into retrieval. The cost is that a ticket is only reachable by id or
through the list, which is the correct trade for data that changes and belongs to someone.

**Alternatives considered.** Indexing tickets as documents (fast to demo, wrong: it makes
operational records retrievable text with no owner and no lifecycle); a `ticket:read`
permission (the sample data grants no such permission, and inventing one would have hidden
the real question — *which* tickets, not *whether*); letting the knowledge path answer and
relying on the prompt to abstain (measured in the console: it does not — it answers with a
citation that does not support the claim).

**Tested by.** `test_a_raised_ticket_can_be_read_back_with_its_title_and_details`,
`test_a_ticket_is_not_visible_to_another_department`,
`test_an_unknown_ticket_is_reported_not_invented` (integration),
`test_a_ticket_keeps_the_requesters_own_words`, `test_ticket_id_is_validated_before_any_lookup`
(unit).
