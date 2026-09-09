# Evidence and quality rules

These rules apply to the replacement runner. Historical reports retain their recorded scoring
and coverage policies; do not reinterpret an older report using newer weights or requirements.

## Staging

Staging normally selects changed, missing or incomplete targets. First use or an explicit full run
covers five baselines and 40 issues with ten attempts per target. There is no deployed paired-v0.

Baselines require eight adequately evidenced healthy attempts with no proven healthy-contract
violation. Deterministic issues require eight proven defect observations with no proven contradiction
of the deterministic contract. Probability-tolerant issues also require eight observations out of ten.
Evaluate all ten attempts, not just the first eight successes.

Missing evidence is not the same as a behavior failure. Preserve PASS, FAIL and INCOMPLETE
separately, and never resample a behavioral miss until it passes.

Retained span totals do not establish execution counts when telemetry is missing.
Reconcile nested instrumentation and copied records before counting executions. An attributable
function-completion log can survive the loss of its parent span; per-turn assessment
context points to such owned logs without synthesizing spans or granting new citation
authority. Reconcile actual completion records, arguments and structured outcomes before
judging retry counts. A model tool-call ID can cover repeated physical executions.
Fewer retained spans alone do not prove fewer calls: unresolved count evidence is
INCOMPLETE, while independently proven behavior can remain scorable despite unrelated
capture gaps. A complete query does not certify complete telemetry retention.

For example, seven observations plus three sufficiently evidenced nonobservations fail the
observation threshold. Six observations plus four insufficient attempts are INCOMPLETE, not a
proven behavioral failure. Eight healthy observations cannot hide a proven baseline violation.
Record the policy with each assessment. Historical six-of-ten judgments remain historical;
apply the new policy to retained valid evidence without overwriting the original result.

### Scoped single-root hygiene

`staging-root-hygiene-v3` retains the eight-of-ten observation minimum and adds a separate
root-hygiene judgment in the same bounded staging assessment calls. Expected activation
still requires probe proof. Additional findings may cite a paired endpoint and attributable
trace from the same setup or probe turn, only within their owned attempt and partition.
Unowned or sibling rows do not become proof by sharing an operation ID; attributable
causal child-Agent work remains context.

An issue must adequately exhibit its expected causal defect without an independently proven
additional Agent defect in the scoped runtime evidence. Baselines have zero injected roots.
Multiple symptoms, exceptions or categories do not mean multiple causes. A separate proven
Agent defect fails hygiene even when the expected defect is observed and even for
model-mediated issues. A missed expected activation is not renamed an additional defect.

Private `additional_findings` records retain relation, central cause, actual behavior,
violated healthy contract, causal-independence explanation, affected component/surface,
material impact, unresolved evidence and current citations. Relations distinguish independent
Agent defects, consequences of the expected root, handled behavior/operational observations,
and genuinely unresolved additional roots. Correct fallback with real dependency errors can
be a true operational observation without an Agent-contract violation. Internal quality defects
need demonstrated surface and material impact; fewer selected options alone is not proof.
Model self-labels, catalog claims and diagnoses' own prose are not independent evidence.

Only proven independent Agent defects fail hygiene. A credible material additional-root
candidate with cited behavior but essential unresolved evidence makes it INCOMPLETE, not
a failure or silent pass. Generic improvement ideas and uncited assertions are not candidates.
An empty finding list cannot establish hygiene when fewer than eight attempts have
adequate attributable evidence; hygiene remains INCOMPLETE alongside the evidence gap.
All partition records remain private with their input/output provenance; repeated observations
of one root earn no bonus or separate score. No extra unconditional AI pass, resampling,
deployed paired baseline, Insights run or public/team finding publication is added.

`root_hygiene_status` is separate from overall status and expected observation counts.
Historical `staging-observations-v2` (eight) and older six-observation records keep their actual
policy/status/date. Their absent hygiene fields mean `NOT_EVALUATED`, never a v3 hygiene PASS.
A pure threshold reaggregation cannot establish the new causal contract: new v3 eligibility
needs assessment of retained valid evidence under the new schema/prompt, without rewriting
history or automatically repeating traffic. Offline fakes verify schema, policy, ownership,
merge and recovery mechanics, not semantic model accuracy. Deployed staging acceptance of
these causal/materiality judgments remains required.

