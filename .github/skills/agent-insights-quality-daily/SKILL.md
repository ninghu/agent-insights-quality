---
name: agent-insights-quality-daily
description: Run weekday Agent Insights qualification, reporting, email, and publication.
license: MIT
---

# Daily Agent Insights Quality

<!-- prompt-version: 2.2.0 -->

1. Resolve the Pacific business date.
2. Fetch the privately configured Azure Boards query with `fetch-quality-work-items` into
   `~/.aiq-runtime/agent-insights-quality/`, passing the report date. Keep active exact-`Quality` items
   plus items closed on the previous Pacific date; exclude `Removed`.
3. Run `python -m agent_insights_quality run-daily --report-date <date> --work-items <snapshot>`.
   The runner must synchronize the canonical private Azure Blob registry before any Agent traffic.
4. Assess all five baseline packages and every issue package with GPT-5.6 Sol using the repository
   assessment prompt. Use independent `endpoint_evidence`; never assign `insight_engine` unless
   endpoint behavior and trace contract are both proven. Equal nonzero request, response, and usable
   response counts plus a verified trace contract prove the reviewed runtime contract was exercised;
   semantic assertions are optional.
   Never assume a baseline card is Noise merely because it came from `v0`; use independent trace
   proof to distinguish an Agent runtime defect, Insight false positive, framework gap, or external
   infrastructure failure.
5. Run `finalize` with all assessments and the same private work-item snapshot. Finalization must
   attempt the public-safe daily ADX publication and create the immutable email request with the
   privately configured quality-trend dashboard link. ADX may receive public catalog expectations
   and full reasoning already present in the committed sanitized report, but never private assessment
   packages, work-item context, prompts, responses, traces, evidence references, provider IDs,
   Foundry versions, or private links. Record the returned ADX status. If ADX
   publication fails, continue the email and pull-request flow; the email warning, private receipt,
   final automation result, and pull-request description must explicitly report the failure.
6. Use the available Copilot email capability to send the immutable HTML request exactly once. Set
   HTML mode explicitly, never create a draft, and never retry an ambiguous send.
7. Import one simple delivery receipt:
   - confirmed success: `sent`, opaque provider reference, retry forbidden;
   - explicit no-send failure: `failed`, retry allowed only for a later reviewed run;
   - ambiguous result: `unknown`, retry forbidden and manual verification required.
8. Create an `aiq-daily/` generated-only pull request. Include the ADX publication status in the
   description without committing the cluster URI, dashboard link, or private receipt.
9. Enable auto-merge only when the report is `PASS` or trusted `FAIL` and required checks pass.

The runner executes five Agents concurrently and versions sequentially within each Agent. Complete
runs use the reviewed score threshold of 90.

Any `inconclusive` baseline assessment or `INCOMPLETE` issue assessment makes the whole run
`INCOMPLETE` with no numeric quality score.

Never change catalogs, Agent implementations, schemas, infrastructure, configuration, or skills from
daily automation. Never target staging. ADX is the only authorized analytics write and accepts only
the finalized public-safe v2 result and explanation payload; Application Insights remains read-only.
Never write telemetry or commit the Boards query, work-item snapshot, ADX receipt, rendered
dashboard, cluster URI, or dashboard link.
