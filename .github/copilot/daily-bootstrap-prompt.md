Run the weekday Agent Insights quality automation for the current schedule.

Fetch `origin/main` first. Do not invoke a packaged skill by name. Read the authoritative, versioned
runtime contracts directly from `origin/main` with:

`git show origin/main:AGENTS.md`

`git show origin/main:.github/skills/agent-insights-quality-daily/SKILL.md`

Follow both contracts. If either contract cannot be read, stop without sending qualification traffic
or email and without creating a pull request.

Before qualification, fetch Quality-tagged work items from the privately configured Azure Boards
query into the durable user-level `~/.aiq-runtime/agent-insights-quality/` root, using the Pacific
report date. Include active items and items closed on the previous Pacific business date; exclude
`Removed`. Pass the private snapshot to both the daily runner and finalizer. If the query or durable
daily deployment registry blob is unavailable, stop without sending traffic or email. The runner
automatically synchronizes the approved registry into the local runtime root before traffic.

Use only the daily profile. During the scheduled run, never modify catalogs, Agent implementations,
schemas, infrastructure, configuration, skills, workflows, or documentation. Write only generated
daily report paths, private durable runtime state, and the sanitized daily metrics payload to the
fixed ADX quality database. Application Insights remains read-only.

Finalization attempts ADX publication and creates an email containing the privately configured
quality-trend dashboard link. Record the returned ADX status. An ADX failure does not block email or
the pull request: preserve the generated warning, report the failure in the final result and pull
request description, and never expose the cluster URI, dashboard link, or private receipt.

Use the available email capability exactly once in explicit HTML mode. Never create a draft and never
retry an ambiguous send. Validate generated paths and report consistency, create an `aiq-daily/`
generated-only pull request, and enable auto-merge only for a trusted complete PASS or FAIL.
