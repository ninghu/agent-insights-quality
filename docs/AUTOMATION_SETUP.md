# Copilot Automation Setup

## Required capabilities

- repository read and generated-report pull requests;
- Azure CLI authentication for the reviewed daily profile;
- read-only Application Insights queries;
- deployed Foundry Agent endpoint access;
- one email capability with explicit HTML support.

The fixed reviewed recipient is `agentinsightsteam@microsoft.com`.

The automation does not need issue, release, deployment, or mailbox search capabilities.

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

Enable only the required capabilities above. The bootstrap remains small and stable; the versioned
daily skill on `main` is the authoritative runtime contract.

## First-run observation

Verify live progress output, 30 assessment packages, one immutable HTML send attempt, five per-Agent
reports, generated-path validation, required checks, and auto-merge. A complete trusted FAIL is a
valid product-quality result; INCOMPLETE is not published.
