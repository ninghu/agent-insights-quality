---
name: test-agent-validation
description: Validate all fixed Test Agent authorities locally for explicit human approval.
license: MIT
---

# Test Agent Validation

Use this skill for local, report-free validation. It never runs Agent Insights, assessment, scoring,
report publication, ADX, email, Optional Daily Test, or a GitHub merge gate.

1. Freeze one clean commit after the comprehensive review and targeted mechanical verification. Run
   `python -m agent_insights_quality run-test-agent-validation` with no identity, path, or SHA
   arguments; it discovers the repository, open PR, exact head, and shared runtime root.
2. Freeze every reviewed mode before traffic. Baselines require five mechanically complete attempts;
   deterministic issue packages require five issue and five paired-`v0` attempts; model-mediated
   packages require seven of each. Expected issue observations remain context for later private
   Copilot review, never a local verdict or gate. Never infer mode, resample, reclassify from results,
   or lower an attempt count.
3. Hold the Sweden-environment OS file lock under
   `~/.aiq-runtime/agent-insights-quality/test-agent-validation/` for the whole invocation. A second
   worktree fails closed. Every journal update is atomic and every required history event is
   content-addressed and append-only.
4. Before mutation, verify the clean PR head, authenticated Azure CLI user, exact
   `aiq-staging-swedencentral` Account and Project, reviewed model, quota/headroom, read-only Sweden
   `g30` access, Project-scoped App Insights connection, and environment-namespaced lifecycle state.
   A pre-traffic partial deployment may resume only the unchanged commit, cycle, substrate,
   Project, and topology; every other incomplete journal resumes exact cleanup before a new cycle.
5. Reconcile 41 stable baseline/issue Agent names in the one durable staging Project. Persist and
   exact-select every server-assigned provider version ID and content digest; never route to `latest`.
   Phase 1 deploys only the fixed `weather-agent/v0` Prompt baseline and
   `finance-agent/v0` Hosted baseline and runs their official baseline
   attempts concurrently. Only when both pass may Phase 2 provision the remaining 39 at no more than
   eight and run all remaining official traffic in five Agent lanes.
   Retain confirmed-ready pre-traffic authorities and retry only unresolved authorities, with at most
   three recovered versions per Agent.
6. Run every fixed setup/probe attempt through deployed endpoints. Within each Agent, versions remain
   sequential; independent Agent lanes continue after an Agent-local failure. Use the identical issue
   matrix against paired `v0`. Query telemetry at no more than four and allow one scenario attempt per
   runtime. If an issue authority has a recoverable execution or evidence failure, retain and
   quarantine that provider version, keep the stable Agent name, reconcile a new server-assigned
   provider version for the same content digest, and bind replacement evidence to its exact operation
   and time window after bounded hydration/stability waiting. Never rerun completed authorities, splice
   superseded evidence, recover a baseline this way, or retry a pre-traffic source/contract failure
   that rebuilding cannot change. Application Insights remains read-only; shared runtime failure or ambiguous
   identity/correlation aborts. Each selected operation must contain an `invoke_agent` span with a
   nonempty canonical `gen_ai.output.messages` attribute; the span's telemetry table and parent do
   not matter, and chat, tool, or other spans cannot satisfy it. Never create monitors or inject
   traces.
7. Any commit change aborts the cycle, performs run-scoped cleanup, and requires a fresh full
   41-authority cycle. Traffic and evidence never cross cycles; stable Agent topology may be reused
   only when the exact provider version/content digest mapping is reconciled. Local dependencies and
   ACR build manifests may be reused only by exact content digest; cycle tags remain cycle-owned.
8. Delete every recorded response, conversation/session, cycle tag, and unshared manifest in reverse
   dependency order. Explicitly retain the durable Project, Project connections/RBAC, and stable
   Agent/version topology. Verify exact absence of run-scoped resources and every reviewed cascade.
   Ambiguity enters the Sweden-namespaced `CLEANUP_BLOCKED` state and never mutates the legacy pointer.
9. Keep lifecycle, history, evidence, and CLEAN files only in the shared private runtime root. The
   successful run writes no approval artifact.
10. After exact-clean evidence, Copilot may review the private trace rows externally before the human
    approval decision; this does not mutate lifecycle state or become a code-generated verdict. Only
    after the user explicitly approves the exact result, run
    `python -m agent_insights_quality approve-test-agent-validation`. It re-reads the current PR head,
    local evidence, and CLEAN proof, then exact-selects the dedicated Sweden `g30` storage account and
    create-once writes the one minimal immutable approved record. It never reads or mutates legacy
    storage. Merge remains manual.

Never put raw prompts, responses, traces, provider IDs, Azure IDs, or private context in Git.
