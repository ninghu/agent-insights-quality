Run one explicitly requested NEW private Daily email trial from the committed candidate worktree.

## Human-provided run inputs

Before submitting this template, the operator replaces the two quoted placeholders below.
TEST_TO_ADDRESS is exactly one literal private TEST mailbox; NEW_POSITIVE_RERUN is a new
positive integer for this explicitly authorized measurement. Keep the filled copy private,
not in Git or shared command logs. Do not infer an address from conversation text, account
identity, contact lookup or a report. No address lists, display names, team mailbox or official
recipient override are allowed. If either placeholder is unfilled or invalid, stop before
starting any traffic; do not fall back to configuration to repair an explicit invalid input.
Pass the address as one literal argument, never evaluated code. Inside the PowerShell
single-quoted value, escape any literal single quote as two single quotes.

Read AGENTS.md and set PYTHONPATH to the candidate's src; confirm module resolution.
Do not fetch/replace the candidate with old main. After normal integration, an explicitly
requested app TEST may instead use the fresh latest-main automation worktree.

```powershell
$testTo = '<TEST_TO_ADDRESS>'
$rerunText = '<NEW_POSITIVE_RERUN>'
$rerun = 0
if ($testTo -eq '<TEST_TO_ADDRESS>' -or [string]::IsNullOrWhiteSpace($testTo) -or
    -not [int]::TryParse($rerunText, [ref]$rerun) -or $rerun -lt 1) {
    throw 'Fill TEST_TO_ADDRESS and NEW_POSITIVE_RERUN before starting any traffic.'
}
python -m agent_insights_quality run-daily --test-run --rerun $rerun --fresh-traffic --test-to $testTo
```

The runner validates the single address and durably freezes it under runtime ownership before
providers or traffic. The filled placeholder is run input, not permission for the app to choose
or replace the prepared To address. Official eligible reports still use the fixed team mailbox;
official failures use their frozen private fallback. This TEST template never selects official mode.

## Resume and exact delivery

For recovery instead, inspect the existing process/checkpoints and resume the original
identity, source, recipient and frozen intent; do not allocate another rerun or resend completed traffic.
Use the same literal TEST address, or omit `--test-to` to retain the already frozen destination.
Editing this template or the default configuration must never overwrite original prepared mail.
A conflicting recipient/mode is a blocker, not a request to reroute that rerun. A legacy run with
unknown identity or no recoverable recipient must surface its blocker, not silently take a new default.
Python owns the entire qualification, scoring and private report storage pipeline.
Python alone signs the user-approved read-only per-Agent SAS links, with an exact expiry
and forwarding warning. The app must not upload, mint/refresh links, rewrite the email,
or claim/send an expired request. A bare Blob reference is not itself a browser login viewer.

Use the returned private email record only. Claim it with one opaque claim ID before sending.
Read the exact recipient, subject and HTML from the successful claim's private request path.
Use the app's native email capability once in HTML mode, then record its actual provider result
with `email-result`. Never invent a receipt or retry a potentially submitted email.

Do not send a team report, write ADX, publish repository reports/trends, create a PR or enable
automation. Do not advance official latest or log the address/filled command in shared outputs.
Retain numeric coverage and actual excluded-unit reasons from the prepared
report; do not add visible Full/Partial or quality PASS/FAIL labels.
Surface blockers immediately; the trial is not complete merely because an HTML preview exists.