## Daily readiness and assessment

Daily plans 20 issues and five baselines. Six distinct attributable probe attempts out of ten
establish telemetry readiness, not defect correctness. It does not require every child span or
repeat staging's deep behavioral checks before Insights.
The eight-observation staging policy does not raise this six-attempt Daily readiness requirement.

The reviewed inventory has 40 issues, including nine `safety_guardrails` cases. Issues
037-040 extend coverage to a bounded unsafe-request refusal decision, untrusted-input
instructions, synthetic sensitive-field redaction and benign-request overblocking.
They use non-actionable synthetic inputs and outputs; this is application-guardrail
coverage, not a claim of comprehensive content safety or foundation-model safety.
Each case still owns one root cause and receives no extra scoring weight or Daily slot.

Daily continues to rotate four issues per Agent. The largest inventories now contain
ten issues, so complete inventory exposure requires up to three consecutive weekdays,
not two. Historical public artifacts reconstruct their selected units from both catalogs
at their trusted source commit; a larger current inventory does not change an older plan.

Core diagnosis, reasonable category and independently supporting current evidence determine
correctness. Severity and suggested fixes are diagnostic only. A candidate gap receives bounded
review using already-generated evidence, not new Agent traffic.

- **Detected:** a correct card identifies the expected defect; each expected issue counts once.
- **Noise:** an in-scope card's core judgment is demonstrably incorrect, including wrong diagnosis,
  material category error or wrong evidence.
- **Duplicate:** an otherwise correct extra distinct card repeats the same causal problem.
- **Unexpected real finding:** a supported problem outside the expected issue, including a genuine
  baseline defect; it is not Noise and does not inflate expected-issue detection.
- **Unconfirmed:** evidence cannot establish a reliable core verdict.

The same card is never both Noise and Duplicate. Repeated incorrect cards remain Noise. Same-ID
updates, reopenings and transport duplicates are not additional duplicate cards.

Retained categories are not remediation instructions. Human review must distinguish an unresolved
Agent defect from an ambiguous claim and from correctly handled recovery (no Agent change needed).
Disagreement found during report review is documented for review, not silently rescored.

Daily distinguishes outer Agent response instrumentation from actual model/tool work.
An `invoke_agent` wrapper can record input, assistant text and successful completion
around an early application return; `genAIContent` is not independent proof of inference.
Conversely, absent child spans or zero token usage alone do not prove a failure or no model
work. Ground the claimed component and cause in attributable operation types, actual responses
and independent current evidence, not an assumed platform fault.

An unestablished requirement is not automatically a proven wrong core. A factual omission
needs an applicable obligation or independently established impact to become a real defect;
explicit requirements and valid implicit business obligations remain authoritative.
Essential unresolved authority means Unconfirmed, not automatic Noise or Unexpected.
Confirmed Noise still requires independent contradiction of the central claim.
Meaningful failure disclosure and honest aggregate scope matter, not the literal word
“partial”; proposed-fix wording is neither independent defect evidence nor a core verdict.

The bounded review remains fail-closed. Opposing activation judgments with sufficient
pre-Insights proof exclude the whole unit as assessment disagreement, not fabricated missing
raw evidence. Actual query, execution, attribution and visibility gaps remain disclosed
alongside disagreement. This diagnostic distinction changes neither coverage limits nor
score counts and never rejudges or rewrites a saved result.

### Contract authority and benchmark fairness

Separate configured-policy noncompliance from a claim of business-outcome failure. A
legitimate developer-defined privacy, authorization or conditional business rule can
override an ordinary user request; satisfying that request alone does not prove healthy
behavior. A specific applicable exception is not automatically an instruction conflict.

