# Agent Insights Quality

`agent-insights-quality` is a public, reusable daily qualification platform for the Agent Insights
engine. It exercises controlled synthetic Foundry agents through their deployed endpoints, validates
the resulting insights against reviewed contracts, and preserves a sanitized quality history.

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

The scheduled Copilot automation follows
`.github/skills/agent-insights-quality-daily/SKILL.md`. It creates a reproducible plan, runs healthy
and faulted traffic, invokes the production engine, applies deterministic and Copilot judgments,
reconciles quality memory and ADO bugs, commits only allowlisted generated paths, and sends the final
email. Qualification uses the protected test-recipient variable; promotion to the protected
production-recipient variable requires a human-reviewed configuration change.
