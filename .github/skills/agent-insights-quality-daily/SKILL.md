---
name: agent-insights-quality-daily
description: Run weekday Agent Insights qualification, reporting, email, and publication.
license: MIT
---

# Daily Agent Insights Quality

<!-- prompt-version: 4.0.0 -->

1. Set `PYTHONPATH` to the current worktree's `src` directory for every repository Python command
   and verify `agent_insights_quality.__file__` resolves inside that worktree.
2. Resolve the Pacific business date. Fetch the privately configured Azure Boards query with
   `fetch-quality-work-items` into the durable private runtime root. Keep active exact-`Quality`
   items plus items closed on the previous Pacific date; exclude `Removed`.
3. In the visible coordinator, run `daily-prepare`, `daily-provision`, and `daily-guide`.
   Preparation binds the clean checkout, catalogs, reviewed policy, four issues per Agent, private
   work-item snapshot, and opaque execution identity. Daily never reads staging validation state.
4. Start one visible session for each pending Weather, Healthcare, Finance, Travel, and Support
   lane. Each runs only:

   ```powershell
   python -m agent_insights_quality daily-run-agent --agent <name>
   ```

   The five lanes may run concurrently; there is no hidden fan-out. Each lane processes exactly
   `v0`, issue 1, issue 2, issue 3, and issue 4 in forward order. For each version, finish the full
   traffic -> telemetry/trace verification -> Agent Insights -> immutable result pipeline before
   waiting the reviewed 60 seconds and starting the next version. Daily never sends paired-`v0`
   issue traffic.
5. Every Agent/version owns an independent bounded claim lock and immutable artifact directory.
   Endpoint calls, telemetry queries, Agent Insights calls, and artifact writes never hold the global
   coordinator lock. Publish the exact traffic receipt before any aggregate pointer update; repeated
   identical publication is idempotent and conflicting content fails closed. Status and composition
   rebuild aggregate lane progress from immutable version artifacts after a crash. Never resend
   traffic after a definitive receipt.
6. Verify one whole target at a time from its exact response identities. `operation_Id` bounds the
   trace; the exact Agent, version, response identity, `span_id`, and `parent_span_id` select the
   `invoke_agent` root and complete descendant tree. Ignore foreign-identity roots, collapse only
   byte-identical telemetry rows, and fail closed on multiple distinct exact roots. Query and
   stabilize the whole batch, never individual attempts.
7. The total Daily telemetry maturity horizon is five minutes from immutable
   `traffic_completed_at`. Apply the age-aware hydration grace, poll adaptively with public-safe
   heartbeats, and use a short consecutive-snapshot stability interval. At the five-minute boundary,
   take one final exact whole-target snapshot. Complete unambiguous evidence may pass immediately
   there even when another stability interval cannot fit.
8. Send and retain all ten attempts. Aggregate acceptance requires at least six complete
   role-specific passes:
   - baseline: six complete healthy attempts;
   - issue: six complete defect-observed attempts.

   The remaining four attempts are transparent misses of any kind and never veto six strict passes.
   Each passing attempt still requires complete endpoint, exact identity, required semantic and trace
   evidence, and internally consistent proof. Preserve pass indices and mutually exclusive miss
   categories in `role_pass_summary`. Pure Prompt traffic still forbids every function call.
9. If evidence is still missing or ambiguous at the boundary, persist `skipped_telemetry`; do not
   run Agent Insights and do not retraffic. Complete evidence with fewer than six role passes is
   `skipped_agent_activation`. A terminal provider/Agent Insights failure after verified traffic is
   `skipped_insight`. Skip only that version and continue the same Agent lane after pacing. If `v0`
   is skipped, continue its four issues and expose missing baseline coverage.
10. Reset/reconcile the Agent Insights monitor exactly once per Agent, immediately before that
    Agent's first eligible Insight run. Never reset between versions. Derive and persist each
    version's minute-ceiling lookback from its own immutable traffic start through actual Insight
    start plus margin. Exact operation filtering excludes all earlier versions, including skipped
    traces. Persist start intent before POST, reconcile ambiguous starts without endpoint traffic,
    and accept only exact-run, exact-version cards wholly linked to that version's operations.
11. Run `daily-compose` after all five lanes are terminal. `daily-status` and `daily-guide` are
    read-only. Composition performs long artifact/package work outside the global lock and uses only
    a short lifecycle CAS to publish `COMPOSED`.
12. Assess only eligible baseline and issue packages with GPT-5.6 Sol. Use independent endpoint and
    full-request trace proof; never use a card's claim as Agent proof. Keep global card-link,
    ownership, duplicate, and scoring-field rules. A focused recheck may reuse evidence but never
    sends traffic or forces a verdict.
13. Skipped issues are excluded from both score numerator and expected-issue denominator. Noise and
    duplicate penalties include only eligible assessed issue cards. Reports, email, ADX, improvement
    input, and per-Agent views must expose `eligible_issue_count`, `skipped_issue_count`, exact skipped
    issue statuses/reasons, and baseline coverage. If no issue is eligible, produce no numeric score
    and fail finalization.
14. Finalize with the same private work-item snapshot and schema-bound GPT-5.6 Sol improvement
    analysis. Attempt public-safe ADX publication, create and send exactly one immutable HTML email
    request, import exactly one provider receipt, and create one generated-only `aiq-daily/` pull
    request. Never publish private packages, prompts, payloads, traces, identifiers, dashboard links,
    work-item content, claims, or provider receipts.

For an email-only test, use `daily-prepare --test-run --rerun N` with a nonzero rerun and the private
test recipient. It never writes ADX, repository report/trend paths, or a pull request unless the
separate reviewed preview option is explicitly supplied. Stop after importing the one provider
receipt.

Use `daily-fail --reason-code <public_safe_code> --confirm` only for an unrecoverable run. Daily
status/guide never deploy, invoke, query telemetry, run Agent Insights, assess, publish, or email.
