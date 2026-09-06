# Operations

Use an authenticated local environment. Keep configuration and generated private artifacts under
`$HOME\.aiq-runtime\agent-insights-quality\`, never in the repository.

Set `PYTHONPATH` to the active source checkout before repository Python commands. Official automation
uses a fresh latest-main worktree; a private trial uses the candidate being evaluated.

## Entry points

```powershell
python -m agent_insights_quality validate
python -m agent_insights_quality generate-docs
python -m agent_insights_quality run-staging
python -m agent_insights_quality run-staging --full
python -m agent_insights_quality run-daily --test-run --rerun 1
python -m agent_insights_quality status --profile staging
python -m agent_insights_quality status --profile daily
```

`generate-docs` updates `AGENT_CATALOG.md` and `ISSUE_CATALOG.md`; it never rewrites traffic
or expected behavior. Runtime commands now use the replacement runner; there is no legacy
fallback. Production readiness still requires the candidate's deployed trial and TEST email.

## Recovery

For an explicitly requested new private measurement, use a new nonzero `--rerun` identity
with `--test-run --fresh-traffic`. This bypasses previous-trial traffic reuse without deleting
earlier evidence. Repeating the same run resumes its frozen intent and completed checkpoints,
even when the flag is omitted; it does not generate another fresh measurement. An unfinished
fresh run cannot silently switch source. Ordinary official new-day runs remain fresh without
this private-only flag.

Repeat the same command to resume matching work. Do not manually invent generation IDs, clear
state, delete Agent objects or resend completed traffic. A code/scenario change selects affected
work; unchanged completed results retain their original provenance.

An issue-catalog entry-only edit re-evaluates that issue when Git proves the inventory, Agent
assignment and global catalog settings are unchanged. Unknown comparisons, inventory changes and
shared verifier changes remain conservative. Expectation-only traffic edits reuse matching raw
evidence; an explicit execution safety cap is not a substitute for the reviewed early-stop guard.

Staging resumes the same source and selection mode across midnight, retaining its original run
date and completed calls. Repeating a completed full run is a no-op. Only an explicitly requested
fresh full exercise uses `run-staging --full --new-run`; it cannot replace an unfinished full run.

Preserve pending deployment/session/Insights records after an interrupted request. Unknown accepted
POSTs are not safe to repeat. Native Insights submission keys and their exact request bodies are
reused when supported. A definitively rejected submission gets a fresh key and recomputed lookback;
uncertain window coverage is excluded from Engine scoring, not counted as a missed issue.
A new email delivery test uses an explicit nonzero rerun identity; an
ambiguous previous send is reconciled, never sent again blindly.

## Email presentation and local review

Email shows the quality score, expected/detected/missed issue counts, Noise and Duplicate.
It has no baseline-coverage row, always-on exclusion table, score-change note or visible
Full/Partial/PASS/FAIL labels. Actual exclusions have a conditional notice linking to their
Agent's human-validation detail; all of each excluded unit's counts remain excluded.
The eligibility/scoring policies are unchanged. An invalid measurement has no quality score
and is addressed only to the personal recipient. TEST is private; measured zero is valid.

The Outlook-compatible brief presents Summary, What needs improvement, What is working, and a
five-Agent table, then optional private Quality work-item tables. Agent | Findings |
Human Validation | Assigned To are the only Agent-table columns. Assignments come from the
reviewed `catalogs/AGENT_CATALOG.yaml` owner fields, separately from the frozen measured unit
contracts. Foundry links use saved Daily Sweden environment and actual deployment object names;
missing or conflicting metadata produces an explicit missing-link notice, never an API URL or
old-region fallback. No catch-all Run notes, Other findings, methodology body or Run reference
section is inserted, including hidden private context.

The authoritative detail is `report.md`: one table per Agent with stable Agent-name anchors and
five rows for the baseline plus four issue versions. Run num, actual Agent version, Expected
insight, Generated insight(s), Assessment and short Notes keep review compact. Each row represents
ten attempts, not one call. New/updated cards are listed with aligned Correct/Noise/Duplicate
labels; unchanged historical cards are omitted. Missed and unscored cases remain distinct.
Detailed evidence and complete judgments stay in the original private artifacts referenced by
the preview manifest, not repeated payload/provenance sections in the MD.
Private detail is rendered through a separate boundary; public Markdown receives only approved
aliases and reviewed catalog context, never actual private card titles or provider versions.
An `unexpected_real` result is not an automatic Agent fix task. Human validation distinguishes
an actionable defect, an ambiguous claim needing confirmation, and already-handled behavior
requiring no Agent change. This presentation never changes retained judgments or scores.

```powershell
python -m agent_insights_quality email-preview --delivery-id <existing-delivery-id>
python -m agent_insights_quality email-preview --delivery-id <existing-delivery-id> --restyle
python -m agent_insights_quality email-preview --delivery-id <existing-delivery-id> --restyle --scoring-revision <published-40-character-commit>
```

The first command preserves the exact prepared recipient, subject and HTML. Explicit `--restyle`
creates a clearly labelled local presentation preview from that delivery's frozen result, only
when the reviewed unit context still matches. Both export `email.html`, `email.eml`, `report.md`,
`report.html` and a provenance manifest under the private Daily `previews/` folder.
The MD is the EML's actual `text/markdown` attachment. `report.html` is a browser view derived
from that same MD, not an independent report. Browser email links to its per-Agent anchors;
EML tells readers which heading to open in the attachment, without broken relative/cid/file links.
Exact export preserves the original email HTML bytes, even when the original presentation is
obsolete. A legacy request without frozen inputs gets an explicit unavailable-detail MD notice,
not invented counts. The manifest identifies authoritative/derived files, attachment names,
hashes, retained assessment references, assignment provenance and link blockers.
Neither command claims, sends, changes the original request, invokes Agent/Sol/Insights or publishes data.
EML is marked unsent, not delivered. Normal runtime ownership applies; do not bypass an active
runner's lock to export a preview.

The preview CLI returns `report_markdown_path`, `report_html_path`, `email_html_path`,
`email_eml_path`, `manifest_path` and `blockers`, alongside the content-addressed presentation ID.
New delivery preparation also durably saves private `artifacts/presentation/report.md` and its
evidence-reference checkpoint before freezing delivery inputs. Daily status exposes
`private_report_markdown_path` and `presentation_blockers`. Existing prepared requests return
unchanged. Native HTML handoff does not claim to attach a report: absent a delivery-available
detail URL it discloses that the link is unavailable; local EML export supplies the attachment.

Summary's **How Scoring Works** row links only to a verified, immutable GitHub version of
`docs/QUALITY_BAR.md`. It never assumes `main` has the current formula. `--scoring-revision`
performs a bounded public read and requires exact equality with the reviewed local document
(apart from CRLF/LF). An unpublished, malformed or stale version is rejected, not linked.
For new preparation or restyle without an existing receipt, an operator can provide
`config/report-links.json` under the private runtime root containing
`{"scoring_revision":"<published-40-character-commit>"}`. Successful verification is frozen
with the presentation; resume uses the retained receipt, not a mutable branch.
Without a correct published version, `scoring_link_publication_required` is an explicit
publication blocker and the row says the link is pending. This does not block eligible inline
email, authorize publication, change credentials, or publish TEST content. Optional generated
report files/PR requests are not proof that a detailed GitHub report URL is available.

New work-item tables include Type and use a frozen provider-as-of snapshot. Closed items cover
the interval since the previous successfully submitted eligible official report's snapshot;
TEST, failure notices, unsent requests and ambiguous sends do not advance that boundary. With
no prior boundary, an explicitly labelled initial seven-day view is used. The configured Quality
query still defines scope. An unavailable optional query is not an empty result and never blocks
an otherwise eligible email. Legacy local previews retain their original recorded day and display
unrecorded Type/cutoff fields honestly; they do not refetch or invent a newer reporting window.

## Provider and checkpoint recovery

New Insights polling has a separate `insights_poll_timeout_seconds` budget (default 1200).
Deployment polling retains `poll_timeout_seconds` (default 600); trace hydration is separate.
A local wait timeout is not a native failed run. Resume queries the original accepted operation
before deciding its outcome, without rewriting its existing deadline or creating another analysis.

Local `runner.log` and `events.jsonl` preserve starts, heartbeats, retries and outcomes across
restart. They work without ADX. Raw evidence is kept separately. Status reads do not take the
writer lock or perform live work.

New Daily runs may select an assessor through private `config/daily-assessment.json`, with exactly
`deployment_name`, `model`, `model_version` and `credential` (`azure_cli`). Staging continues to
use private `config/assessment.json` or the Sol defaults. Existing runs resume their frozen
assessment settings, regardless of changes to those files. A new run with a different assessor
reuses matching execution evidence where applicable, but creates new judgments and records the
configured model identity; it never relabels earlier judgments from another model.

Daily assessment has a bounded `daily_assessment_max_payload_bytes` setting (default 4,000,000;
maximum 8,000,000). Oversized raw inputs use the existing lossless reference encoding; no evidence
is truncated to fit. A package that still exceeds the configured budget remains unscorable.
This byte budget does not establish the deployed model's token/context limit: native rejections
and missing evidence remain explicit assessment failures, not successful measurements.

## Interactive long-running work

During an interactive rollout, a nested app session can own the entire staging or private Daily
runner command while its parent monitors progress and coordinates fixes. Python still owns all
internal orchestration; do not create per-Agent orchestration sessions.

Monitor `QualityOperationsV1` by the exact `FrameworkRunId`, using local checkpoints as the
authoritative outcome and recovery state. An idle app session or an old heartbeat is not proof
that its runner completed. After an app restart, confirm whether the local process survived
before resuming the same command under normal runtime ownership; never bypass a lock or start
a second writer. Nested sessions do not guarantee process survival across app restarts.

Independent repair work can proceed in separate worktrees without modifying the running candidate.
Integrate repairs after the active run ends, then let incremental selection choose affected units.

## Private performance observations

Daily keeps five Agent lanes and sequential versions within each lane. Independent attempts
use up to `daily_attempt_workers` (default 4), under the shared `daily_attempt_budget` (default 10).
Each attempt's setup and verification turns remain ordered. Travel business attempts remain
serial because its graph-wide booking ledger is shared; staging remains serial per target.
Completed immutable evidence/card snapshots enter the four-worker assessment pipeline immediately,
while other safe lane work continues. Final aggregation waits for both execution and assessment.

`daily_travel_session_lookahead` is an opt-in integer setting: **0 (default, off)** or **1**.
It is not enabled by the four-attempt setting. For a new Daily run, 1 permits preparation of
only the next native session within the already activated Travel version while the current
attempt executes. Travel business requests remain strictly serial; no booking fixture, Agent
source, request body, attempt count or staging behavior changes. Retained traffic owned by
another run keeps the existing serial path. The option is frozen per run; resume restores it
even if configuration defaults change, and legacy runs remain off.

The adapter sends only a version-bound session-creation request, not an Agent invocation.
Existing fake-wire and local Hosted response tests do **not** establish that the platform's
session creation cannot restart or alter another active session's host/ledger. Keep this option
off until that contract is established by controlled acceptance. No additional traffic, test
run or warmup is implied by adding the option.

Current and prepared attempts both hold the global attempt permit through completion.
At most one session is ahead; budget 1 degenerates to serial preparation without deadlock.
An uncertain business response stops further Travel business calls in that version, while
already submitted preparation is drained and checkpointed. Unknown session POSTs are never
repeated blindly. Fatal checkpoint failures cancel and drain both workers before ownership
is released, including already-started threaded HTTP sends through repeated cancellation.
An unpersisted session response remains unresolved rather than authorizing a retry.
Private `attempt_phase` measurements separate `session_preparation` and
`business_execution`; `wait:prepared_session` records waiting for the preceding attempt.
Whole-attempt time includes those intervals and is not pure service time or additive wall time.

After six attributable verification attempts, Daily can continue batched evidence collection for
`daily_evidence_grace_seconds` (default 30), within the existing hydration deadline, to allow late
invocation anchors to arrive. It stops earlier when all completed verification responses have
anchors. Expiry does not raise readiness to ten or require complete child trees: six remains the
minimum, and any essential gaps are disclosed. Grace deadlines survive restart.

Live commands return `performance_path` when the private performance artifact was saved.
It identifies one process segment under the run's `artifacts/performance/` directory.
Resuming creates a separate segment instead of rewriting earlier measurements; bounded batches
and progress records retain observations during a long run. An unfinished segment is not proof
of completed execution, and an uncheckpointed tail may be unavailable after abrupt termination.

Use stage and lane timings to locate the critical path, queue timings for existing concurrency
limits, adapter/HTTP timings for awaited calls, and hydration/poll/backoff timings for waits.
These intervals nest and overlap: their sums are not the run's wall time or pure service latency.
The segment begins after source and plan selection; earlier startup remains in command-status logs.
Reused or skipped work has a null duration, not an instantaneous fresh execution. Active peaks
describe the existing concurrency, not a changed limit.

Sol token totals use actual returned usage only, with known-response counts and null values when
usage is unavailable. Payload bytes are not a token estimate. Observations contain no raw prompts,
responses, credentials or provider identifiers and are not sent to ADX, including in TEST mode.
Measurement-persistence failures surface as warnings without rerunning qualification; primary
side-effect checkpoint failures remain fatal. Performance data never affects quality scores.

## Environment boundaries

Staging uses `aiq-staging-swedencentral`; Daily uses `aiq-daily-swedencentral`. Both reuse Agent
objects and separate g30 telemetry. The canonical deployment registry is in the dedicated
Sweden private `deployment-registries` blob container. There is no legacy-region/storage fallback.

Staging never runs Agent Insights, scores Daily cards or sends a team report. It may emit safe
operational events. An email-only Daily test writes private evidence/previews/logs and sends only
to the configured private recipient: no public report, ADX writes or generated PR.

When blocked by access, a provider capability or an ambiguous outcome, keep checkpoints and surface
the specific decision needed. Continue unrelated safe work rather than hiding the blocker or
lowering the evidence bar.
