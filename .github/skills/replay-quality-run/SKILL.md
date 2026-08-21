---
name: replay-quality-run
description: Reproduce a quality run without changing reviewed ground truth or durable state.
---

# Replay a Quality Run

1. Select an existing sanitized date/plan hash and retrieve the exact private retained artifacts
   through authorized runtime access. Verify plan, catalog, build/model labels, prompt hash, agent
   digests, and traffic seeds before running.
2. Use a new rerun project `aiq-YYYYMMDD-rNN`. Resolve exact private resource identifiers at runtime.
   Persist its artifacts under `reports/daily/YYYY/MM/DD/aiq-YYYYMMDD-rNN/`; never overwrite the
   original date-level plan or report.
3. Recreate immutable versions and invoke deployed endpoints only. Never inject traces.
4. Use the original assignments, ordering, seeds, half-open windows, evidence projection, deterministic
   checks, and judge contracts. Mark any unavailable dependency `INCONCLUSIVE`.
5. Default to no ADO, memory, email-audience, ground-truth, or source-manifest mutation. A replay may
   emit sanitized diagnostics under its rerun record.
6. Promote replay results into durable memory or bug actions only through an explicit human-reviewed
   decision after all normal gates pass.
7. Validate all outputs and the generated-path allowlist. Never commit private identifiers, raw
   traces, credentials, private ADO content, or real data.
