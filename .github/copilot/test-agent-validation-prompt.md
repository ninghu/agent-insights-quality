# Official Sweden Staging Qualification

Read and follow `.github/skills/test-agent-validation/SKILL.md`. This is the current official
staging qualification for new candidates. Validation is local and report-free; GitHub runs only
ordinary mechanical CI.

Run `python -m agent_insights_quality run-test-agent-validation` from one reviewed clean commit.
Before mutation, verify the exact open PR head, authenticated Azure CLI user, measured quota and
headroom, read-only Sweden `g30` telemetry access, and the durable
`aiq-staging-swedencentral` Account and Project.

The coordinator must atomically publish one immutable desired-state assignment, release its global
lock, and distribute each content-changed authority exactly once across at most eight disjoint
asynchronous deployment workers. Each authority has a unique runtime Agent identity. Workers may
exact-reuse or deploy their assigned version and return immutable readiness receipts, but they
never publish shared lifecycle, topology, or registry state. After the barrier, the coordinator
centrally re-reads all 41 versions, verifies zero monitors and exact Project/telemetry bindings,
then atomically publishes the sole reconciled registry.

Invoke only selected deployed endpoints, with at most eight workers, and verify with at most four
read-only telemetry workers. Select changed content, prior FAIL/incomplete results, and authorities
without valid exact PASS evidence. Reuse other PASS evidence only under exact source/content,
provider-version, mapping, environment, and shared-contract bindings. Correlate every response to
one unique exact-name/version `invoke_agent` anchor and its complete descendant tree. Reject
orphaned, cyclic, duplicate, conflicting, cross-root, or late contradictory spans.

Keep required content-addressed history, desired state, receipts, registries, and evidence under
`~/.aiq-runtime/agent-insights-quality/test-agent-validation/`. Starting a new validation records
`SUPERSEDED` and atomically swaps active state without deleting any provider object or evidence.
Never create a monitor, run Agent Insights, assess or report cards, publish ADX, send email, run
Daily, or write validation lifecycle/evidence to Blob.

The successful result creates no approval artifact. Only after explicit user approval may the
separate `approve-test-agent-validation` command re-read the exact PR head and READY evidence, then
create the one minimal immutable approved Blob record. Merge remains manual.
