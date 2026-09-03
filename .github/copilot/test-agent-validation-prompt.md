# Official Sweden Staging Qualification

Read and follow `.github/skills/test-agent-validation/SKILL.md`. This is the current official
staging qualification for new candidates. Validation is local and report-free; GitHub runs only
ordinary mechanical CI.

From one reviewed clean commit, the visible Copilot coordinator runs
`python -m agent_insights_quality prepare-test-agent-validation`. Before mutation, verify the exact
open PR head, authenticated Azure CLI user, measured quota and headroom, read-only Sweden `g30`
telemetry access, and the durable `aiq-staging-swedencentral` Account and Project.

The visible Copilot coordinator must atomically publish immutable desired-state and phase
assignments, release its global lock, and remain responsive. Create visible Copilot sub-sessions for
all parallel deployment, invocation, and verification work. Never use subprocesses,
`ThreadPoolExecutor`, or another hidden in-process pool. Each non-empty phase independently publishes
one to eight deterministic, cost-balanced logical shards based only on selected authorities. Every
active shard maps 1:1 to one visible sub-session.

Each authority has a unique runtime Agent identity. Deployment sub-sessions receive disjoint
immutable assignments, exact-reuse or deploy only assigned versions, and persist per-authority
readiness receipts without publishing shared lifecycle, topology, or registry state. Stale
sub-sessions fail closed. After the barrier, centrally re-read all 41 versions, verify zero monitors
and exact Project/telemetry bindings, then atomically publish the sole reconciled registry.

Give each deployment sub-session exactly
`python -m agent_insights_quality deploy-test-agent-validation-shard --shard-id <N>`. After that
barrier, the coordinator runs
`python -m agent_insights_quality reconcile-test-agent-validation-deployment`, which publishes the
invocation and verification assignments. Give each invocation sub-session exactly
`python -m agent_insights_quality invoke-test-agent-validation-shard --shard-id <N>`.

Immediately after definitive authority completion, atomically publish its generation-fenced
invocation receipt. Bind exact Agent source, provider content, traffic-generation/execution,
provider-version, runtime, environment, Project, telemetry resource-set identity, response/session
references, invoke/evidence windows, complete issue and paired-`v0` provenance, source-artifact
schema/version, origin run/commit/generation/shard, and immutable artifact digest. Unknown,
ambiguous, duplicate, partial, or indeterminate retried-POST outcomes are not reusable.
Cross-generation reuse performs one atomic generation-fenced extraction; stale sub-sessions cannot
extract or publish it.

The next generation selects only changed, incomplete, or missing authorities. Within that
set, invoke only authorities without current exact-bound completed receipts; assign all others
verify-only work with no new endpoint traffic.

Verification begins after the invocation barrier, is read-only, and sends no traffic. Run at most
eight visible verification sub-sessions. Each claims one generation-fenced authority at a time,
finishes it before claiming another, and uses no internal concurrency, deployment, invocation, or
shared private prompt/CLI state. Each verification sub-session repeats the no-ID
`python -m agent_insights_quality prepare-test-agent-validation-assessment` and
`python -m agent_insights_quality import-test-agent-validation-assessment` cycle one authority at a
time. Claims are hidden, worktree-bound, distinct, and leased; status exposes only aggregate slots.
After the verification barrier, have the coordinator run
`python -m agent_insights_quality compose-test-agent-validation`.

For a baseline, create one batched stable telemetry snapshot covering all five attempts. For an issue,
create exactly two target batches: one stable snapshot for all issue attempts and one for all
paired-`v0` attempts. Never query or stabilize attempts independently. Correlate every response to one
unique exact-name/version `invoke_agent` anchor and its complete descendant tree. Reject orphaned,
cyclic, duplicate, conflicting, cross-root, or late contradictory spans.

Persist one immutable generation-fenced authority result immediately after deciding it and before
claiming another. Keep `PASS`, `FAIL`, and `INCOMPLETE` distinct. Apply baseline `5/5`,
deterministic `5/5` plus paired `v0` `0/5`, and model-mediated `>=5/7` plus paired `v0` `0/7`.
Complete stable evidence below threshold is `FAIL`; missing, ambiguous, partial, or unstable evidence
is `INCOMPLETE`. Later failures never discard completed authority results. Retry only missing,
`INCOMPLETE`, or exact-binding-changed authorities. Final composition requires exact current results
for all 41 authorities.
Receipt reuse proves the traffic-generation/execution binding only; every new verification package
binds the receipt's immutable digest and the current verifier commit and verifier digest.

Shard primitives accept only `--shard-id`, resolve the hidden active generation and immutable
assignment, and never accept run/generation IDs or authority IDs. Use
`run-test-agent-validation` only for status and next-action guidance; it never creates sub-sessions
or executes phase work.

Keep required content-addressed history, desired state, receipts, registries, and evidence under
`~/.aiq-runtime/agent-insights-quality/test-agent-validation/`. Starting a new validation records
`SUPERSEDED` and atomically swaps active state. There is no cleanup: never delete any provider object,
receipt, or evidence.
Never create a monitor, run Agent Insights, assess or report cards, publish ADX, send email, run
Daily, or write validation lifecycle/evidence to Blob.

The successful result creates no approval artifact. Only after explicit user approval may the
separate `approve-test-agent-validation` command re-read the exact PR head and READY evidence, then
create the one minimal immutable approved Blob record. Merge remains manual.
