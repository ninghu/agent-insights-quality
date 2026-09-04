Run the weekday Agent Insights quality automation for the current schedule.

Fetch `origin/main` first. Do not invoke a packaged skill by name. Read the authoritative, versioned
runtime contracts directly from `origin/main` with:

`git show origin/main:AGENTS.md`

`git show origin/main:.github/skills/agent-insights-quality-daily/SKILL.md`

Follow both contracts. If either contract cannot be read, stop without sending qualification traffic
or email and without creating a pull request.

Before every repository Python command, set `PYTHONPATH` to the current worktree's `src` directory in
the same shell process. Verify `agent_insights_quality.__file__` resolves inside the current worktree
before the first command. Never use an editable installation from another worktree.

Before qualification, fetch Quality-tagged work items from the privately configured Azure Boards
query into the durable user-level `~/.aiq-runtime/agent-insights-quality/` root, using the Pacific
report date. Include active items and items closed on the previous Pacific business date; exclude
`Removed`. Pass the private snapshot to both the daily runner and finalizer. If the query or durable
daily deployment registry blob is unavailable, stop without sending traffic or email. The central coordinator synchronizes the approved registry into the local runtime root before
opening Agent lanes.

Use only the daily profile. During the scheduled run, never modify catalogs, Agent implementations,
schemas, infrastructure, configuration, skills, workflows, or documentation. Write only generated
daily report paths, private durable runtime state, and the public-safe v2 result and explanation
payload to the fixed ADX quality database. ADX content must come only from public catalogs and the
committed sanitized report; never publish private work items, assessment packages, prompts,
responses, traces, evidence references, provider IDs, Foundry versions, or private links.
Application Insights remains read-only.

This is an official scheduled run. Never pass `--test-run`; that explicit mode is reserved for a
reviewed email-only rerun and intentionally skips ADX and pull-request publication.

Run `daily-prepare` and `daily-provision` centrally. Use `daily-guide` for all three barriers. First,
create five visible whole-Agent traffic sessions and run only their `daily-run-traffic-agent`
commands; each invokes `v0` and four issues sequentially with 60-second inter-version pacing and no
telemetry or Agent Insights work. After all 25 receipts, create up to eight visible
`daily-verify-next` evaluators for read-only dynamic version claims. After all 25 targets are
eligible, create five visible `daily-run-insights-agent` sessions. Each resets its Agent monitor
once, then autonomously processes all five versions without endpoint traffic. Never use a subprocess,
thread pool, or hidden worker. Keep the coordinator responsive with `daily-status`; run
`daily-compose` only after all 25 Insight receipts exist.

Run up to five visible per-Agent assessment sub sessions after the 25-package barrier. Validate their
outputs and any exact eligible focused rechecks with `daily-validate-assessments`. Keep improvement
analysis, finalization, ADX, delivery claim, receipt import, generated paths, and one PR centralized
and ordered.

Finalization attempts ADX publication and creates an email containing the privately configured
quality-trend dashboard link. Record the returned ADX status. An ADX failure does not block email or
the pull request: preserve the generated warning, report the failure in the final result and pull
request description, and never expose the cluster URI, dashboard link, or private receipt.

Run `daily-email-claim` exactly once, then use the available email capability exactly once in explicit
HTML mode. Never create a draft and never retry an ambiguous send. Import one private receipt; do not
create a public send attestation. Validate generated paths and report consistency, create one `aiq-daily/`
generated-only pull request, and enable auto-merge only for a complete numeric report.
