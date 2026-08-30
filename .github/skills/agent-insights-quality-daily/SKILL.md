---
name: agent-insights-quality-daily
description: Run weekday Agent Insights qualification, reporting, email, and publication.
license: MIT
---

# Daily Agent Insights Quality

<!-- prompt-version: 2.5.0 -->

1. Set `PYTHONPATH` to the current worktree's `src` directory in the same shell as every repository
   Python command. Verify `agent_insights_quality.__file__` resolves inside the current worktree.
2. Resolve the Pacific business date.
3. Fetch the privately configured Azure Boards query with `fetch-quality-work-items` into
   `~/.aiq-runtime/agent-insights-quality/`, passing the report date. Keep active exact-`Quality` items
   plus items closed on the previous Pacific date; exclude `Removed`.
4. Run `python -m agent_insights_quality run-daily --report-date <date> --work-items <snapshot>`.
   The runner must synchronize the canonical private Azure Blob registry, verify the fixed telemetry
   connection, stagger concurrent Agent starts, and wait for the configured clean interval before any
   Agent traffic. It may recover at most three transiently incomplete versions per Agent before
   finalization.
5. Assess all five baseline packages and the four deterministically selected issues per Agent with
   GPT-5.6 Sol using the repository assessment prompt. The daily run contains 20 issue packages and
   five baseline packages, 25 total. Use independent `endpoint_evidence`; never assign `insight_engine` unless
   endpoint behavior and trace contract are both proven. Equal nonzero request, response, and usable
   response counts plus a verified trace contract prove endpoint execution. Require every reviewed
   baseline semantic assertion, each designated issue activation assertion, and one terminal success
   plus output signal per baseline request. Pure Prompt requests must each have one direct terminal
   response and no function calls.
   Never assume a baseline card is Noise merely because it came from `v0`; use independent trace
   full-request and separate card-linked proof to distinguish an Agent runtime defect, Insight false
   positive, framework gap, or external infrastructure failure. Route contradictory intermediate
   evidence to `test_framework` or `unresolved`.
6. Before finalization, give every `inconclusive` baseline or `INCOMPLETE` issue with complete runtime
   evidence one focused GPT-5.6 Sol recheck. Re-read the package-bound reviewed Agent source and
   configuration, current endpoint evidence, independent trace proof, and the card's exact claim.
   Never send new traffic for this recheck, never force a conclusive verdict, and retain `INCOMPLETE`
   when independent evidence remains insufficient.
7. Run `finalize` with all assessments and the same private work-item snapshot. Finalization must
   first use `--prepare-improvement-input` to write the normalized public-safe improvement input.
   Assess that input with GPT-5.6 Sol using
   `src/agent_insights_quality/prompts/improvement.md`, save the strict schema-bound JSON privately,
   then run `finalize` with `--improvement-analysis <path>`. Official Daily atomically publishes the
   living and immutable-snapshot improvement files with the report. An email-only test writes only a
   private improvement preview and never mutates or links the living document.
   Finalization must
   attempt the public-safe daily ADX publication and create the immutable email request with the
   privately configured quality-trend dashboard link. ADX may receive public catalog expectations
   and full reasoning already present in the committed sanitized report, but never private assessment
   packages, work-item context, prompts, responses, traces, evidence references, provider IDs,
   Foundry versions, or private links. Record the returned ADX status. If ADX
   publication fails, continue the email and pull-request flow; the email warning, private receipt,
   final automation result, and pull-request description must explicitly report the failure.
8. Use the available Copilot email capability to send the immutable HTML request exactly once. Set
   HTML mode explicitly, never create a draft, and never retry an ambiguous send.
9. Import one simple delivery receipt:
   - confirmed success: `sent`, opaque provider reference, retry forbidden;
   - explicit no-send failure: `failed`, retry allowed only for a later reviewed run;
   - ambiguous result: `unknown`, retry forbidden and manual verification required.
10. Create an `aiq-daily/` generated-only pull request. Include the ADX publication status in the
   description without committing the cluster URI, dashboard link, or private receipt.
11. Enable auto-merge only when the report is `PASS` or trusted `FAIL` and required checks pass.

For a reviewed email-only test, run `run-daily` with both `--test-run` and a nonzero `--rerun N`.
The private `daily_test` recipient override is required. The test run still uses deployed endpoints,
the clean-window policy, and private durable runtime state, but finalization must send only the one
TEST-marked email. It must not contact ADX, write repository report or trend paths, validate generated
paths, create a pull request, or enable auto-merge. Stop after importing the provider receipt into the
private run directory. Scheduled official runs never pass `--test-run`.

The runner executes five Agents concurrently and versions sequentially within each Agent. Complete
runs use the reviewed score threshold of 90. Daily provisioning is new-only: it requires
`AIQ_APPROVED_VALIDATION_RECORD`, checks out that record's exact commit, and recomputes the validation
digest. It never falls back to a legacy staging promotion receipt. Local Test Agent Validation covers
all 36 issues plus five baselines outside this Daily workflow.

Any `inconclusive` baseline assessment or `INCOMPLETE` issue assessment makes the whole run
`INCOMPLETE` with no numeric quality score.

Never change catalogs, Agent implementations, schemas, infrastructure, configuration, or skills from
daily automation. Never target staging. ADX is the only authorized analytics write and accepts only
the finalized public-safe v2 result and explanation payload; Application Insights remains read-only.
Never write telemetry or commit the Boards query, work-item snapshot, ADX receipt, rendered
dashboard, cluster URI, or dashboard link.
