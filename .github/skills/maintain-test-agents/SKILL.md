---
name: maintain-test-agents
description: Repair or modify reviewed synthetic Agents and issue versions, with explicit scope approval for inventory additions or changes.
---

# Maintain test Agents and issues

Read [contributor boundaries](../../../AGENTS.md),
[the change workflow](../../../CONTRIBUTING.md), and
[evidence rules](../../../docs/QUALITY_BAR.md). The catalogs own the reviewed inventory.

First distinguish an existing-target repair from an inventory change. Adding, removing,
renaming or reassigning an Agent/issue requires explicit scope agreement, including
Daily rotation and scoring coverage. Do not silently expand or renumber the benchmark.

For an Agent change, establish its healthy behavior and review the affected independently
deployable versions. For an issue change, establish one independently fixable causal defect,
its activation scenario and independent endpoint/raw-span evidence. Do not change the
validation mode merely to fit an observed result.

Follow the contributor workflow for complete version-owned source, canonical traffic and
targeted local checks. Prompt Agents remain tool-free. After catalog changes, regenerate
the readable catalog views with `python -m agent_insights_quality generate-docs`, then use
`python -m agent_insights_quality validate`. Set `PYTHONPATH` to this worktree's `src` and
confirm module resolution before repository Python commands.

Live qualification requires explicit authorization and a committed candidate; use the
separate [staging skill](../staging-qualification/SKILL.md). Source maintenance does not
implicitly authorize deployment, new measurement traffic, publication, email or scheduling.
Keep runtime evidence private and preserve historical results.
