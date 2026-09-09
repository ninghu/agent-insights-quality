# Contributor guide

This public repository measures Foundry Agent Insights against five synthetic Agents, five
baselines and 40 reviewed single-root issues. The goal is credible evidence, not a high score.
Issue counts are Weather 7, Healthcare 6, Finance 9, Travel 8 and Support 10. Issue-007
belongs to Support's Hosted implementation; Healthcare remains pure Prompt.

## Rewrite status

The replacement runner and offline suites are integrated. Its deployed staging and private
TEST email acceptance remain separate requirements before scheduled use. Never use legacy
staging/Daily fallbacks or stale procedural docs/skills as new authority.

## Safety and source contracts

- Use synthetic data and public-safe configuration only. Never commit credentials, private
  Azure/ADO or provider IDs, endpoints, raw traces, model payloads, work items or customer data.
- Formal qualification invokes deployed Agent endpoints. Application Insights is read-only:
  no direct trace injection or destructive telemetry cleanup.
- Keep registries, evidence, checkpoints, logs, assessments and delivery records under the
  environment-separated `$HOME\.aiq-runtime\agent-insights-quality\` root, never in Git.
- The canonical registry stays in Sweden g30's private `deployment-registries` container.
  Validate downloads before replacing the local cache; never fall back to legacy storage.
- ADX accepts only an allowlisted public-safe projection, not raw logs/evidence/model output.
  Validate values as well as field names. Preserve historical reports and legacy West US resources.
- `catalogs/AGENT_CATALOG.yaml` and `catalogs/ISSUE_CATALOG.yaml` own the reviewed inventory.
  Source, scenarios, expectations, schemas, infrastructure and scoring changes require normal review.
- Each version owns complete deployable source: Prompt `definition.json` or Hosted `source/`.
  Keep one causal defect per issue; no runtime defect selectors or build-time source patching.
- Prompt Agents remain pure Prompt: no tools, tool fixtures or emitted function calls.

## Execution design

- Python owns bounded parallelism, retries, checkpoints, telemetry, assessment and email preparation.
  Copilot automation launches the runner and performs only the final app-native email send from its
  fixed recipient/HTML request. It does not orchestrate or assess qualification.
- Reuse the staging and Daily Sweden Central Accounts/Projects and their Agent objects, with
  separate g30 telemetry. Change versions/build artifacts only when deployment inputs change.
- Staging selects changed, missing or incomplete targets. First use or explicit full staging
  covers all 45 targets with ten attempts each. No deployed paired-v0 traffic; matched baseline
  comparisons belong in local Hosted tests. Retain unchanged results with their actual source/date.
- An explicitly authorized `run-staging --target <catalog-key> --new-run` measures only that
  target's ten attempts. Its repeated command resumes even after FAIL/INCOMPLETE; another fresh
  request requires `--after-run <actual-targeted-run-id>` and resolved prior work, never resampling.
- Baselines need eight adequately evidenced healthy attempts and no proven healthy-contract
  violation. Deterministic issues need eight proven observations and no proven deterministic-contract
  violation; probability-tolerant issues use their reviewed threshold, currently eight of ten.
  Assess all ten, distinguish PASS/FAIL/INCOMPLETE, and never resample misses to force a pass.
  Record the staging policy used; retain historical six-of-ten results without relabeling them.
- Staging root hygiene separately rejects proven independent extra Agent defects. Material
  unresolved candidates or inadequate evidence stay INCOMPLETE; legacy hygiene is NOT_EVALUATED.
  Same-root symptoms and correctly handled operational failures are not extra Agent defects.
- Staging creates no Insights runs, Daily score, team email or quality-publication rows.
  There is no staging PASS admission gate, digest chain, promotion receipt or staging-to-Daily
  approval ceremony. Ordinary source review remains required.
- Daily runs five concurrent Agent lanes, each baseline then four rotated issues sequentially:
  25 version units and 20 planned issues. New official runs use latest main and fresh traffic.
- Daily readiness is six distinct attributable probe attempts with invocation traces out of ten,
  not ten perfect responses, complete child trees or repeated staging behavioral assertions.
- Correlate actual endpoint responses, turns and trace scope without conflating response/model/
  operation IDs. Collect batched raw evidence per target; disclose gaps, truncation and ambiguity.
  Save the evidence visible before Insights starts; later evidence cannot prove earlier visibility.
- Assessment calls the explicitly configured deployed model directly with raw evidence and reviewed
  expectations (staging defaults to GPT-5.6 Sol; new Daily runs may use a private assessor override).
  Freeze the assessor per run; a model change requires new judgments, not relabeling old output.
  Validate structured judgments/citations; a card's claim cannot independently prove its defect.
- Persist per-turn results and small atomic source/provider/stage checkpoints. Resume matching
  unfinished work; repair only affected units. Reconcile ambiguous remote outcomes before retrying,
  and never repeat completed traffic, Insights starts or email blindly.
- Append private `runner.log` and `events.jsonl` with starts, heartbeats, retries and outcomes.
  Safe ADX event delivery has an independent best-effort outbox. Surface logging failures;
  inability to persist a side-effect checkpoint stops further unsafe side effects.

## Correctness, coverage and delivery

- Core diagnosis, reasonable category and independent current evidence determine correctness.
  Severity, wording and suggested fixes are diagnostic only. One issue earns at most one detection.
- Noise is a confirmed core-incorrect card; Duplicate is an extra distinct correct card for the
  same root cause. Never count one card as both. Page copies and same-ID updates are not duplicates.
  An unexpected real finding is neither Noise nor an extra correct expected issue.
- New-score policy v2: `100 * C / (C + N_scored + 0.5 * D_scored + 0.25 * M)`,
  where `M = E_scored - C`. Noise weighs 1, Duplicate 0.5 and Miss 0.25.
  Scorable baselines contribute penalties but no healthy bonus. Display one decimal, no threshold.
  Preserve v1 results under their recorded policy; explicit rescoring creates a separate derived
  result/report without new judgments, changed coverage or replacement of a prepared email.
- Full covers all planned units. Partial permits at most two unscorable baseline/issue units:
  exclude each wholly from score counts; show coverage, exclusions/reasons and unscored findings.
  More than two, no scorable issue or systemic integrity failure means no quality score/team
  report, only a private failure notice. Missing attempts are not the same as excluded units.
- All sinks use one result model. Work-item enrichment, ADX, private report storage and stateless
  improvement analysis are optional warnings, not reasons to rerun or block eligible inline email.
- Python publishes frozen MD/derived HTML/minimal manifests to the existing private
  `quality-artifacts` container, never repository reports or generated branches/PRs.
  Each Agent has its own compact MD/HTML report from the same result, without rescoring.
  Conditional writes/readback precede receipts and approved user-delegation SAS signing.
  Grants are blob-specific, HTTPS/read-only, up to seven days including key clock skew; never
  account-key SAS, role grants or public container access. SAS belongs only in private access
  records/email/previews, never uploaded reports, manifests, ADX, Git or logs. Expired unclaimed
  mail is blocked; explicit access refresh never rewrites or resends it. Keep normal
  source/catalog/scoring reviews and historical public validation.
- Acceptance requires a real private TEST email with recomputable counts, evidence-backed gaps
  and disclosed coverage. Unified `run-daily --report-mode test --to-address` allocates and
  freezes a private nonzero rerun; legacy `--test-run --rerun` remains supported. TEST
  sends only to the private test recipient, and writes private reports/previews/evidence/logs only:
  no public report/trend, ADX writes, generated PR or official team email.
- Provider acceptance alone is not inbox-delivery proof. The USER enables weekday automation
  after acceptance and normal integration to main; never enable it as part of the private trial.
- Claim the prepared email request before the app sends it, then persist the send outcome.
  An interrupted/ambiguous send is reconciled, not retried blindly. Do not choose recipients or
  rewrite report content in the automation prompt.
- The single bootstrap accepts only REPORT_MODE (`test|official`) and one literal TO_ADDRESS.
  Python freezes source/mode/routing before providers. Pending/prepared/claimed/unknown automatic
  launches resume unchanged across midnight; only terminal email evidence permits a new TEST.
  Eligible unified official mail uses its frozen explicit To; official failures always use the
  separate frozen private fallback. Legacy eligible official mail retains TEAM_RECIPIENT.

## Local development

Default pytest collection is only `tests/unit`. Keep it fast and offline: small synthetic
fixtures, fake transports/clocks, no Azure/Hosted SDK imports, credentials, Docker or real sleeps.
Add actual domain/runner tests as replacement code lands; structural checks are not runtime proof.
Optional Hosted tests use the `hosted-test` extra and explicit collection outside the default
suite, exercising real business/instrumentation paths with external boundaries controlled.

Set `PYTHONPATH` to this worktree's `src`; confirm module resolution before Python commands:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
python -c "import agent_insights_quality; print(agent_insights_quality.__file__)"
python -m pytest
python -m ruff check src tests agents pyproject.toml
```

Use targeted checks; compile Bicep only for infrastructure changes. Never restore superseded
tests, prose constraints or compatibility flows to satisfy legacy validation.
