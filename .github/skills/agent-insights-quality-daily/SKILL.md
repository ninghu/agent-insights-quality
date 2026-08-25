# Daily Agent Insights Quality

<!-- prompt-version: 2.0.0 -->

1. Resolve the Pacific business date.
2. Run `python -m agent_insights_quality run-daily --report-date <date>`.
3. Assess all five baseline packages and every issue package with GPT-5.6 Sol using the repository
   assessment prompt. Use independent `endpoint_evidence`; never assign `insight_engine` unless
   endpoint behavior and trace contract are both proven.
4. Run `finalize` with all assessments.
5. Use the available Copilot email capability to send the immutable HTML request exactly once. Set
   HTML mode explicitly, never create a draft, and never retry an ambiguous send.
6. Import one simple delivery receipt:
   - confirmed success: `sent`, opaque provider reference, retry forbidden;
   - explicit no-send failure: `failed`, retry allowed only for a later reviewed run;
   - ambiguous result: `unknown`, retry forbidden and manual verification required.
7. Create an `aiq-daily/` generated-only pull request.
8. Enable auto-merge only when the report is `PASS` or trusted `FAIL` and required checks pass.

The runner executes five Agents concurrently and versions sequentially within each Agent. Complete
runs use the reviewed score threshold of 90.

Never change catalogs, Agent implementations, schemas, infrastructure, configuration, or skills from
daily automation. Never target staging. Never write telemetry.
