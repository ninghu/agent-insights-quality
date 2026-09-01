---
name: test-agent-validation
description: Validate all fixed Test Agent authorities locally for explicit human approval.
license: MIT
---

# Test Agent Validation

Use this skill for the report-free Sweden Central staging gate. It never runs Agent Insights,
assessment, scoring, reporting, ADX, email, Daily traffic, approval, or merge.

1. Freeze one reviewed clean commit and run
   `python -m agent_insights_quality prepare-test-agent-validation`. Prepare binds the exact durable
   `aiq-staging-swedencentral` Account and Project, reconciles all 41 catalog authorities and shared support
   images, and sends no test traffic. Reuse a matching stable Agent and exact server-assigned
   version. Changed content creates a new exact version under the same stable Agent name; never
   select `latest`.
2. Deterministically assign every catalog authority exactly once across 10 shards. Pass each
   assignment explicitly with repeated `--authority` arguments; never invent Agent names or embed a
   fixed shard map in code.
3. Launch no more than eight concurrent
   `invoke-test-agent-validation-shard --cycle-id <id> --shard <1-10> --authority <id> ...`
   processes. Each shard invokes only its assigned authorities, sequentially, including each
   issue's paired `v0`, and writes private endpoint/window/version bindings. It does not query
   telemetry or decide results.
4. Wait for all 10 invocation shards. Then launch no more than four concurrent
   `verify-test-agent-validation-shard` processes with the exact same cycle, shard, and authority
   arguments. Verification sends no traffic. It performs exact read-only post-invoke trace
   hydration/correlation and writes an independently bound shard package.
5. Run `compose-test-agent-validation --cycle-id <id>`. Composition requires exactly 10 packages,
   exact repository/PR/commit/digest/Project/topology bindings, and nonoverlapping coverage of all
   41 catalog authorities. Missing or mismatched evidence fails closed.
6. Run `cleanup-test-agent-validation --cycle-id <id>` after composition, or add
   `--shard <n> --authority <id> ...` for lane-scoped recovery. Cleanup removes only run-scoped
   sessions, responses, and temporary artifacts. The run-scoped cleanup never deletes stable Agents, versions, Hosted
   identities, blueprints, deployments, runtime principals, the durable Project, or telemetry.
7. Stop for human review of the exact composed evidence and CLEAN proof. Only after explicit
   approval may `approve-test-agent-validation` create the minimal immutable Sweden `g30` record.
   Daily promotion remains separate and sends no smoke traffic.

Keep all lifecycle, shard locks, packages, response references, and evidence under the private
durable `~/.aiq-runtime/agent-insights-quality/test-agent-validation/` root. Never write
credentials, private Azure identifiers, raw traces, complete payloads, or private context to Git.
The preserved West US 2 environment and lifecycle are never modified.
