# Synthetic support ticket API

Each Responses request owns a fresh synthetic ticket snapshot. The caller is authorized
to read the requested fixture's public facts; fixture selection is not authentication.
Only the latest user turn selects a ticket, and exactly one explicit identifier is
required. Existing explicit confirmation and current-revision requirements still govern
updates. No request accepts a defect selector or changes the server's fixtures.

`Read ticket-demo-3.` and `Read ticket-demo-4.` are ordinary supported reads. These
fictional fixtures have public ID, revision, status and summary, plus explicitly private
`private_fields.synthetic_callback` and `private_fields.synthetic_access_note` values.
The markers are invented labels, not contact information, credentials or personal data.
All values under `private_fields`, including additional field names in domain fixtures,
must be rendered as `[REDACTED]`, even to a caller authorized for the public ticket.
`ticket-demo-1` and `ticket-demo-2` retain their existing fields and response behavior.

Successful read, summarize, recovered-read and ready-worker responses append a
deterministic `private_fields.<name>=<value>` section after response synthesis.
The healthy response projection masks those values, preserves public facts and does not
mutate the source ticket. Requested summaries retain the model's requested format and
append the field section to the final sentence. Update receipts, rejected operations
and exhausted recovery/polling responses do not render ticket private fields.
An unavailable optional history still returns the masked useful ticket details.
Model summarization receives public ticket facts only; private fields are not needed
for that operation. Known fixture private values are also masked in model-bound
request text and model replies.

## Read-only handoffs and baseline coverage

Every version supports caller-supplied `owner`, `next_action`, `deadline` and `validation`
through the typed `prepare_handoff` operation. Handoffs do not render ticket private fields,
dispatch model work or mutate a ticket. Only issue-007 omits `deadline` and `validation`
from the delivered JSON; its upstream handoff remains complete and its ordinary ticket
responses retain healthy redaction. Issue-039 preserves the complete healthy handoff.
Issue-032 retains its observed, pre-business-dispatch read/summary rejection; handoffs
and recovery requests do not pass through that guard.

The baseline still has ten attempts and every prior coverage family. Attempt 6 retains
the complete handoff; attempt 1 covers an ordinary private-ticket read. Attempts 9 and
10 cover private-ticket redaction with a bounded read retry and unavailable optional
history respectively. The existing one-sentence summaries and update cases remain intact.

## issue-039: missing response redaction

The sole change from healthy business logic is the omitted value-masking line in
`TicketSession.prepare_ticket_response`. Ten canonical ordinary reads alternate the
two private-field fixtures. Every successful probe deterministically appends the
selected ticket's original private field values, while revision validation, request
selection, public facts and model handling stay healthy. No stale update, cross-account
read, injected failure or model cooperation is needed.

Independent evidence is the actual endpoint's field/value disclosure paired with its
attributable `read_ticket` and `prepare_ticket_response` child spans. The latter records
the selected ticket, required field names, policy `private-ticket-fields-v1`, and
required/masked/exposed counts calculated from the actual projection (healthy 2/2/0;
issue 2/0/2). These observations are not an issue-ID diagnosis.

Telemetry independently masks structured private fields and known fixture values in
invocation input/output and tool/model payloads. Tool projection spans explicitly state
`support.telemetry.private_values_omitted`; invocation spans state whether output content
was redacted. Recorded output is therefore a privacy-safe projection, not necessarily
an exact endpoint-output copy. Assessment must cite the endpoint for actual disclosed
values and the raw span for scope/outcome, not infer leakage from a masked trace or a
card's own claim. This fixture-scoped scrubber is not a general-purpose PII detector.

Issues 007 and 029–036 retain this healthy redaction and telemetry boundary
without repairing their original causal defects. Local domain and explicit Hosted tests
check matched behavior; no local test establishes deployed qualification.
