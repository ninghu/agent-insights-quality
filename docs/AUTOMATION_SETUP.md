# Copilot Automation Setup

## Required capabilities

- repository read and generated-report pull requests;
- Azure CLI authentication for the reviewed daily profile;
- read-only ARM access to the concrete Daily Foundry Project and Azure location metadata;
- read-only Application Insights queries;
- deployed Foundry Agent endpoint access;
- read access to one privately configured Azure Boards saved query;
- Storage Blob Data Reader access to the dedicated Sweden Central `g30` storage account's private
  `deployment-registries` container;
- an authenticated local Azure CLI user with Foundry Project Manager on the validation account,
  Monitoring Reader on Sweden staging `g30`, and ACR push;
- ADX Database Viewer and Ingestor access to the fixed quality analytics database;
- one email capability with explicit HTML support.

The fixed reviewed recipient is `agentinsightsteam@microsoft.com`.
Install the live Azure clients from the reviewed optional dependency set with
`python -m pip install -e ".[azure]"`; this includes the ADX data client used by daily
finalization.

During a reviewed delivery test, a private
`~/.aiq-runtime/agent-insights-quality/config/email-recipient.json` override may target one Microsoft
mailbox. Official runs ignore this override and always use the committed team recipient.

Official Daily does not need work-item mutation, release, or mailbox search capabilities. Local Test
Agent Validation uses the explicitly resolved Azure CLI user for durable topology reconciliation.
Keep the Boards query URL and fetched work-item snapshot private; neither belongs in repository
configuration or generated reports. Deployment registries and run state live under the durable
user-level `~/.aiq-runtime/agent-insights-quality/` root so scheduled worktrees share deployment and
lifecycle state.
The canonical registry is stored in the dedicated Sweden Central `g30` storage account; provisioning
operators need Storage Blob Data Contributor, while qualification-only operators need Storage Blob
Data Reader. The retained legacy storage account is not a fallback and is never modified.
ADX publication receipts and the rendered dashboard import file also stay under this durable private
root. Daily email uses the reviewed public `https://aka.ms/agent-insights/quality` short link.

Test Agent Validation has no runner, GitHub environment, OIDC principal, required check, or remote
reconciler. Every worktree uses the same OS lock and local state under
`~/.aiq-runtime/agent-insights-quality/test-agent-validation/`. The active journal is replaced
atomically; required content-addressed history, desired-state plans, receipts, registries, and
evidence stay local. Worker operations use authority and shard locks rather than holding the global
coordinator lock. Interrupted work resumes from exact fenced receipts. A new validation archives
legacy state byte-for-byte when needed, writes `SUPERSEDED`, and swaps the active pointer without
deleting any provider object or private evidence.

## Readiness

Run `.github/copilot/daily-readiness-prompt.md` manually. It must not write telemetry, files, commits,
pull requests, or email.

## Controlled email test

For a full reviewed qualification delivery test, use
`daily-prepare --test-run --rerun N --report-date <date> --work-items <snapshot>` with a nonzero rerun
number, then assess and finalize normally. It sends exactly one TEST-marked email to the private
`daily_test` override and writes all report and receipt artifacts under that private run directory.
It does not contact ADX, modify repository report or trend paths, or create a pull request. If
delivery is ambiguous, verify manually before any later retry. Remove the private override after the
test.

GitHub publication remains disabled unless the reviewed test also passes `--publish-preview`.
That opt-in appends the sanitized report, aggregate Markdown, five per-Agent Markdown files, and a
small public manifest under `<public-run-id>/` on the dedicated orphan
`aiq-email-test-preview` branch. Existing run directories are never changed or removed. Email links
use that permanent branch/run path; the Insight Engine improvement link remains hidden. Any
unexpected branch path, noncanonical generated file, private link, or divergent existing run aborts
finalization before the immutable email request is created. The preview branch never creates a pull
request.

## Scheduled automation

- Repository: `ninghu/agent-insights-quality`
- Execution: cloud
- Trigger: weekday
- Time zone: `America/Los_Angeles`
- Bootstrap: `.github/copilot/daily-bootstrap-prompt.md`

The scheduled coordinator owns preparation/provisioning, composition, assessment validation,
improvement analysis, finalization, ADX, one-time send claim, receipt import, generated paths, and
one pull request. Five visible Copilot sub sessions run the whole Agent lanes concurrently, bounded by
`max_parallel_agents`; up to five later visible sub sessions assess one Agent's five packages each.
No Daily command internally fans out work. `daily-status` and `daily-guide` are read-only orchestration
surfaces and keep the central coordinator responsive.
An unrecoverable run must be closed explicitly with `daily-fail --reason-code <public_safe_code>
--confirm`; the private failure receipt releases the next business date without deleting retained
state.

Enable only the required capabilities above. The bootstrap remains small and stable. It reads
`AGENTS.md` and the versioned daily skill directly from `origin/main` with `git show`; it does not
invoke a runtime-packaged skill by name.

## First-run observation

Verify five exact Agent completion receipts, live progress output, 25 assessment packages (20 issues plus five baselines), active and yesterday-closed Quality work-item
sections, one immutable HTML send attempt with the dashboard link, explicit ADX publication status,
five per-Agent reports, the stable Insight Engine improvement memory and immutable dated snapshot,
all seven public-safe ADX views, generated-path validation, required checks, and auto-merge.
Incomplete execution retains private durable diagnostics but creates no report, ADX row, or pull
request.
