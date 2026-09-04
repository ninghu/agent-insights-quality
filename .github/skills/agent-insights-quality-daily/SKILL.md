---
name: agent-insights-quality-daily
description: Run weekday Agent Insights qualification, reporting, email, and publication.
license: MIT
---

# Daily Agent Insights Quality

<!-- prompt-version: 3.1.0 -->

1. Set `PYTHONPATH` to the current worktree's `src` directory in the same shell as every repository
   Python command. Verify `agent_insights_quality.__file__` resolves inside the current worktree.
2. Resolve the Pacific business date.
3. Fetch the privately configured Azure Boards query with `fetch-quality-work-items` into
   `~/.aiq-runtime/agent-insights-quality/`, passing the report date. Keep active exact-`Quality` items
   plus items closed on the previous Pacific date; exclude `Removed`.
4. In the central Daily coordinator session, run `daily-prepare` with the Pacific report date and
   immutable private work-item snapshot, then run `daily-provision`. Manually invoking
   `daily-prepare` is the human operational decision to run Daily. Test Agent Validation is separate
   advisory evidence: Daily never reads staging state or a validation digest.
   Preparation binds the exact clean checkout, catalogs, reviewed limits, four-issue selections, and
   hidden system-generated execution identity. Provisioning reconciles and freezes the Daily
   registry, topology, and region under one private lifecycle/quiescence lock.
5. Run `daily-guide`. For the traffic phase, start one visible Copilot session for each pending
   Weather, Healthcare, Finance, Travel, and Support lane. Each session runs only
   `daily-run-traffic-agent --agent <name>`. Lanes retain their reviewed initial offsets. Within a
   lane, invoke `v0` and then four selected issue versions sequentially, using ten reviewed
   observations per target and waiting exactly 60 seconds after a durable version receipt before
   starting the next version. Daily never sends per-issue paired-`v0` traffic. Traffic sessions do
   not query telemetry, verify traces, start Agent Insights, or build packages. Do not use hidden
   subprocess or thread-pool concurrency. The verification phase cannot open until all 25 immutable
   exact response-bound traffic receipts exist. Ambiguous endpoint delivery fails closed and is
   never represented as a completed receipt.
6. After the traffic barrier, start up to eight visible evaluator sessions. Each repeatedly runs
   `daily-verify-next`, dynamically claims one version, and performs one read-only whole-target
   telemetry snapshot using that receipt's exact references and window. `daily-release-verification`
   releases only the caller's unfinished claim; lease expiry remains the crash fallback. Slow
   versions do not block other claims. A baseline or issue that does not prove the reviewed `6/10`
   threshold fails the Daily run and never reaches Agent Insights; conclusive misses are not
   resampled. The Insight phase opens only after all 25 targets are eligible.
7. After the verification barrier, start one visible Agent Insights session per Agent with
   `daily-run-insights-agent --agent <name>`, up to five concurrently. Each Agent lane persists and
   reconciles exactly one monitor-reset epoch, then autonomously processes `v0` and its four issues
   sequentially without another reset. For every version it derives a bounded, minute-ceiling
   lookback from immutable `traffic_started_at` through actual Insight start plus the reviewed margin,
   persists the exact start intent before the POST, reconciles exactly one provider run, polls it to
   terminal, and accepts only cards bound to that run whose links are unique and wholly inside the
   version's verified operation set. Zero, one, or multiple cards are terminal package outcomes.
   Prior-run cards are excluded; exact-run cross-version cards fail closed. Once all 25 Insight
   receipts exist, run `daily-compose` to create exactly five baseline and twenty issue packages.
   `daily-status` and `daily-guide` are read-only. Use `daily-fail --reason-code
   <public_safe_code> --confirm` only for an unrecoverable run.
