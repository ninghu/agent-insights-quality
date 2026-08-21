Run today's Agent Insights quality workflow in ninghu/agent-insights-quality.
First run `python -m agent_insights_quality check-runtime-readiness`. If it is not ready, stop without
side effects and report its actionable INCONCLUSIVE result. Otherwise follow
`.github/skills/agent-insights-quality-daily/SKILL.md` exactly, using the `ninghu` identity and
GPT-5.6 Sol, including its final email and failure-finalizer rules.
