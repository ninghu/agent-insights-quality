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

Email shows numeric issue/baseline coverage and every excluded unit, without visible Full/Partial
labels or a quality PASS/FAIL threshold. The internal eligibility policy is unchanged: an eligible
result may be a team report; an invalid measurement has no score and is addressed only to the
configured personal recipient. TEST is always private. A measured zero remains a real zero score.

The Outlook-compatible brief presents Summary, What needs improvement, What is working, and a
five-Agent overview before optional private work items. Engine gaps require independent current
evidence; exclusions and unexpected real Agent findings remain separate. No day-to-day improvement
is implied when a comparable prior measurement is unavailable. Detailed unit reasoning is separate
from the brief and uses the same result, not another assessment.

```powershell
python -m agent_insights_quality email-preview --delivery-id <existing-delivery-id>
python -m agent_insights_quality email-preview --delivery-id <existing-delivery-id> --restyle
```

The first command preserves the exact prepared recipient, subject and HTML. Explicit `--restyle`
creates a clearly labelled local presentation preview from that delivery's frozen result, only
when the reviewed unit context still matches. Both export `email.html`, `email.eml`, `report.html`
and a provenance manifest to a content-addressed directory under the private Daily `previews/`
folder. The restyled EML attaches the detailed report; browser HTML links to its adjacent file.
Neither command claims, sends, changes the original request, invokes providers or publishes data.
EML is marked unsent, not delivered. Normal runtime ownership applies; do not bypass an active
runner's lock to export a preview.

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
Each attempt's setup and verification turns remain ordered. Travel is currently limited to one
attempt at a time because its graph-wide booking ledger is shared; staging remains serial per target.
Completed immutable evidence/card snapshots enter the four-worker assessment pipeline immediately,
while other safe lane work continues. Final aggregation waits for both execution and assessment.

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
