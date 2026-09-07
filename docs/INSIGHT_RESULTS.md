# Interpreting Insight results

Read the numeric score alongside scored/planned coverage, exclusions and confirmed quality gaps.
A high score with excluded units does not establish the same coverage as a fully measured run.
Full/Partial remain internal coverage classifications, not visible report labels.

The adapter captures detailed cards before and after each Insights run. Current contributions are
separate from unchanged history. Cards do not need a per-card run ID, and their links may accumulate
across runs; historical links alone neither prove a current issue nor make a card incorrect.

Each current card is assessed against independent endpoint/raw-span evidence. A correctly detected
issue may coexist with Noise or Duplicate cards. A true bug in a supposedly healthy baseline is a
real Agent finding, not an automatic false positive.

Review a card's central causal claim and affected surface before secondary fields.
Minor wording, a nearby reasonable category, disputed severity, an incorrect fix or
one weak example does not automatically invalidate an independently supported root.
Any error used to justify Noise must materially contradict the diagnosis or its
current evidence scope.

Internal component behavior can be a real finding even when its output is not
returned to the user. For example, unnecessary padding in an internal concision
review is not disproved by a concise endpoint answer. But a claim that the padding
replaced the delivered answer must match the real endpoint. Do not invent that
stronger claim, or reinterpret an explicitly false external claim as internal.
Keep genuinely unresolved scope uncertain. Saved reasons identify the claim,
surface, evidence and any material contradiction; this does not change old judgments.

Successful recovery does not erase a real dependency failure. An operational card
that accurately reports the dependency issue and acknowledges recovery may be correct,
even if no Agent fix is needed or the fault was deliberately exercised. Distinguish
that from a false claim that the Agent failed to recover.

Confirm that evidence was visible for the actual analysis window before calling a missing card an
Engine gap. Late telemetry, untriggered Agent defects, malformed execution and incomplete evidence
must not be blamed on the Insight Engine.

Review the private per-Agent report's five version rows alongside its saved classifications and
short Notes. Normal detections and healthy baselines need no Notes. Assessment labels match the
generated-card numbers: **1. Matched**, **2. Noise**, **3. Duplicate**, and so on.
**Unexpected** is a saved valid non-target finding, without expected-detection credit or an automatic
Agent-fix recommendation. **Missed** means Insights did not diagnose the expected defect.
**Unconfirmed** rows are excluded from scoring and are not counted as misses.
Verified Agent headings link directly to the actual Foundry Agent; missing identities are not guessed.
Top-level score, counts, coverage and reading guidance are a short bullet list.

Expand **Assessment details** in an exceptional row to read the full saved rationale. Disputed
assessments show both the initial and focused-review rationales and remain wholly unscored when
their resolved core is unknown. These escaped private quotations are not new judgments; the
report never changes saved classifications, scores or archives. Public reports, email briefs
and ADX do not include these private rationales.
Expanded details retain each finding's saved core and classification alongside the quoted reasons.

Detailed reproduction/evidence references stay private. New reports are archived in private
Storage; approved expiring links grant read access to individual Agent files. They are not public
reports or generated-report PRs. Historical public reports remain unchanged, and optional official
ADX receives only its allowlisted projection. See [Quality rules](QUALITY_BAR.md) for score and
exclusion semantics and [Operations](OPERATIONS.md) for report access and recovery.
