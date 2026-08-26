# Copilot Automation Setup

## Required capabilities

- repository read and generated-report pull requests;
- Azure CLI authentication for the reviewed daily profile;
- read-only Application Insights queries;
- deployed Foundry Agent endpoint access;
- read access to one privately configured Azure Boards saved query;
- one email capability with explicit HTML support.

The fixed reviewed recipient is `agentinsightsteam@microsoft.com`.

The automation does not need work-item mutation, release, deployment, or mailbox search capabilities.
Keep the Boards query URL and fetched work-item snapshot private; neither belongs in repository
configuration or generated reports.

## Readiness

Run `.github/copilot/daily-readiness-prompt.md` manually. It must not write telemetry, files, commits,
pull requests, or email.

## Controlled email test

Run `.github/copilot/email-test-prompt.md` manually. Confirm exactly one rendered HTML email arrives.
If delivery is ambiguous, verify manually before any later retry.

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

Verify live progress output, 30 assessment packages, active and yesterday-closed Quality work-item
sections, one immutable HTML send attempt, five per-Agent reports, generated-path validation, required
checks, and auto-merge. A complete trusted FAIL is a valid product-quality result; INCOMPLETE is not
published.
