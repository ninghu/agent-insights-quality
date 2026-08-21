# Security Policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or exposed secret. Use GitHub's private
vulnerability reporting for this repository. Include the affected version, impact, minimal
reproduction, and suggested mitigation without adding real credentials or customer data.

## Data boundary

This repository accepts synthetic data and public-safe contracts only. Never commit credentials,
private Azure or ADO identifiers, internal endpoints, raw traces, private work-item content, complete
production prompt payloads, or real customer, health, or financial data. If sensitive content is
committed, rotate or revoke it through the owning service and follow the repository's private
security process; deleting it from a later commit is not sufficient.
