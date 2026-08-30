# Copilot Automation Setup

## Required capabilities

- repository read and generated-report pull requests;
- Azure CLI authentication for the reviewed daily profile;
- read-only ARM access to the concrete Daily Foundry Project and Azure location metadata;
- read-only Application Insights queries;
- deployed Foundry Agent endpoint access;
- read access to one privately configured Azure Boards saved query;
- Storage Blob Data Reader access to the private `deployment-registries` container;
- an authenticated local Azure CLI user with Foundry Project Manager on the validation account,
  Monitoring Reader on staging `g29`, ACR push, and Blob contributor access for the immutable
  `test-agent-validation-approved-records` container;
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
Agent Validation uses the explicitly resolved Azure CLI user for deployment and cleanup.
Keep the Boards query URL and fetched work-item snapshot private; neither belongs in repository
configuration or generated reports. Deployment registries and run state live under the durable
user-level `~/.aiq-runtime/agent-insights-quality/` root so scheduled worktrees share approved state.
The canonical registry is stored in the existing private Azure storage account; provisioning operators
need Storage Blob Data Contributor, while qualification-only operators need Storage Blob Data Reader.
ADX publication receipts and the rendered dashboard import file also stay under this durable private
root. Daily email uses the reviewed public `https://aka.ms/agent-insights/quality` short link.

Test Agent Validation has no runner, GitHub environment, OIDC principal, required check, or remote
reconciler. Every worktree uses the same OS lock and local state under
`~/.aiq-runtime/agent-insights-quality/test-agent-validation/`. The active journal is replaced
atomically; required content-addressed history, evidence, and CLEAN files stay local. Process exit
releases the lock, and the next invocation resumes cleanup before starting a new cycle. The 72-hour
execution TTL never prevents cleanup.

Only the final approved validation record uses Blob. The storage account must have versioning and the
`test-agent-validation-approved-records` container must have immutable storage with versioning.
The local approval command verifies the Azure CLI token remains bound to the resolved user on every
SDK token acquisition. No client secret, service principal, or ambient credential chain is accepted.

## Readiness

Run `.github/copilot/daily-readiness-prompt.md` manually. It must not write telemetry, files, commits,
pull requests, or email.

## Controlled email test

For a full reviewed qualification delivery test, use
`run-daily --test-run --rerun N --report-date <date> --work-items <snapshot>` with a nonzero rerun
number, then assess and finalize normally. It sends exactly one TEST-marked email to the private
`daily_test` override and writes all report and receipt artifacts under that private run directory.
It does not contact ADX, modify repository report or trend paths, or create a pull request. If
delivery is ambiguous, verify manually before any later retry. Remove the private override after the
test.

## Scheduled automation

- Repository: `ninghu/agent-insights-quality`
- Execution: cloud
- Trigger: weekday
- Time zone: `America/Los_Angeles`
- Bootstrap: `.github/copilot/daily-bootstrap-prompt.md`

Enable only the required capabilities above. The bootstrap remains small and stable. It reads
`AGENTS.md` and the versioned daily skill directly from `origin/main` with `git show`; it does not
invoke a runtime-packaged skill by name.

## First-run observation

Verify live progress output, 25 assessment packages (20 issues plus five baselines), active and yesterday-closed Quality work-item
sections, one immutable HTML send attempt with the dashboard link, explicit ADX publication status,
five per-Agent reports, the stable Insight Engine improvement memory and immutable dated snapshot,
all seven public-safe ADX views, generated-path validation, required checks,
and auto-merge. A complete trusted FAIL is a valid product-quality result; INCOMPLETE is not
auto-merged, but its sanitized operational result and committed explanation are retained in ADX.
