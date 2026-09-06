Run one explicitly requested private Daily email trial from the candidate worktree.

Read AGENTS.md and set PYTHONPATH to the candidate's src; confirm module resolution.
Do not fetch/replace the candidate with old main. Invoke
`python -m agent_insights_quality run-daily --test-run --rerun <nonzero-test-number>`.
Python owns the entire qualification, scoring and private report storage pipeline.
Python alone signs the user-approved read-only per-Agent SAS links, with an exact expiry
and forwarding warning. The app must not upload, mint/refresh links, rewrite the email,
or claim/send an expired request. A bare Blob reference is not itself a browser login viewer.

Use the returned private email record only. Claim it with one opaque claim ID before sending.
Read the exact recipient, subject and HTML from the successful claim's private request path.
Use the app's native email capability once in HTML mode, then record its actual provider result
with `email-result`. Never invent a receipt or retry a potentially submitted email.

Do not send a team report, write ADX, publish repository reports/trends, create a PR or enable
automation. A Partial result must retain its coverage label and excluded-unit reasons.
Surface blockers immediately; the trial is not complete merely because an HTML preview exists.
