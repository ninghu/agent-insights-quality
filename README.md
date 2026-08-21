# Agent Insights Quality

`agent-insights-quality` is a public, reusable qualification framework for the Agent Insights engine.
The current `0.1.x` release is **repository-contract scaffolding**, not a runnable daily automation
runtime. It defines reviewed schemas, manifests, safety policy, generated-path enforcement, skills,
and validation tooling while the deployment, traffic, production orchestration, judging, ADO, and
email workstreams are still incomplete.

The quality bar is intentionally strict. A day is `AT BAR` only after the complete active catalog
runs, healthy agents produce no insights, all structural checks pass, high-severity recall is 100%,
overall recall is at least 90%, precision is at least 95%, accepted insight attributes are all
correct, and duplicate, umbrella, and stale-version rates are zero. Incomplete or untrustworthy
runs are `INCONCLUSIVE`, never passes.

## Local development

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m agent_insights_quality validate
python -m pytest
```

Authoritative inputs live in `agents/**/manifest.yaml`, `scenarios/catalog.yaml`, `config/`, and
`schemas/`. After changing an agent, scenario, or quality-memory contract, run:

```powershell
python -m agent_insights_quality generate-docs
python -m agent_insights_quality validate
```

Use the repository skills under `.github/skills/` to onboard an agent or scenario and to replay a
run safely. Generated documentation must never be edited by hand.

## Security and privacy

Only generated synthetic data and public-safe configuration are permitted. Do not commit credentials,
tokens, Azure subscription or tenant identifiers, internal endpoints, raw traces, complete prompt
payloads, private ADO content, or real customer, health, or financial data. Credentials and private
resource identifiers must be supplied at runtime. Traffic must invoke deployed agent endpoints;
Application Insights is read-only evidence storage, and direct telemetry injection is forbidden.

## Daily automation

`config/runtime-readiness.yaml` is the fail-closed authority. Today all mandatory runtime phases are
false, so `python -m agent_insights_quality check-runtime-readiness` and `run-daily` return an
actionable `INCONCLUSIVE` result without deploying, sending traffic, modifying ADO, or sending mail.

After every mandatory component is implemented, validated, and human-reviewed, the scheduled Copilot
automation will follow `.github/skills/agent-insights-quality-daily/SKILL.md`. Qualification uses the
protected test-recipient variable; promotion to the protected production-recipient variable requires
a separate human-reviewed configuration change.
