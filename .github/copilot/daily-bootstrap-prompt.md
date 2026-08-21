Run today's Agent Insights quality workflow in ninghu/agent-insights-quality.
Run `python -m agent_insights_quality run-daily --report-date <Pacific YYYY-MM-DD>`; do not invoke
readiness as a separate stopping step.
The wrapper must continue through its failure finalizer after any nonzero phase, persist an
INCONCLUSIVE report and unsent direct-email handoff, and then return nonzero. Follow
`.github/skills/agent-insights-quality-daily/SKILL.md` exactly using the `ninghu` identity and
GPT-5.6 Sol. Submit the email handoff using its no-duplicate order: connected Copilot mail first;
Graph `/me/sendMail` only with confirmed `Mail.Send`; then local Outlook COM only on `hostId=local`
for the exact authenticated-user test mailbox with Sent Items verification. Preserve one
`content_digest`, stop after the first confirmed success, never use a Logic App, and import the
ordered receipt before claiming delivery.