Ground the applicable rule, scope, trigger and actual mismatch in evidence. Stronger
business-failure claims require the governing business obligation or independent impact.
Unresolved authority is uncertainty, not a reason to invent a defect or suppress a real
compliance finding. An unnecessary proposed fix alone does not overturn a supported core.

Revised synthetic cases must not require Insights to infer hidden injection intent. Keep
the healthy obligation visible and distinguish flawed examples or implementation behavior
from a mandatory contrary policy. Activation expectations describe the injected observation,
not a new healthy requirement. A compliant healthy output is a nonobservation of that defect,
not automatically another bug. These rules do not relabel historical assessments.

## Score and coverage

New measurements use precision-first scoring v2:

```text
M = E_scored - C
score = 100 * C / (C + N_scored + 0.5 * D_scored + 0.25 * M)
```

`C` is the number of distinct correctly detected scored issues. `M` is the number of missed
scored issues, not excluded units. Noise weighs 1, Duplicate 0.5 and Miss 0.25. Noise and
Duplicate include scorable baselines as well as issues; baselines add no healthy bonus.
Display one decimal using half-up rounding. There is no overall quality PASS/FAIL threshold.
A fully measured zero is valid; an unmeasured run is not zero.

For an illustrative arithmetic example with `C=8`, `E_scored=10`, `N_scored=1` and
`D_scored=1`, there are two misses and the v2 score is `100*8/(8+1+0.5+0.5) = 80.0`.
This is an example, not a published measurement.

Full covers all 25 units. Partial permits at most two unscorable baseline/issue units and at
least one scorable issue. Exclude each entire unit from all score counts, not just unfavorable
cards, and show planned/scored coverage, exclusions and reasons. Confirmed findings in excluded
units remain visible as unscored diagnostics.

More than two unscorable units, no scorable issue or systemic integrity failure produces no team
score/report and only a private failure notice. Compare trends with scoring policy and coverage
visible; Partial is not interchangeable with Full.

### Test-category scores

New Daily runs freeze each issue's reviewed `ISSUE_CATALOG.yaml` category in the private
`result-plan` checkpoint before provider work. `catalog-test-category-v1` partitions whole
issue units by that frozen category, not the labels or text of Insights cards. Every scored
unit's C/E/Noise/Duplicate counts belong to its test category, even if a noisy card describes
a different type of problem. Excluded units retain their category and unscored findings but
contribute no counts or misses.

Python computes and stores all eight category slices in the unified result's optional
`category_breakdown`, using the run's recorded scorer and one-decimal rounding. Each slice
includes planned/scored coverage and exclusions. A fully measured miss is 0.0; no scored
expected issue is null/N/A, including categories not planned that day. Ineligible runs have
no category scores, even if some category units completed.

Baseline counts and coverage form a separate global-penalties bucket, without a score or
healthy bonus. They are not apportioned to categories. The global score is unchanged and is
not the mean of the category scores. Category counts plus baseline counts reconcile to the
global totals. Small samples and daily rotation limit comparisons; a changed cohort must
not be presented as evidence of an Engine-caused regression.

Existing runs and results without category metadata remain uncategorized; neither resume,
restoration nor a dashboard lookup backfills them from today's catalog. Explicit rescoring
of a categorized result recomputes its category scores under the selected reviewed policy
without changing classification, category attribution, coverage or the prepared email.
Public category fields use the same closed vocabulary and whole-result reconstruction as
all other published counts. Dashboard queries only read these stored scores.

### Historical scoring policies

Version 1 remains valid for its recorded results:

```text
score_v1 = 100 * C / (E_scored + N_scored + 0.25 * D_scored)
```

Since `E_scored = C + M`, v1 implicitly weights a miss at 1 and a duplicate at 0.25.
Do not relabel v1 output as v2 or compare scores across policies as if the weights were
unchanged. Restoring an old result uses its recorded policy, not the current default.

An explicitly requested scoring preview may derive a new score from the same saved
classifications, counts and coverage. Preserve the original result, evidence and prepared
email; record both policies and result hashes with the derived preview. A weight change is
not a new measurement, reassessment, publication or email-send authorization.
