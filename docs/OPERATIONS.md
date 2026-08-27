# Operations

## Environment model

`daily` and `staging` are isolated Foundry accounts and profiles. Never point one profile at the
other profile's account, Project, Application Insights resource, deployment registry, monitor, or
private artifact root.

| Profile | Project | Purpose |
| --- | --- | --- |
| `daily` | `agent-insights-quality` | Weekday qualification |
| `staging` | `agent-insights-quality-staging` | Full qualification before promotion |

Both use 90-day telemetry and artifact retention. The shared ADX quality-history database retains
sanitized daily metrics for 730 days and keeps 90 days in hot cache.

The reviewed telemetry generation is stored only in `config/automation.yaml`; it is not part of the
Agent deployment catalog hash. Increment it only for an explicit human-reviewed clean telemetry
reset; deploy the new generation, update Project connections, then delete the superseded generation.

## One-time deployment

Infrastructure values come only from protected runtime configuration:

The currently signed-in Azure user receives Foundry Project Manager, Monitoring Reader, artifact
storage, ACR push, and ADX Database Viewer and Ingestor access for one-time reviewed provisioning.
Daily execution does not build or push images.

```powershell
python -m agent_insights_quality deploy-infrastructure
python -m agent_insights_quality deploy-analytics
python -m agent_insights_quality provision --profile staging
```

The full infrastructure command includes a two-node production ADX cluster and the
`AgentInsightsQuality` database in `agent-insights-quality-rg`. Use `deploy-analytics` for an
ADX-only deployment that cannot change Foundry models, Projects, telemetry, storage, or registries.
Neither command creates a native ADX dashboard because that dashboard surface has no ARM/Bicep
deployment resource.

Render the private, ready-to-import dashboard file after deployment:

```powershell
python -m agent_insights_quality render-adx-dashboard
```

In the ADX web UI, use **New dashboard** > **Import dashboard from file**, select the rendered file
under `~/.aiq-runtime/agent-insights-quality/dashboards/`, and copy the dashboard share link. Store
the validated link privately:

```powershell
python -m agent_insights_quality configure-adx-dashboard `
  --url "<copied native ADX dashboard share link>"
```

The rendered file and share link contain private Azure context and must never be committed.

Provisioning creates five Agents, 41 immutable versions, and five disabled/manual monitors in exactly
one selected profile. Every hosted version must activate and bind an exact-version session. Prompt
traffic uses an exact Agent reference.

Each issue folder is self-contained. Prompt issues deploy their complete `definition.json`; Hosted
issues package their complete `source/` tree together with the shared requirements and host/container
contract. A deployed issue version contains only its reviewed defect and no dormant branches for
other issues, so source-aware proposed fixes see the exact defective implementation.

Run all 36 staging issues and complete human review before provisioning or changing daily mappings:

```powershell
python -m agent_insights_quality run-full --report-date <Pacific YYYY-MM-DD> `
  --work-items $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
```

For an isolated Agent-only change, qualify only that Agent's `v0` and all assigned issues. Existing
Agents may reuse their latest reviewed evidence only when their content digests, mappings, and all
shared contracts are unchanged. Shared runtime, telemetry, assessment, scoring, schema,
infrastructure, or cross-Agent topology changes require full-catalog qualification or read-only
re-evaluation of retained evidence when no new traffic is needed.

After a complete staging `PASS` or `FAIL` report is human-reviewed, bind all 41 exact content
digests. `INCOMPLETE` is never promotable:

```powershell
python -m agent_insights_quality create-promotion-receipt `
  --report <private-staging-report> --manifest <private-staging-manifest> `
  --registry <private-staging-registry> `
  --output <private-promotion-receipt> --human-reviewed
```

Set `AIQ_STAGING_PROMOTION_RECEIPT` to that private file before provisioning `daily`.

Promotion may compose newly reviewed affected-Agent evidence with the latest valid receipts for
unchanged Agents. The composed receipt must bind every current mapping and exact digest; incomplete
evidence is never reusable. After daily provisioning, verify the registry and endpoints read-only.
Do not send smoke traffic that dirties the next three-hour clean window.

Provisioning writes each profile registry locally and uploads `daily.json` or `staging.json` to the
private Azure `deployment-registries` container using Entra authentication. Every qualification run
downloads the canonical blob before traffic, then validates profile, Project, catalogs, and all
version digests. Authorized operators therefore share one deployed environment without committing
Azure deployment identifiers to Git.

## Daily execution

```powershell
python -m agent_insights_quality run-daily --report-date <Pacific YYYY-MM-DD> `
  --work-items $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
```

The runner validates catalog hashes against the protected daily registry, resets each monitor once,
runs a read-only three-hour clean-window census, runs `v0`, then runs five deterministic issues per
Agent. Agents execute concurrently; exact versions for one Agent execute sequentially. Before each
Hosted version, the runner patches the Agent endpoint to one `FixedRatio` rule with 100% traffic on
that exact version, confirms the selector, and then creates an exact-version session. This keeps
compute behavior and outer telemetry version identity aligned.

