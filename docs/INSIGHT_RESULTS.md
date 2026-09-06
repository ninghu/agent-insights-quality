# Interpreting Insight results

Read the numeric score alongside coverage and confirmed quality gaps. A high Partial score does not
establish the same coverage as a Full score.

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

Confirm that evidence was visible for the actual analysis window before calling a missing card an
Engine gap. Late telemetry, untriggered Agent defects, malformed execution and incomplete evidence
must not be blamed on the Insight Engine.

Detailed reproduction/evidence references stay private. Public reports contain approved aliases,
counts and explanations only. See [Quality rules](QUALITY_BAR.md) for score and exclusion semantics.
