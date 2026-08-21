# Contributing

Use Python 3.11 or newer and keep every contribution public-safe. Do not include credentials, private
resource identifiers, internal endpoints, raw traces, private work-item content, complete production
prompts, or real customer data.

1. Create a focused branch and update authoritative manifests or contracts.
2. Use the onboarding skills under `.github/skills/` for agents and scenarios.
3. Run `python -m agent_insights_quality generate-docs`.
4. Run `python -m agent_insights_quality validate` and `python -m pytest`.
5. Open a human-reviewed pull request. Source contracts, quality gates, skills, and reporting
   promotion must never be changed by daily generated automation.

Ground-truth changes must explain customer impact, compatibility, deterministic evidence, controls,
expected category/severity/fix, and version semantics. Healthy baselines remain immutable.