Daily and staging Projects prohibit ad-hoc debug traffic. A pre-existing `invoke_agent` trace in the
minimum lookback window fails the Agent before any qualification traffic is sent. Monitor reset does
not delete telemetry. Debug locally or in a separately owned sandbox; wait for the three-hour window
to expire before rerunning qualification against the same profile.

The runner prints flushed, thread-safe progress lines for each Agent/version and for endpoint,
telemetry, trace, and Agent Insights stages. Long telemetry waits, Insight runs, and remote retries
emit periodic heartbeats without exposing URLs, payloads, or private identifiers.

Issue source, traffic, and version digests are reviewed contracts. Equal nonzero request, response,
and usable-response counts plus a verified natural trace contract prove that the reviewed runtime
contract was exercised. Optional semantic assertions can add simple output checks without becoming a
second assertion framework.

The measured five-Agent-concurrent daily smoke on 2026-08-25 completed in 37.6 minutes: 32.1 minutes
for endpoint/telemetry/Agent Insights runtime, 5.3 minutes for parallel Sol assessment, and 13 seconds
for finalization. Service retries can extend the runtime beyond this baseline.

An isolated issue failure does not stop later issues. Ambiguous later evidence remains
`INCOMPLETE`; it is never converted into a product miss.

A baseline false-positive Insight contributes to the unified `noise_cards` penalty but does not stop
issue diagnostics. Only an operational, telemetry, provenance, or trace-contract baseline failure
stops that Agent.

## Assessment and finalization

`run-daily` writes private assessment packages beside `run-manifest.json`. Use GPT-5.6 Sol with
`src/agent_insights_quality/prompts/assessment.md`, then finalize:

```powershell
python -m agent_insights_quality finalize `
  --manifest <private-run-manifest> `
  --assessment <issue-001-assessment.json> `
  --assessment <...> `
  --work-items $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
```

Finalization writes sanitized per-Agent Markdown under the report's `agents/` directory. Each row is
one generated Insight card, plus an explicit row for each missing expected issue. Email links to these
GitHub-rendered Markdown reports; private prompts, responses, traces, and resource identifiers remain
excluded. Each report includes a review summary, evaluation legend, and human-validation checklist.

For `daily`, finalization also derives one metrics-only payload and publishes it atomically to ADX.
The payload exposes logical `AIQDailyRuns`, `AIQDailyAgents`, `AIQDailyIssues`, and `AIQDailyFields`
views, but excludes assessment narratives, prompts, responses, traces, evidence references, work
items, provider IDs, and Azure resource identifiers. `run_id` plus a deterministic source digest
makes retries idempotent and rejects conflicting content.

An ADX failure does not change the qualification result or block email and generated pull-request
publication. Finalization writes an explicit private failure receipt, returns the failure code, and
adds a warning to the HTML email. The pull-request description and final automation result must also
state the ADX status.

The direct-email request remains private. Use the available Copilot email capability exactly once and
set HTML mode explicitly. Never create a draft or retry an ambiguous send. Import one receipt with
status `sent`, `failed`, or `unknown`; ambiguous delivery sets `retry_allowed=false` and requires
manual verification.

Every daily email includes the privately configured quality-trend dashboard link. Missing or invalid
dashboard-link configuration prevents creation of a daily email request; configure the copied link
once after importing the dashboard.

Before finalization, fetch the privately configured Azure Boards saved query:

```powershell
python -m agent_insights_quality fetch-quality-work-items `
  --query-url <private-query-url> `
  --report-date <Pacific YYYY-MM-DD> `
  --output $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
```

Pass that snapshot to both the runner and `finalize` with `--work-items`. The email first lists active
exact-`Quality` items, then items closed on the previous Pacific date, using ID, type, title, assignee,
and state. `Removed` items are excluded. The query URL, snapshot, and work-item content remain private
and never enter generated reports. Before traffic, the runner validates the closed date and writes a
private digest binding beside the run manifest; finalization rejects a different or stale snapshot.

```powershell
python -m agent_insights_quality email-receipt-import `
  --request <private-request> --receipt <provider-receipt> `
  --output reports/daily/YYYY/MM/DD/email-receipt.json
```

## Replay

Read-only replay validates the immutable manifest and retained evidence coordinates:

```powershell
python -m agent_insights_quality replay-run --manifest <private-run-manifest>
```

Replay does not reset monitors, send traffic, mutate Insights, or write telemetry. A true reproduction
is a new run with new endpoint traffic and a new run identity.

## Generated pull requests

Generated branches use `aiq-daily/`. Only new-format report, latest, trend, and email-receipt files
are allowed. Trusted `PASS` and `FAIL` reports enable auto-merge after required checks.
`INCOMPLETE` reports do not auto-merge.

## ADX backfill and retry

Publish one or more validated daily reports explicitly with repeatable `--report` arguments:

```powershell
python -m agent_insights_quality publish-adx `
  --report reports\daily\YYYY\MM\DD\report.json
```

To initialize history, invoke the command for every existing daily report that passes the current
report contract, in chronological order. Never adapt or restore superseded report formats solely for
ADX backfill. An identical retry returns `already_published`; a different metrics payload for the
same `run_id` fails closed. Publication receipts remain private under
`~/.aiq-runtime/agent-insights-quality/adx-publications/`.
