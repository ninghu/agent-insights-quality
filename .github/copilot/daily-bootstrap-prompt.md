Run today's Agent Insights quality workflow in ninghu/agent-insights-quality.
First run `python -m agent_insights_quality check-runtime-readiness`. If it is not ready, prohibit
every operational phase but do not stop before finalization: run
`python -m agent_insights_quality finalize-readiness-failure --report-date <Pacific YYYY-MM-DD>`,
send exactly the one rendered email through the connected Microsoft mail capability, and record
the delivery receipt or failure with `record-email-result`. Otherwise follow
`.github/skills/agent-insights-quality-daily/SKILL.md` exactly, using the `ninghu` identity and
GPT-5.6 Sol, including its final email and failure-finalizer rules.
