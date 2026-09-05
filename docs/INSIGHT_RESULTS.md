# Interpreting Insight results

Read the numeric score alongside coverage and confirmed quality gaps. A high Partial score does not
establish the same coverage as a Full score.

The adapter captures detailed cards before and after each Insights run. Current contributions are
separate from unchanged history. Cards do not need a per-card run ID, and their links may accumulate
across runs; historical links alone neither prove a current issue nor make a card incorrect.

Each current card is assessed against independent endpoint/raw-span evidence. A correctly detected
issue may coexist with Noise or Duplicate cards. A true bug in a supposedly healthy baseline is a
real Agent finding, not an automatic false positive.

Confirm that evidence was visible for the actual analysis window before calling a missing card an
Engine gap. Late telemetry, untriggered Agent defects, malformed execution and incomplete evidence
must not be blamed on the Insight Engine.

Detailed reproduction/evidence references stay private. Public reports contain approved aliases,
counts and explanations only. See [Quality rules](QUALITY_BAR.md) for score and exclusion semantics.