8. Start up to five visible Copilot assessment sub sessions, one per Agent and its five packages.
   Assess each baseline and four issues with GPT-5.6 Sol using the repository assessment prompt. Use
   independent `endpoint_evidence`; never assign `insight_engine` unless
   endpoint behavior and trace contract are both proven. Equal nonzero request, response, and usable
   response counts plus a verified trace contract prove endpoint execution. Require every reviewed
   baseline semantic assertion, each designated issue activation assertion, and one terminal success
   plus output signal per baseline request. Pure Prompt requests must each have one direct terminal
   response and no function calls.
   For issue activation, the first mature whole-target batch may be accepted with at least
   `6/10` complete observations plus at most four audited trace-only unknown attempts when
   endpoint and exact identity evidence are complete, all reviewed semantic evidence is sufficient,
   and no sufficient assertion contradicts the expected behavior. Preserve those attempts as
   incomplete/non-observations and carry the acceptance policy and indices in the manifest and
   assessment package. The same rule applies to model-mediated issues. Baselines require `6/10`
   proven healthy attempts plus at most four trace-only unknowns and no observed failure.
   Never assume a baseline card is Noise merely because it came from `v0`; use independent trace
   full-request and separate card-linked proof to distinguish an Agent runtime defect, Insight false
   positive, framework gap, or external infrastructure failure. Route contradictory intermediate
   evidence to `test_framework` or `unresolved`.
9. Before finalization, give every eligible `inconclusive` baseline or `INCOMPLETE` issue with complete runtime
   evidence one focused GPT-5.6 Sol recheck. Re-read the package-bound reviewed Agent source and
   configuration, current endpoint evidence, independent trace proof, and the card's exact claim.
   Never send new traffic for this recheck and never force a conclusive verdict. Rechecks may use
   visible per-Agent assessment sessions but never traffic sessions. Run
   `daily-validate-assessments` with the initial outputs and exact eligible recheck replacements; the
   coordinator rejects missing, extra, stale, or unbound outputs. If independent evidence remains
   insufficient, fail without producing a report.
10. Run `finalize` with the validated assessments and the same private work-item snapshot. Finalization must
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
11. Run `daily-email-claim` once. Use the available Copilot email capability to send only that exact
   immutable HTML request. Set HTML mode explicitly, never create a draft, and never retry an
   ambiguous send. The private claim and imported provider receipt remain the sole send authority;
   never create a public attestation file.
12. Import one simple delivery receipt to the exact private path returned by `daily-guide`:
   - confirmed success: `sent`, opaque provider reference, retry forbidden;
   - explicit no-send failure: `failed`, retry allowed only for a later reviewed run;
   - ambiguous result: `unknown`, retry forbidden and manual verification required.
13. Validate generated paths and create exactly one `aiq-daily/` generated-only pull request. Include
   only the sanitized ADX and delivery status in its description; never commit the cluster URI,
   dashboard link, private claim, or private receipt. Register the one PR with
   `daily-complete-publication`.
14. Enable auto-merge only when a complete numeric report exists and required checks pass.

For a reviewed email-only test, run `daily-prepare` with both `--test-run` and a nonzero `--rerun N`.
The private `daily_test` recipient override is required. By default, the test publishes nothing to
GitHub. The additional explicit `--publish-preview` option authorizes finalization to append one
immutable `<public-run-id>/` directory to the dedicated orphan `aiq-email-test-preview` branch. That
directory contains only the schema-bound manifest, sanitized `report.json`/`report.md`, and exactly
five generated per-Agent Markdown files. Existing run directories are permanent and immutable; any
unmanaged or divergent branch content fails closed. Email links use the stable branch plus unique run
directory. The improvement link stays hidden, and no preview branch creates a pull request.

The test run still uses deployed endpoints, bounded post-invoke telemetry correlation, and private
durable runtime state, but finalization must send only the one TEST-marked email. It must not contact
ADX, write repository report or trend paths, validate generated paths, create a pull request, or enable
auto-merge. Stop after importing the provider receipt into the private run directory. Scheduled
official runs never pass `--test-run` or `--publish-preview`.

Five visible traffic lanes execute concurrently while endpoint requests remain sequential inside
each lane; up to eight visible read-only verification sessions and five visible Insight lanes run
only after their global barriers. The code never internally fans out Daily or assessment lanes.
Daily provisioning is new-only and exact-reconciles the dedicated Sweden `g30`
Daily registry and Project without reading staging records, validation digests, legacy storage, or an
old West US 2 promotion receipt. Local Test Agent Validation covers all 36 issues plus five baselines
outside this Daily workflow and remains advisory to the human who decides whether to invoke Daily.

Any inconclusive baseline or issue assessment fails finalization and produces no report.

Never change catalogs, Agent implementations, schemas, infrastructure, configuration, or skills from
daily automation. Never target staging. ADX is the only authorized analytics write and accepts only
the finalized public-safe v3 result and explanation payload; Application Insights remains read-only.
Never write telemetry or commit the Boards query, work-item snapshot, ADX receipt, rendered
dashboard, cluster URI, or dashboard link.
