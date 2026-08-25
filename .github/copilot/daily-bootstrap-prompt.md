Run the weekday Agent Insights quality automation for the current schedule.

Fetch `origin/main` first. Read `AGENTS.md` and
`.github/skills/agent-insights-quality-daily/SKILL.md` from `origin/main` and follow them as the
authoritative, versioned runtime contract.

Use only the daily profile. During the scheduled run, never modify catalogs, Agent implementations,
schemas, infrastructure, configuration, skills, workflows, or documentation. Write only generated
daily report paths and private `.aiq-runtime` state.

Use the available email capability exactly once in explicit HTML mode. Never create a draft and never
retry an ambiguous send. Validate generated paths and report consistency, create an `aiq-daily/`
generated-only pull request, and enable auto-merge only for a trusted complete PASS or FAIL.
