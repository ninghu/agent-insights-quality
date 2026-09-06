# Travel session preparation canary

`staging_travel_session_lookahead` is a strict integer **0 (default, off)** or **1**.
It exercises the existing bounded Travel session-preparation controller inside normal
incremental staging. It is separate from `daily_travel_session_lookahead`, which also
defaults to **0**. Neither setting enables the other.

For an explicitly authorized upcoming fresh staging selection, the operator can set
these public-safe fields in the existing `config/runtime.json` input, preserving other
approved settings:

```json
{
  "staging_travel_session_lookahead": 1,
  "daily_travel_session_lookahead": 0,
  "daily_attempt_budget": 10
}
```

This file is **not ignored**: the existing source-integrity check includes `config`.
The parent must include the approved public-safe override in the reviewed, committed
candidate before launching; do not bypass that check with an untracked file or an
ignore rule. No configuration file is created by this feature.

Do this **before** initializing that staging run. Use the normal committed-candidate
worktree, its `src` on `PYTHONPATH`, and the existing `run-staging` command.
No separate traffic command, warmup, extra scenario, `--full`, or invented run identity
is required. The changed shared invocation/trace-wire dependencies already select all
affected reviewed traffic; unchanged Agent deployment inputs can reuse native objects
and artifacts.

The trial requires at least one selected Hosted Travel unit whose traffic belongs to
the current run. Otherwise it stops with `staging_session_trial_requires_fresh_travel`
before target provider calls. Inherited traffic is not retagged as a trial. A mixed
selection keeps inherited units on their original serial preparation path.

## Scope and recovery

Within each already activated Travel target, prepare at most the next native session
while the current attempt executes its ordered setup/probe turns. **Business calls
remain strictly serial.** Staging targets use their existing per-version Agent names;
there is no cross-version preparation or new activation policy. Other Agents and
Prompt targets keep serial attempts.

The trial uses the existing `daily_attempt_budget` (strict 1–10) as its global
whole-attempt ceiling. All staging attempts share that ceiling while the trial is on,
including serial peers. Current/prepared attempts retain permits through preparation,
waiting and business completion. Budget 1 is safe and serial, but cannot demonstrate
overlap. Default-off staging retains its original scheduling.

Policy is immutable in the run's `completed/staging-session-lookahead.json`.
Resume restores it, even if the current configuration differs. Existing runs without
that record freeze **0**, and their private staging summary reports
`session_preparation_trial: requested=1, frozen=0, acceptance=not_exercised` if 1 was
requested too late. Completed phases are reused on same-source resume; never repeat
completed staging merely to manufacture acceptance or favorable latency.
The CLI's completed-run fast path rejects a late request for an old off/missing
policy with `staging_session_trial_frozen_off`; its empty-selection fast path also
rejects a requested trial rather than claiming it ran.
The separate `daily-session-lookahead` records and existing Daily behavior are unchanged.

Version affinity is saved before session POSTs. Stable session/request identities,
known-rejection retry bounds and unknown/202 reconciliation rules are unchanged.
Unknown business stops further business for that target; at most the already-in-flight
next preparation can finish. Cancellation and fatal checkpoints drain started HTTP
threads before permits and runtime ownership are released. No blind retry of an
unpersisted native outcome is authorized.

## Acceptance evidence, not a new qualification policy

Recovered upstream source creates a new, version-bound native session without a user
task/model invocation and without explicitly stopping another session. That source
is **not** proof of the serving binary or runtime no-interference. Preparation can
start a container, execute startup code, reserve quota and consume resources. It is
not free or equivalent to a business request.

Staging still runs exactly ten canonical attempts per target, captures the original
raw evidence and invokes normal Sol qualification with the reviewed eight-observation
policy. Daily readiness remains six. No engine Insights run, paired baseline traffic,
extra model assessment, score, publication or email is added.

After the normal staging run, use its **actual resolved run ID** in this read-only
command from the integrated candidate:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m agent_insights_quality.preparation_audit --run-id $runId
```

It reads only existing staging artifacts and prints aliases/counts, not native
session/request IDs. It neither constructs providers nor acquires ownership, writes,
queries telemetry, invokes Agents/models, or enables settings.

Review:

- Frozen trial policy 1 and current-run-owned Travel traffic, not inherited results.
- Ten unique ready sessions per Travel unit, matching provider-version affinities,
  and all planned turns bound to the correct attempt session.
- Ordered completed business receipts and per-target observed business peak at most 1.
- Actual next-session preparation/invocation overlap; the global whole-attempt peak
  stays within the recorded budget. No overlap at budget 1 is expected, not acceptance.
- Original-plan counts, attributable probe roots, completed raw Sol qualification and
  retained setup/continuation evidence demonstrating no host/booking-state interference.
- All performance segments, including resumed segments. Missing/truncated/unfinalized
  segments, unobserved turns or failed preparation are gaps, not zero-latency successes.

Performance intervals are overlapping adapter wall time, not pure service time or
additive run savings. The audit does not decide native-contract acceptance or modify
qualification. Following fresh qualification **and human evidence review**, the parent
may configure the existing `daily_travel_session_lookahead: 1` before the authorized
fresh N7 run. There is no automatic setting change or new approval-record ceremony.
