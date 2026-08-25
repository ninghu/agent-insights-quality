Run the weekday Agent Insights quality qualification from the repository skill.

1. Resolve the current Pacific business date and stop if it is not a weekday.
2. Use only `python -m agent_insights_quality run-daily --report-date <date>`. Never deploy,
   provision, rotate telemetry, target staging, or modify repository contracts.
3. Preserve the runner's live Agent/version progress output. If clean-window, endpoint, telemetry,
   trace, or Agent Insights evidence is incomplete, finalize `INCOMPLETE` and do not publish.
4. Assess all five baseline packages and 25 issue packages with GPT-5.6 Sol using the repository
   assessment prompt. Evaluate every generated card, use independent endpoint evidence, and classify
   ownership.
5. Finalize the report. Send its immutable email request exactly once using connected Microsoft mail,
   authorized Graph, then local Outlook as fallbacks; stop after the first successful transport and
   import the receipt.
6. Create an `aiq-daily/` generated-only pull request containing report JSON/Markdown, five per-Agent
   Markdown reports, latest views, trend, and email receipt.
7. Enable auto-merge only for a trusted, complete `PASS` or `FAIL` after required checks pass.
