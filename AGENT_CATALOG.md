# Agent Catalog

<!-- Generated from catalogs/AGENT_CATALOG.yaml; do not edit. -->

Each Agent owns one complete baseline and its reviewed single-root issue versions.
Validation modes are reviewed data, not inferred from observed model behavior.

| Agent | Owner | Type | Framework | Model | Terminal evidence | Semantic assertions | Trace operations | Validation | Issue count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: |
| `weather-agent` | Billy Hu | `prompt` | `foundry_prompt` | `gpt-5.4-mini` | `direct_prompt` | `required_per_request` | `uniform` | `baseline` | 6 |
| `healthcare-agent` | Ilya Matiach | `prompt` | `foundry_prompt` | `gpt-5.4-mini` | `direct_prompt` | `required_per_request` | `uniform` | `baseline` | 6 |
| `finance-agent` | Han Che | `hosted_code` | `microsoft_agent_framework` | `gpt-5.4-mini` | `standard_assistant_message` | `required` | `uniform` | `baseline` | 8 |
| `travel-agent` | Sean Gayler | `hosted_code` | `langgraph` | `gpt-5.4-mini` | `standard_assistant_message` | `required` | `uniform` | `baseline` | 8 |
| `support-ticket-agent` | Nishal Dsilva | `hosted_custom_container` | `custom_responses` | `gpt-5.4-mini` | `explicit_span_attributes` | `required` | `required_per_request` | `baseline` | 8 |
