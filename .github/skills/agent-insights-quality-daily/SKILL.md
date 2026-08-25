# Daily Agent Insights Quality

1. Resolve the Pacific business date.
2. Run `python -m agent_insights_quality run-daily --report-date <date>`.
3. Assess all five baseline packages and every issue package with GPT-5.6 Sol using the repository
   assessment prompt. Use independent `endpoint_evidence`; never assign `insight_engine` unless
   endpoint behavior and trace contract are both proven.
4. Run `finalize` with all assessments.
5. Send the immutable email request through connected Microsoft mail first, authorized Graph second,
   and local Outlook last. Stop after one successful transport.
6. Import the provider receipt.
7. Create an `aiq-daily/` generated-only pull request.
8. Enable auto-merge only when the report is `PASS` or trusted `FAIL` and required checks pass.

The runner executes five Agents concurrently and versions sequentially within each Agent. Complete
runs use the reviewed score threshold of 90. Treat local Outlook `Send()` returning without error as
successful; do not perform a Sent Items or mailbox post-send validation.

Never change catalogs, Agent implementations, schemas, infrastructure, configuration, or skills from
daily automation. Never target staging. Never write telemetry.
