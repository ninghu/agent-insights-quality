Run the weekday Agent Insights quality automation for the current schedule.

Fetch `origin/main` first. Do not invoke a packaged skill by name. Read the authoritative, versioned
runtime contracts directly from `origin/main` with:

`git show origin/main:AGENTS.md`

`git show origin/main:.github/skills/agent-insights-quality-daily/SKILL.md`

Follow both contracts. If either contract cannot be read, stop without sending qualification traffic
or email and without creating a pull request.

Before qualification, fetch Quality-tagged work items from the privately configured Azure Boards
query into `.aiq-runtime`, using the Pacific report date. Include active items and items closed on the
previous Pacific business date; exclude `Removed`. Pass the private snapshot to both the daily runner
and finalizer. If the query fails, stop without sending traffic or email.

Use only the daily profile. During the scheduled run, never modify catalogs, Agent implementations,
schemas, infrastructure, configuration, skills, workflows, or documentation. Write only generated
daily report paths and private `.aiq-runtime` state.

Use the available email capability exactly once in explicit HTML mode. Never create a draft and never
retry an ambiguous send. Validate generated paths and report consistency, create an `aiq-daily/`
generated-only pull request, and enable auto-merge only for a trusted complete PASS or FAIL.
