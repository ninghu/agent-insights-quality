---
name: agent-insights-quality-daily
description: Run weekday Agent Insights qualification, reporting, email, and publication.
license: MIT
---

# Daily Agent Insights Quality

<!-- prompt-version: 3.0.0 -->

1. Set `PYTHONPATH` to the current worktree's `src` directory in the same shell as every repository
   Python command. Verify `agent_insights_quality.__file__` resolves inside the current worktree.
2. Resolve the Pacific business date.
3. Fetch the privately configured Azure Boards query with `fetch-quality-work-items` into
   `~/.aiq-runtime/agent-insights-quality/`, passing the report date. Keep active exact-`Quality` items
   plus items closed on the previous Pacific date; exclude `Removed`.
4. In the central Daily coordinator session, run `daily-prepare` with the Pacific report date and
   immutable private work-item snapshot, then run `daily-provision`. Preparation binds the exact clean
   commit's immutable approved staging record. Provisioning reconciles and freezes the Daily registry,
   topology, region, reviewed limits, four-issue selections, and hidden system-generated execution
   identity under one private lifecycle/quiescence lock.
5. Run `daily-guide`. Start exactly one visible Copilot sub session for each pending Weather,
   Healthcare, Finance, Travel, and Support lane, up to the configured `max_parallel_agents`. Each
   sub session runs only `daily-run-agent --agent <name>`. Never split versions or create subprocess,
   thread-pool, or other in-process workers. Each lane runs `v0` then its four selected issues
   sequentially, uses its Agent lock and exact checkpoint set, and may claim at most three transient
   recoveries across resumes. The lane's reviewed start offset prevents a simultaneous endpoint burst.
6. Keep the coordinator responsive while lanes run. Use `daily-status` or `daily-guide`; neither
   command fans out or sends traffic. Resume only pending lanes. After all five immutable exact-bound
   completion receipts exist, run `daily-compose`. Composition accepts only those five receipts and
   produces exactly 20 issue packages plus five baseline packages. If the run is unrecoverable, use
   `daily-fail --reason-code <public_safe_code> --confirm` centrally; this writes a private immutable
   failure receipt and releases the quiescence claim without deleting any evidence or provider state.
7. Start up to five visible Copilot assessment sub sessions, one per Agent and its five packages.
   Assess each baseline and four issues with GPT-5.6 Sol using the repository assessment prompt. Use
   independent `endpoint_evidence`; never assign `insight_engine` unless
   endpoint behavior and trace contract are both proven. Equal nonzero request, response, and usable
   response counts plus a verified trace contract prove endpoint execution. Require every reviewed
   baseline semantic assertion, each designated issue activation assertion, and one terminal success
   plus output signal per baseline request. Pure Prompt requests must each have one direct terminal
   response and no function calls.
   Never assume a baseline card is Noise merely because it came from `v0`; use independent trace
   full-request and separate card-linked proof to distinguish an Agent runtime defect, Insight false
   positive, framework gap, or external infrastructure failure. Route contradictory intermediate
   evidence to `test_framework` or `unresolved`.
8. Before finalization, give every eligible `inconclusive` baseline or `INCOMPLETE` issue with complete runtime
   evidence one focused GPT-5.6 Sol recheck. Re-read the package-bound reviewed Agent source and
   configuration, current endpoint evidence, independent trace proof, and the card's exact claim.
   Never send new traffic for this recheck and never force a conclusive verdict. Rechecks may use
   visible per-Agent assessment sessions but never traffic sessions. Run
   `daily-validate-assessments` with the initial outputs and exact eligible recheck replacements; the
   coordinator rejects missing, extra, stale, or unbound outputs. If independent evidence remains
   insufficient, fail without producing a report.
9. Run `finalize` with the validated assessments and the same private work-item snapshot. Finalization must
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
10. Run `daily-email-claim` once. Use the available Copilot email capability to send only that exact
   immutable HTML request. Set HTML mode explicitly, never create a draft, and never retry an
   ambiguous send. The private claim and imported provider receipt remain the sole send authority;
   never create a public attestation file.
11. Import one simple delivery receipt to the exact private path returned by `daily-guide`:
   - confirmed success: `sent`, opaque provider reference, retry forbidden;
   - explicit no-send failure: `failed`, retry allowed only for a later reviewed run;
   - ambiguous result: `unknown`, retry forbidden and manual verification required.
12. Validate generated paths and create exactly one `aiq-daily/` generated-only pull request. Include
   only the sanitized ADX and delivery status in its description; never commit the cluster URI,
   dashboard link, private claim, or private receipt. Register the one PR with
   `daily-complete-publication`.
13. Enable auto-merge only when a complete numeric report exists and required checks pass.

For a reviewed email-only test, run `daily-prepare` with both `--test-run` and a nonzero `--rerun N`.
The private `daily_test` recipient override is required. The test run still uses deployed endpoints,
bounded post-invoke telemetry correlation, and private durable runtime state, but finalization must send only the one
TEST-marked email. It must not contact ADX, write repository report or trend paths, validate generated
paths, create a pull request, or enable auto-merge. Stop after importing the provider receipt into the
private run directory. Scheduled official runs never pass `--test-run`.

Five visible Copilot Agent-lane sub sessions execute concurrently while versions and endpoint
requests remain sequential inside each lane. The code never internally fans out Daily lanes or
assessment lanes. Daily provisioning is new-only: it exact-selects the
dedicated Sweden `g30` storage account, derives and fetches the exact clean commit's immutable
approved-record Blob, checks its locked WORM metadata, and
recomputes the validation digest. It never accepts an operator-supplied local record or falls back to
legacy storage or an old West US 2 promotion receipt. Local Test Agent Validation covers all 36 issues plus five
baselines outside this Daily workflow.

Any inconclusive baseline or issue assessment fails finalization and produces no report.

Never change catalogs, Agent implementations, schemas, infrastructure, configuration, or skills from
daily automation. Never target staging. ADX is the only authorized analytics write and accepts only
the finalized public-safe v3 result and explanation payload; Application Insights remains read-only.
Never write telemetry or commit the Boards query, work-item snapshot, ADX receipt, rendered
dashboard, cluster URI, or dashboard link.
