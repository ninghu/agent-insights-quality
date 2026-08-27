---
name: agent-insights-quality-daily
description: Run weekday Agent Insights qualification, reporting, email, and publication.
license: MIT
---

# Daily Agent Insights Quality

<!-- prompt-version: 2.0.0 -->

1. Set `PYTHONPATH` to the current worktree's `src` directory in the same shell as every repository
   Python command. Verify `agent_insights_quality.__file__` resolves inside the current worktree.
2. Resolve the Pacific business date.
3. Fetch the privately configured Azure Boards query with `fetch-quality-work-items` into
   `~/.aiq-runtime/agent-insights-quality/`, passing the report date. Keep active exact-`Quality` items
   plus items closed on the previous Pacific date; exclude `Removed`.
4. Run `python -m agent_insights_quality run-daily --report-date <date> --work-items <snapshot>`.
   The runner must synchronize the canonical private Azure Blob registry, verify the fixed telemetry
   connection, and wait for the `0.1`-hour clean interval before any Agent traffic. It may recover at
   most three transiently incomplete versions before finalization.
5. Assess all five baseline packages and every issue package with GPT-5.6 Sol using the repository
   assessment prompt. Use independent `endpoint_evidence`; never assign `insight_engine` unless
   endpoint behavior and trace contract are both proven. Equal nonzero request, response, and usable
   response counts plus a verified trace contract prove the reviewed runtime contract was exercised;
   semantic assertions are optional.
   Never assume a baseline card is Noise merely because it came from `v0`; use independent trace
   proof to distinguish an Agent runtime defect, Insight false positive, framework gap, or external
   infrastructure failure.
6. Run `finalize` with all assessments and the same private work-item snapshot.
7. Use the available Copilot email capability to send the immutable HTML request exactly once. Set
   HTML mode explicitly, never create a draft, and never retry an ambiguous send.
8. Import one simple delivery receipt:
   - confirmed success: `sent`, opaque provider reference, retry forbidden;
   - explicit no-send failure: `failed`, retry allowed only for a later reviewed run;
   - ambiguous result: `unknown`, retry forbidden and manual verification required.
9. Create an `aiq-daily/` generated-only pull request.
10. Enable auto-merge only when the report is `PASS` or trusted `FAIL` and required checks pass.

The runner executes five Agents concurrently and versions sequentially within each Agent. Complete
runs use the reviewed score threshold of 90.

Any `inconclusive` baseline assessment or `INCOMPLETE` issue assessment makes the whole run
`INCOMPLETE` with no numeric quality score.

Never change catalogs, Agent implementations, schemas, infrastructure, configuration, or skills from
daily automation. Never target staging. Never write telemetry. Never commit the Boards query or
work-item snapshot.
