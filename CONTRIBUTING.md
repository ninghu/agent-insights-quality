# Contributing

Agent and Issue catalogs are human-reviewed contracts. Changes to an Agent implementation or issue
require:

1. catalog and schema validation;
2. deterministic packaging tests;
3. staging deployment of the exact content digest;
4. full 36-issue staging qualification;
5. human review before daily promotion.

Live operators use the shared protected infrastructure. Provisioning requires Storage Blob Data
Contributor on the private registry container; qualification requires Storage Blob Data Reader.
Registry blobs synchronize into `~/.aiq-runtime/agent-insights-quality/` and must never be committed.

Run:

```powershell
python -m agent_insights_quality generate-docs
python -m agent_insights_quality validate
python -m pytest
python -m ruff check .
az bicep build --file infra\main.bicep --stdout
```

Do not add compatibility readers for superseded formats. Keep changes synthetic and public-safe.
