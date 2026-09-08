# Operations

Use an authenticated local environment. Keep secrets, environment-specific configuration and
generated private artifacts under `$HOME\.aiq-runtime\agent-insights-quality\`, never in Git.
Public-safe runner settings in source `config/runtime.json` are different: review and commit
them before launch. Never bypass the source-integrity check with an ignored local override.

Set `PYTHONPATH` to the active source checkout before repository Python commands. Official automation
uses a fresh latest-main worktree; a private trial uses the candidate being evaluated.
Confirm module resolution and the intended Azure CLI subscription before live work. On a shared
machine, use an operator-prepared private `AZURE_CONFIG_DIR` for the environment rather than
changing the shared default subscription. Set it in each new process; keep its credentials private.

The [three repository skills](../README.md#skills) are thin workflow entry points. Source changes
follow [Contributing](../CONTRIBUTING.md); evidence, classification and scoring follow
[Quality rules](QUALITY_BAR.md). Neither skills nor app prompts replace the Python orchestrator.

## Entry points

```powershell
python -m agent_insights_quality validate
python -m agent_insights_quality generate-docs
python -m agent_insights_quality run-staging
python -m agent_insights_quality run-staging --full
python -m agent_insights_quality run-staging --target finance-agent/issue-019 --new-run
python -m agent_insights_quality run-daily --report-mode test --to-address "<TO_ADDRESS>"
python -m agent_insights_quality status --profile staging
python -m agent_insights_quality status --profile daily
```

`generate-docs` updates `AGENT_CATALOG.md` and `ISSUE_CATALOG.md`; it never rewrites traffic
or expected behavior. Runtime commands now use the replacement runner; there is no legacy
fallback. Production readiness still requires the candidate's deployed trial and TEST email.
Daily automation uses [one bootstrap](../.github/copilot/daily-bootstrap-prompt.md): only
REPORT_MODE (`test` or `official`) and one literal TO_ADDRESS, with no human test counter.
`run-daily --help` lists the unified and retained legacy forms; never mix their identity flags.
Eligible official mail uses its initialized, immutable To; official failure notices always use
the separately frozen private configuration fallback, not the official distribution.
Legacy official mail retains TEAM_RECIPIENT. See [routing and launch records](AUTOMATION_SETUP.md).

## Staging qualification and diagnostics

Run staging only on an explicitly authorized committed candidate. Python selects affected
targets and applies the recorded [staging and single-root policy](QUALITY_BAR.md#staging).
Expected activation and additional findings remain separate. Preserve FAIL, INCOMPLETE and
historical NOT_EVALUATED hygiene rather than interpreting a missing finding as a clean result.
The app must not add assessments or resample traffic to change a qualification outcome.

Staging does not stop collection at Daily's six-probe readiness threshold. Within the existing
hydration deadline, it waits for attributable roots for all completed setup/probe responses and
one further complete observation, giving related spans another opportunity to arrive. This is a
collection stopping rule, not a ten-perfect-attempt admission gate or proof that every child span
exists. At the deadline, the saved evidence is assessed as available under the unchanged
eight-observation policy; gaps remain explicit. Snapshot visibility is recorded after queries finish.
Evidence-only recovery preserves completed calls, the original deadline and earlier snapshots;
a later snapshot never proves that those records were visible earlier.
Orchestration/recovery changes alone do not request new judgments for completed PASS or FAIL
measurements. They retain their actual source/date. Changes to explicit assessment, expectation,
policy and evidence dependencies still select the corresponding reviewed reassessment work.
Daily-only assessor prompt changes do not requalify unchanged staging results. They still
refresh Daily judgments when a legacy trial explicitly reuses execution evidence.

The Daily wrapper/Noise guidance and semantic-disagreement diagnostics do not change staging
judgments or policy. The diagnostic code resides in shared `assessment.py`, however: changes
to that file conservatively select completed staging targets for retained-evidence reassessment
under the existing file-level dependencies, not redeployment or automatic new traffic.
Do not hide that selection by weakening dependencies. Offline semantic fixtures test frozen
judgment handling, not assessor reliability; revised prompts need a separately authorized
future Daily trial. Historical results and prepared reports retain their recorded reasons.

Use the [trace-context audit](#caller-invocation-context) on retained results for propagation
diagnostics. An explicitly authorized [session preparation trial](STAGING_PREPARATION_TRIAL.md)
can exercise the bounded Travel lookahead inside normal staging, followed by its read-only audit.
Neither audit changes a judgment, creates traffic or enables Daily. Staging generates no
Insights, Daily score, report email or quality-publication rows.

## Recovery

For unified automation, Python allocates a positive TEST identity above retained run/outbox
numbers and freezes its exact source, date, mode and destination before providers. The same
command resumes that identity across midnight while unfinished/prepared/claimed/unknown.
Only accepted/delivered/definitively rejected email evidence releases it for a later new TEST;
process completion and prepared mail are not authorization for another run. Never change
source, mode or To to bypass recovery. Official mode retains the weekday/date singleton.
Use the frozen source worktree for recovery, not a freshly advanced main.

Legacy manually numbered trials remain supported with `--test-run --rerun` and optional
`--fresh-traffic`. A new positive legacy identity with fresh traffic bypasses earlier-trial
reuse without deleting evidence. Its repeated command resumes frozen intent even when that
flag is omitted. An unfinished fresh run cannot silently switch source.

Repeat the same command to resume matching work. Do not manually invent generation IDs, clear
state, delete Agent objects or resend completed traffic. A code/scenario change selects affected
work; unchanged completed results retain their original provenance.

An issue-catalog entry-only edit re-evaluates that issue when Git proves the inventory, Agent
assignment and global catalog settings are unchanged. Unknown comparisons, inventory changes and
shared verifier changes remain conservative. Expectation-only traffic edits reuse matching raw
evidence; an explicit execution safety cap is not a substitute for the reviewed early-stop guard.

Staging resumes the same source and selection mode across midnight, retaining its original run
date and completed calls. Repeating a completed full run is a no-op. Only an explicitly requested
fresh full exercise uses `run-staging --full --new-run`; it cannot replace an unfinished full run.

### Explicit single-target staging

For an explicitly authorized fresh measurement of **one** reviewed target, use
`run-staging --target finance-agent/issue-019 --new-run` (substitute one exact catalog
`Agent/version` key). This does not run incremental selection or expand to the other 40 targets.
It executes all ten canonical attempts, not only earlier missing or unfavorable attempts.
Never repeat measurements until PASS or change the reviewed traffic, thresholds or assessor
to obtain a different outcome. The single-target result is not a full-inventory qualification.

Python freezes the target, committed source, date, native run/work identity and predecessor
reference before providers. Repeating that exact command, or `run-staging --target <same-key>`,
resumes the same measurement across midnight—even after a final FAIL or INCOMPLETE.
It does not allocate fresh traffic, replace the evidence deadline or request another judgment
for an already assessed batch. Resume requires the frozen source and target.

A subsequent separately authorized whole-target measurement requires
`run-staging --target <key> --new-run --after-run <previous-targeted-run-id>`, using the
actual native targeted run ID. Repeating this command also resumes its frozen request.
An older request cannot replace its successor. `--full`, multiple/repeated targets,
short names, wildcards and arbitrary selector syntax are rejected.

The environment ownership lock excludes active writers. A prior final INCOMPLETE judgment
with ten resolved attempts may be superseded (including unsubmitted continuations after a
proven terminal rejection, not pending/unknown outcomes); an interrupted deployment, session, invocation,
Insights operation, assessment or selected-but-unbound launch may not. Those require recovery
first, not pointer deletion or fabricated completion. Conservatively unresolved work is blocked.
The targeted control participates in staging history and per-target supersession; later
incremental/full work can supersede it, and its stale command then fails closed.
Old traffic, judgments and legacy full/incremental controls remain unchanged; only the selected
target's current index advances, retaining each other target's actual source and date.

Preserve pending deployment/session/Insights records after an interrupted request. Unknown accepted
POSTs are not safe to repeat. Native Insights submission keys and their exact request bodies are
reused when supported. A definitively rejected submission gets a fresh key and recomputed lookback;
uncertain window coverage is excluded from Engine scoring, not counted as a missed issue.
A new automatic TEST reserves its own nonzero rerun identity; an
ambiguous previous send is reconciled, never sent again blindly.

## Bounded maintenance repair loop

`maintain-test-agents` can coordinate an explicitly authorized development loop:
saved report, evidence-backed repair, reviewed candidate, scoped staging, private TEST Daily,
and analysis. It delegates measurement and delivery to the existing staging/Daily entry
points; it is not another runner, assessor, scheduler or policy authority.

### Authorization and budget

Before native work, confirm the repair scope, a finite maximum number of rounds, one literal
private TEST mailbox and the permitted staging/PR actions. For example, an approved
three-round batch permits at most three repair-and-measure rounds, not repeated runs until
the score improves. Existing explicit authorization can be reused within those bounds.
Source editing permission alone does not authorize native traffic, mail or merging.
If no applicable saved report exists, explicitly authorize one initial TEST baseline and
reserve one of those rounds for it. This does not authorize repeated unchanged baselines.

For new Agent or issue onboarding, the TEST report belongs to the contributor initiating
that onboarding. Obtain one explicit private `TO_ADDRESS` from that person; an existing
explicit authorization from the same person may be reused. Do not infer an address from
Git authorship, look up contacts, use a fixed maintainer default, or reuse another person's
recipient. Pass the address as run input rather than rewriting shared/default configuration.
The existing Daily routing binding freezes the destination before traffic. A conflicting
unfinished run must be reconciled with its original recipient, never redirected to the new
contributor. The official team route and other completed/prepared requests remain unchanged.

Keep a private maintenance log under the environment-separated runtime root. Record the
authorization, round limit, reserved round, candidate commit, planned staging scope, actual
runner IDs, exact resume commands, PR references, outcomes and remaining blockers. This is
maintenance bookkeeping, not a replacement for runner checkpoints. Copy IDs returned by
Python; never fabricate IDs or edit run/control/assessment/delivery records to advance.

Reserve a round before its first new native measurement. A revised candidate measured after
an unsuccessful round consumes another round even if the previous round stopped in staging.
Matching-source recovery, including the runner's own bounded retries, resumes the reserved
round rather than allocating new work. Preserve the log across interruptions; do not reset
the budget when opening another session or worktree.

### One repair-and-measure round

1. Prefer an applicable existing report. Check its source, actual model identities, cohort,
   exclusions and evidence visible at analysis time. Investigate a suspected problem against
   source and independent saved evidence; a card, suggested patch or score is not proof.
2. Make a concrete repair within scope, preserving the reviewed inventory, intended defect,
   requests, expectations and scoring rules. Use isolated worktrees for independent changes,
   then targeted checks and normal review. Commit the candidate before native actions and
   keep its worktree unchanged while it runs.
3. Use the existing staging commands for the explicitly authorized changed targets. Respect
   the single-target predecessor/resume contract above. Stop if the needed scope exceeds
   approval; do not silently select the full inventory. Retain other targets' actual source
   and date. Staging FAIL/INCOMPLETE is not permission to resample, and this loop adds no
   all-staging-PASS admission gate. A proven broken changed-target contract needs repair or
   an explicit blocker, not a misleading Daily measurement.
4. Run `python -m agent_insights_quality run-daily --report-mode test --to-address "<TO_ADDRESS>"`
   from the approved committed candidate, using the Daily bootstrap's source and routing
   checks. Never borrow an active official recipient for TEST. Python chooses the actual
   date/cohort and positive TEST
   identity, freezes evidence and assessments, and prepares the report. Do not retry phases
   or reassess old evidence manually. The app claims and sends only the exact prepared mail
   request, then records the real outcome; an ambiguous send stops for reconciliation.
5. Analyze the new immutable result with its coverage and limitations. Separate a confirmed
   framework/test-Agent defect from service behavior, an intended injected defect, model
   uncertainty or unavailable external evidence. A next round requires another justified,
   reviewed repair, available budget and safely resolved prior work. After any authorized
   initial baseline, an unchanged candidate, a documentation-only edit or a desire for fewer
   misses does not justify fresh traffic.

Normal source PRs are part of maintenance, not generated report publication. Creation and
merging require their existing authorization and protected review/CI rules; never change
main directly or merge an unreviewed repair because a TEST score is high. TEST measurements
remain private: no public report/trend, ADX writes, generated report PR or team email.
Keep official weekday automation measurement-only and leave its schedule unchanged.
An onboarding target may not appear in the actual date-rotated Daily plan. Keep its scoped
staging evidence and disclose the Daily coverage; do not change the date or claim that a
TEST email validates a target omitted from that measurement.

### Stop and report

Stop when no actionable unintended defect is established, the round limit is reached, or
safe recovery, delivery, credentials, an external/service owner or broader scope is needed.
Missing rows and unproven causal claims are different gaps; neither permits fabricated proof.
Changes to inventory, canonical traffic/expectations, models, scoring/coverage/thresholds,
service deployments or resource configuration/permissions require separate approval, not
automatic repair.

Report the candidate/PRs, completed rounds, counts and exclusions, delivery state and unresolved
items. Provider acceptance is not inbox proof. Preserve failed and incomplete measurements
without relabeling them. The stopping condition is no further justified repair in the approved
scope, not zero findings, all matches, full coverage or a score of 100.

## Email presentation and local review

Email shows the quality score, expected/detected/missed issue counts, Noise and Duplicate.
It has no baseline-coverage row, always-on exclusion table, score-change note or visible
Full/Partial/PASS/FAIL labels. Actual exclusions have a conditional notice linking to their
Agent's human-validation detail; all of each excluded unit's counts remain excluded.
The eligibility/scoring policies are unchanged. An invalid measurement has no quality score
and is addressed only to the personal recipient. TEST is private; measured zero is valid.

The Outlook-compatible brief presents Summary, What needs improvement, What is working, and a
five-Agent table, then optional private Quality work-item tables. Agent | Findings |
Human Validation | Assigned To are the only Agent-table columns. Assignments come from the
reviewed `catalogs/AGENT_CATALOG.yaml` owner fields, separately from the frozen measured unit
contracts. Foundry links use saved Daily Sweden environment and actual deployment object names;
missing or conflicting metadata produces an explicit missing-link notice, never an API URL or
old-region fallback. No catch-all Run notes, Other findings, methodology body or Run reference
section is inserted, including hidden private context.

The authoritative detail is `report.md`: one table per Agent with stable Agent-name anchors and
five rows for the baseline plus four issue versions. Run num, actual Agent version, Expected
insight, Generated insight(s), Assessment and short Notes keep review compact. Each row represents
ten attempts, not one call. In Agent version, reviewed issue IDs link to their `ISSUE_CATALOG.md`
anchor at the report's recorded source commit; native version numbers and `v0` remain plain text.
Without reviewed catalog context or source metadata, IDs remain plain text rather than guessing.
New/updated cards are listed with aligned Correct/Noise/Duplicate
labels; unchanged historical cards are omitted. Missed and unscored cases remain distinct.
Assessment numbers align with generated cards: "1. Matched", "2. Noise", "3. Duplicate".
"Missed" marks an undetected expected defect; "Unconfirmed" rows are excluded, not misses.
A separate "Unexpected" is a saved valid non-target finding, not expected-detection credit
or an automatic Agent-fix request. Private Agent headings link to verified Foundry objects,
and the report overview uses a short bullet list.
Routine Notes are empty. Exceptional findings expose full saved rationales under collapsed
Assessment details; disagreements show both passes without changing the resolved judgment.
Detailed evidence and complete judgments stay in the original private artifacts referenced by
the preview manifest, not repeated payload/provenance sections in the MD.
Private detail is rendered through a separate boundary; public Markdown receives only approved
aliases and reviewed catalog context, never actual private card titles or provider versions.
An `unexpected_real` result is not an automatic Agent fix task. Human validation distinguishes
an actionable defect, an ambiguous claim needing confirmation, and already-handled behavior
requiring no Agent change. This presentation never changes retained judgments or scores.

```powershell
python -m agent_insights_quality email-preview --delivery-id <existing-delivery-id>
python -m agent_insights_quality email-preview --delivery-id <existing-delivery-id> --restyle
python -m agent_insights_quality email-preview --delivery-id <existing-delivery-id> --restyle --scoring-revision <published-40-character-commit>
python -m agent_insights_quality email-preview --delivery-id <existing-delivery-id> --restyle --rescore --scoring-revision <published-current-guide-commit>
```

The first command preserves the exact prepared recipient, subject and HTML. Explicit `--restyle`
creates a clearly labelled local presentation preview from that delivery's frozen result, only
when the reviewed unit context still matches. Both export `email.html`, `email.eml`, `report.md`,
`report.html` and a provenance manifest under the private Daily `previews/` folder.
The MD is the EML's actual `text/markdown` attachment. `report.html` is a browser view derived
from that same MD, not an independent report. Restyled local browser email uses per-Agent anchors;
new prepared mail instead uses the independently published per-Agent files described below.
EML tells readers which heading to open in the attachment, without broken relative/cid/file links.
Exact export preserves the original email HTML bytes, even when the original presentation is
obsolete. A legacy request without frozen inputs gets an explicit unavailable-detail MD notice,
not invented counts. The manifest identifies authoritative/derived files, attachment names,
hashes, retained assessment references, assignment provenance and link blockers.
Neither command claims, sends, changes the original request, invokes Agent/Sol/Insights or publishes data.
EML is marked unsent, not delivered. Normal runtime ownership applies; do not bypass an active
runner's lock to export a preview.

`--restyle --rescore` explicitly derives a **LOCAL SCORING PREVIEW**, using the current
approved scoring policy and the original frozen classifications, counts and coverage.
It also exports `result.json` and records the source/derived policies, scores and result hashes.
The original result, prepared email and all invocation/assessment records remain unchanged.
This is not another measurement, claim or send. The local email links to its newly derived
detail report, not the original archive's older-policy SAS pages. It does not overwrite or
republish that archive or mint access links. Use the newly published current scoring guide
for this preview; historical ordinary restyles keep their retained guide revision.

The preview CLI returns `report_markdown_path`, `report_html_path`, `email_html_path`,
`email_eml_path`, `manifest_path` and `blockers`, alongside the content-addressed presentation ID.
New delivery preparation also durably saves private `artifacts/presentation/report.md` and its
evidence-reference checkpoint before freezing delivery inputs. Daily status exposes
`private_report_markdown_path` and `presentation_blockers`. Existing prepared requests return
unchanged. Native HTML handoff does not claim to attach a report: verified time-limited links
are inline; when unavailable, that limitation is disclosed. Local EML export supplies the attachment.

## Automatic private report publication

Daily no longer writes `reports/` in Git or prepares generated branches, PRs or merge requests.
Historical reports and their read-only CI validator remain; ordinary source, catalog and scoring
changes still require review. ADX remains a separate optional allowlisted projection for official
runs only. TEST never touches ADX, public reports/trends, generated PRs or the team mailbox.

After measurement, Python freezes the private overview Markdown from the final `QualityResult`
and `RetainedReviewContext`, five independent per-Agent Markdown documents, their derived HTML,
and a minimal manifest. Each Agent document contains only that Agent's five compact version rows,
owner, actual versions and saved classifications/Notes. It does not recompute a per-Agent score.
Any global score/counts/coverage are explicitly labelled **Overall Daily**; Agent counts are sums
of that same result's units. It binds exact UTF-8 bytes,
hashes, source revision, reviewed plan, report date, run ID, profile, assessment identity and
renderer version/hash before contacting storage. Work-item snapshots, email recipients, raw
trace/model payloads, credentials and private local paths are not manifest fields. The bundle
contains the overview `report.md`/`report.html`, `agents/{agent}/report.md`/`report.html`, and
`manifest.json`, not email, raw evidence or SAS URLs.

The destination account is the run's **saved** `Environment.storage_account_name`. The existing
`quality-artifacts` container must already exist, have no public access, and the account's
`allowBlobPublicAccess` must be explicitly false. Verification failures stop publication.
The adapter reads account metadata with normal Azure CLI credentials and uses lazy
`azure-storage-blob` imports. It never creates containers/accounts, changes roles/credentials,
requests account keys, or accesses `deployment-registries`. Python callers may inject a token
credential and matching read-only account-privacy verifier; there is no new hosted viewer or
GitHub data repository. Injected service account/endpoint scope is validated too.

Immutable keys are `reports/daily/{official|test|failure}/{run-id}/{presentation-id}/...`.
Only eligible official reports can CAS-update `reports/daily/official/latest.json`, and only
after every object and the manifest have exact content readback and a durable receipt. A newer report date
cannot be replaced by an old retry; different content for the same date is a visible conflict.
TEST and failure notices never update official latest. Existing objects are conditionally
created, never overwritten. Accepted PUTs are reconciled by reading the exact bytes, not treated
as delivery proof. A missing object may be retried only with the same frozen conditional PUT.

Private requests, local MD/HTML/manifest copies, intents and verified receipts live in
`daily/outboxes/private-reports/` under the normal runtime root. Publication does not depend on
the checkout location after preparation. A flush has a 90-second budget, 10-second per-operation
timeout input, one PUT per immutable object, and at most two latest CAS attempts. SDK retries
are disabled. Calls are synchronous under runtime ownership, after measurement: no SDK worker
threads survive cancellation/unwind. An in-flight synchronous operation drains with its configured
credential/network timeouts before ownership can exit; the deadline prevents further calls.
Publication checkpoint failure disables further publication in that worker. Measurement is kept;
an otherwise eligible inline email is still prepared. No retry triggers traffic, assessment or mail.

Daily output and `status` expose `private_report`: publication status/code, presentation ID,
request/receipt paths, local Markdown/HTML/manifest paths and truthful access metadata.
`generated_paths`, `github_request_path` and `public_report_path` are no longer Daily outputs.
The private receipt contains authenticated-storage references with `access_required: true`,
`auth_mode: entra_storage_data_plane`, and `human_validation_available: false`.

### Approved seven-day read access

The user approved **user-delegation SAS** after acknowledging that anyone holding a link can read
its one file without prior Storage RBAC. Python signs only after the immutable report receipt
exists and each Agent file has verified byte readback. It obtains a user delegation key through
the existing Entra/Azure CLI identity, not `listKeys`, an account key or a new role grant.
`GetUserDelegationKey` denial is an explicit `report_access_delegation_denied` blocker; no fallback
or self-grant is attempted. The storage container remains private.

Every SAS is scoped to one exact account/container/Agent blob: `sr=b`, `sp=r`, `spr=https`.
There is no list, write or delete permission, and no container/account SAS. The key begins at
current UTC minus five minutes for clock skew and ends exactly seven days after that start.
Thus the displayed expiry is **up to seven days**, normally six days 23 hours 55 minutes after
issuance—not seven days plus skew. The actual UTC expiry and URLs are frozen in a private,
versioned access record, bound by hash to the prepared email. Keys/token credentials themselves
are never persisted. The provider is closed before email preparation.

Each email Human Validation cell has **View report**, pointing to that Agent's own HTML blob
with its read grant, not a fragment in the overview. Markdown archives and their private access
records remain available, including through the separate access-refresh preview; email does not
duplicate them with a **Download MD** link. The email shows the exact expiry and a forwarding
warning. No SAS URL appears in another report's body, the uploaded
manifest, Git, ADX, operational events or CLI output. Status exposes only the access record/preview
paths, expiry and readiness under `private_report.access`. Bare storage references still require
authenticated storage access; the separate, approved SAS grants provide browser access directly
without an Entra-login app or a new viewer service.

When signing is unavailable, inline email remains eligible and the archived/local report remains
available. `human_validation_link_unavailable` is retained only when no usable grant is bound.
The immutable scoring-guide link and its retained receipt are unchanged.

```powershell
python -m agent_insights_quality private-report-flush --delivery-id <run-id>
python -m agent_insights_quality private-report-flush --delivery-id <run-id> --read-only
python -m agent_insights_quality private-report-refresh-access --delivery-id <run-id> --access-revision refresh-1
```

The flush commands only resume an **existing frozen publication request**, even from another source
checkout. `--read-only` performs remote reads and persists local reconciliation receipts, never
remote writes. Both use normal runtime ownership and log safe codes privately. Neither opens
qualification ports, refetches work items, renders again, prepares/claims/sends mail, or updates
old requests. Already prepared historical deliveries without a publication request are not
automatically backfilled or restyled. Their previous manual uploads/previews are not automatic
publisher receipts. A separate reviewed migration is required to enroll such a historical bundle.

`private-report-refresh-access` is an explicit, separate operation: it signs the **same already
published Agent files** into a new immutable access revision and creates a private **NOT SENT**
access preview. It neither republishes report bytes nor changes any EmailRequest, claim or send
record. Repeating the same access revision reuses its URLs/expiry, even when expired. Interrupted
signing requires a new explicit revision, never an automatic remint under the old identity.

An unclaimed email whose links have expired is rejected by `email-claim` with
`email_report_access_expired_needs_new_revision`; no claim is persisted. Refreshing access does
**not** make that original email claimable or silently replace its links. Use the explicit refresh
preview for separately authorized review; preparing a different send request is a separate reviewed
action, not part of this command. A sent email's eventual link expiry is expected, while the
underlying report stays archived. Status reads recompute expiry; exact email exports preserve
old HTML and disclose expired access as a blocker. No expiry path reinvokes Agents, reassesses,
changes recipients, automatically sends/resends mail, or enables a schedule.

Every access restore/read and new email claim also requires the matching immutable **bundle
receipt**, not merely a saved SAS record. Missing, corrupt or mismatched receipts/grants make
current access unavailable and refuse a new claim before its checkpoint. A verified bundle does
not depend on advancing the optional latest pointer: pending/conflicting latest delivery alone
does not invalidate its links.

Read-only `status` reconstructs publication/access from the frozen request, validated receipt
and grant (including the prepared email's hash-bound descriptor), never cached
`private-publication` readiness. Missing/corrupt records produce explicit unavailable/error
states, not cached `delivered` or `ready` claims. It performs no Azure, credential, rendering,
signing or writer-ownership work. `email_status` and `inbox_delivery_confirmed` describe the
stored historical send outcome separately; invalid current links never rewrite or reclassify
an accepted, delivered or unknown email record.

Summary's **How Scoring Works** row links only to a verified, immutable GitHub version of
`docs/QUALITY_BAR.md`. It never assumes `main` has the current formula. `--scoring-revision`
performs a bounded public read and requires exact equality with the reviewed local document
(apart from CRLF/LF). An unpublished, malformed or stale version is rejected, not linked.
For new preparation or restyle without an existing receipt, an operator can provide
`config/report-links.json` under the private runtime root containing
`{"scoring_revision":"<published-40-character-commit>"}`. Successful verification is frozen
with the presentation; resume uses the retained receipt, not a mutable branch.
If the local guide has since changed, a historical restyle can verify the retained immutable
revision against its saved content hash; it never substitutes the new policy's guide.
Without a correct published version, `scoring_link_publication_required` is an explicit
publication blocker and the row says the link is pending. This does not block eligible inline
email, authorize publication, change credentials, or expose TEST content publicly. Historical
report files are not proof that a detailed report URL is delivery-available.

New work-item tables include Type and use a frozen provider-as-of snapshot. Closed items cover
the interval since the previous successfully submitted eligible official report's snapshot;
TEST, failure notices, unsent requests and ambiguous sends do not advance that boundary. With
no prior boundary, an explicitly labelled initial seven-day view is used. The configured Quality
query still defines scope. An unavailable optional query is not an empty result and never blocks
an otherwise eligible email. Legacy local previews retain their original recorded day and display
unrecorded Type/cutoff fields honestly; they do not refetch or invent a newer reporting window.

## Provider and checkpoint recovery

### Deployment content identity and Git provenance

New deployments carry a separate `content_hash` (`v1:sha256:…`) in memory and
source-bound work checkpoints, `details.content_hash` in the canonical private
registry, and `aiq_content_hash` in native metadata. `source_revision`
and `aiq_source_revision` remain actual Git provenance: the last commit touching
the selected deployment input paths, **not** a fingerprint. A reused version keeps
its original deployment provenance; the run separately retains its own source
revision. Squashing identical input trees therefore does not create another Agent
version merely because the last-touch commit changed.

The hash covers the exact resolved Prompt definition or Hosted definition, the
deterministic Hosted code ZIP (selected version-owned source, baseline requirements
and `host.yaml`), or the selected container build context (source, requirements and
Dockerfile) **and resolved digest-pinned image reference**. Effective Hosted model,
environment overrides, runtime/entrypoint, protocol, CPU/memory and deployment API
settings participate. Container platform-reserved environment overrides are omitted
exactly as on the wire; Prompt ignores Hosted configuration. JSON object ordering is
normalized, while deployed file names and bytes are retained. Target, logical version,
Agent type, runtime name, profile and project endpoint bind the identity: an identical
asset is never permission to reuse another Agent/profile's version.

Traffic, expectations, assessment/scoring/reporting changes, Git history and generator
source text do not themselves enter the hash; a generator's changed **deployment
output** does. Staging source selection remains separate and may select new traffic
without requiring another native version. Container preparation still resolves the
existing exact content-addressed ACR tag, without rebuilding an available image merely
for another commit. The image reference captures registry/repository/digest changes.
These hashes describe declared inputs, not reproducible execution: mutable model
aliases, platform-injected values, remote dependency resolution and base-image tags
can drift independently. They are not discovered or changed by this identity check.

Migration is conservative. An active legacy registry entry or native version without
a verifiable content hash is not backfilled, relabelled or adopted by commit equality.
New work creates a content-keyed version once if no exact content-keyed native match
exists. The registry's current pointer advances normally; existing native versions,
other registry entries, frozen run records and historical results are retained.
This can cost one migration deployment; it avoids claiming evidence of inputs that
old metadata never recorded. Do not bulk rewrite historical registries/results.

A saved work deployment is different from a registry candidate. Resume uses its
original source/provider/content identity without repackaging or reading current
source/configuration. Completed traffic/Insights are not repeated. Unknown creates
reconcile only the exact submission metadata, including original provenance; multiple
matches remain ambiguous, and no match never authorizes a blind retry. A missing
frozen active version is an error, not permission to replace it. Known rejected new
submissions may retry only after verifying identical content; a rejected legacy
submission requires explicitly selected new work rather than inventing a hash.
Frozen discovery checks all same-content/binding candidates before provenance:
another source's ownership blocks even a definitively rejected retry, without adopting
that version or submitting another. Fresh non-resume reuse still retains its owner's provenance.
The saved input hash is checked before a rejected container submission can build
again; its final digest-pinned artifact must still match before an Agent POST.
Unresolved registry records block new work until their owning run is reconciled.

Fresh reuse requires complete matching native content/binding/provenance metadata.
A create's own response can omit metadata because its saved request already binds
the returned identity; subsequent discovery/readback must verify it. Local fake-wire
tests do not establish native metadata retention/readback, ACR availability or
end-to-end version reuse. Those need separately authorized native acceptance; this
change authorizes no deployment, traffic, reassessment, rescoring or scheduling.

### Registry compatibility and source-bound cutover

The canonical v1 registry keeps its original six deployment fields: `target_key`,
`agent_name`, `provider_version`, `agent_type`, `source_revision` and `details`.
Older source readers construct `Deployment(**record)` and reject a new top-level
`content_hash`; they can read and preserve the extensible `details` projection.
The new reader validates the nested hash against recorded native metadata and normalizes it
to the in-memory field. It also accepts the earlier draft top-level representation,
but rejects conflicting dual declarations. Loading never rewrites the canonical blob;
only ordinary validated, conditional saves emit the compatible representation.
Do not bulk backfill legacy hashes or rewrite frozen work checkpoints.

This is **registry read compatibility**, not permission to interchange runner versions.
New work checkpoints still require their owning source, and old code still uses Git-based
deployment selection without the content/provenance conflict guard. Older writers may
advance a target's current pointer to a hashless version; retained native versions and
frozen work records remain the recovery authority. Do not resume a new run under old code,
restore a stale whole-registry backup, or claim that an old/new writer mixture is safe.

Review cutover separately for each profile, under its normal ownership lock. Staging and
Daily use different canonical registry blobs and runtime roots. Inspect the actual active
controls, frozen source and unresolved remote outcomes before authorizing new work; finish
or reconcile an old run with its owning source rather than changing its lineage or clearing
its lock. Preserve private canonical snapshots/ETags and every existing native version.
If a draft top-level record is found remotely, stop for an explicitly reviewed format
migration; local acceptance of that format is not proof that older readers can recover it.
The [Daily two-slot call journal](#bounded-daily-judgment-correction) and
[targeted-staging source/predecessor checks](#explicit-single-target-staging) remain unchanged.
None of these rules authorizes native acceptance, a deployment or a schedule.

New Insights polling has a separate `insights_poll_timeout_seconds` budget (default 1200).
Deployment polling retains `poll_timeout_seconds` (default 600); trace hydration is separate.
A local wait timeout is not a native failed run. Resume queries the original accepted operation
before deciding its outcome, without rewriting its existing deadline or creating another analysis.

Live evidence discovery and hydration send `Cache-Control: no-cache`. Azure Monitor's
[default two-minute response cache](https://learn.microsoft.com/azure/azure-monitor/logs/api/cache)
can otherwise keep returning the initial incomplete view throughout a bounded hydration window.
Bypassing that cache does not alter query scope, extend deadlines, lower readiness requirements
or guarantee telemetry export. Later observations remain later evidence, never retroactive proof.

Local `runner.log` and `events.jsonl` preserve starts, heartbeats, retries and outcomes across
restart. They work without ADX. Raw evidence is kept separately. Status reads do not take the
writer lock or perform live work.

New Daily runs may select an assessor through private `config/daily-assessment.json`, with exactly
`deployment_name`, `model`, `model_version` and `credential` (`azure_cli`). Staging continues to
use private `config/assessment.json` or the Sol defaults. Existing runs resume their frozen
assessment settings, regardless of changes to those files. A new run with a different assessor
reuses matching execution evidence where applicable, but creates new judgments and records the
configured model identity; it never relabels earlier judgments from another model.

Daily assessment has a bounded `daily_assessment_max_payload_bytes` setting (default 4,000,000;
maximum 8,000,000). Oversized raw inputs use the existing lossless reference encoding; no evidence
is truncated to fit. A package that still exceeds the configured budget remains unscorable.
This byte budget does not establish the deployed model's token/context limit: native rejections
and missing evidence remain explicit assessment failures, not successful measurements.

### Bounded Daily judgment correction

The first request states the same root-group/match invariant as the local validator.
Correct cards for one independently evidenced expected root all use `expected_match=true`;
the aggregator, not a model's choice of a primary card, assigns one detection and extra
Duplicates. First-call schema descriptions preserve this distinction before review is needed.
The input also explains retained, exactly owned logs with missing parents. That diagnostic
alone is not a unit-wide acquisition failure; an essential limitation still needs its affected
claim and missing proof explained. Current-core uncertainty and real evidence gaps remain
unscorable under the unchanged policy.

New Daily runs freeze `daily-assessment-two-slots-v1`: one initial judgment plus either
one focused review or one root-consistency correction, never both. Existing provider
HTTP retry/cooldown budgets are separate and unchanged. Only a schema-valid, fully covered
initial response whose attempts, cards and citations validate before a local
`assessment_root_conflict` can use correction. Other failures do not become generic retries.

Correction uses the same frozen evidence, assessor and schema, with the invalid response
and validation feedback treated as data. The invalid initial remains saved, is not a vote
and earns no credit. Root correction cannot clear unrelated initial review requirements.
The corrected response must fully validate. Any remaining review
candidate, including Unknown, keeps the unit unscorable when the second slot is exhausted;
an oversized correction also cannot earn partial credit or authorize a third call.

Requests bind source, work, input, schema and prompt/assessor identity. Outputs are durable
before phase completion, so completed calls resume without repetition. Missing output after
submission, cancellation, recorded provider failures or a changed binding fail closed.
A terminal correction cannot refresh its input to buy another call in the same run.
Legacy completed results restore unchanged; unfinished legacy assessments without durable
call state stop with `assessment_legacy_call_state_unavailable`, not inferred cache migration
or an in-place rewrite of an old report. Staging behavior is unchanged; shared-file changes
may still select retained staging evidence for reassessment under existing dependency rules.

## Interactive long-running work

During an interactive rollout, a nested app session can own the entire staging or private Daily
runner command while its parent monitors progress and coordinates fixes. Python still owns all
internal orchestration; do not create per-Agent orchestration sessions.

Monitor `QualityOperationsV1` by the exact `FrameworkRunId`, using local checkpoints as the
authoritative outcome and recovery state. An idle app session or an old heartbeat is not proof
that its runner completed. After an app restart, confirm whether the local process survived
before resuming the same command under normal runtime ownership; never bypass a lock or start
a second writer. Nested sessions do not guarantee process survival across app restarts.

Independent repair work can proceed in separate worktrees without modifying the running candidate.
Integrate repairs after the active run ends, then let incremental selection choose affected units.

## Private performance observations

### Caller invocation context

`invocation_trace_context` is a strict integer setting, 0 or 1, default **1 for new
staging and Daily runs**. It adds a W3C `traceparent` only to top-level Agent invocation
POSTs made through the direct REST adapter, not session control, Insights, Sol or
administrative requests. Setup and probe turns receive independent contexts while
their Prompt response continuation or Hosted native session remains unchanged.
Subagent calls inside deployed workflows retain normal root-context inheritance.

The frozen algorithm `aiq-w3c-sha256-v1` derives trace and parent IDs separately from
the immutable client request ID using SHA-256, with domain
`agent-insights-quality/invocation-context/v1`, purpose labels `trace` and `parent`,
and NUL separators. The first 32/16 lowercase hex characters are used; an all-zero
identifier becomes one. Version is `00`, flags `01` (a sampling hint, not a guarantee).
No client span is exported and no stored operation ID is assigned or rewritten.
Known-rejected retries retain the same request ID/context; unknown submissions are
not repeated. Each turn's small immutable `outbound-context` checkpoint records only
the algorithm, expected header, request/source identity and pre-invoke provenance.
It is **not proof that a request reached the network**; no full headers or credentials
are captured. Failure to persist the plan or invocation stops the POST.

The policy is frozen in `completed/invocation-trace-context.json` before invocation.
Legacy runs without it remain off, resumed runs restore their frozen policy, and
inherited traffic uses its owning run's policy. Existing completed receipts are not
retroactively given headers. The shared context driver (`invocation_context.py`) and
invocation wire adapters (`providers/runtime.py`, `providers/transport.py`) are traffic
inputs: their changes select fresh staging traffic for affected reviewed targets,
including Prompt Agents, rather than reassessing old traffic. Report/presentation,
read-only audit and assessor-only edits are not new traffic dependencies. This does
not itself change Agent deployment inputs or require a manual `--full` override.

Explicit Agent context runs in an isolated Python context with optional OpenTelemetry
HTTP instrumentation suppressed. Conflicting case-insensitive trace headers and
attempted context-header replacement fail closed; ambient baggage/tracestate is not
forwarded. This controls the caller, not a platform's later parent selection.

After authorized staging, use its resolved run ID for the offline, read-only audit:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m agent_insights_quality.trace_audit --profile staging --run-id $runId
```

It reads retained invocation records and attributable Snapshot roots, preferring the
immutable pre-Insights snapshot where present, and emits aliases rather than raw IDs.
`different_context_or_unsupported`, missing roots or shared observed contexts require
inspection before proceeding to a full Daily measurement. A legitimate platform
restart can change context, so the comparison never changes readiness or exclusions.
Caller uniqueness is not proof of end-to-end propagation. Keep the platform outcome
unknown until the fresh staging evidence supports acceptance; do not silently claim
the platform honored the header. Ten attempts, six Daily readiness, eight-observation
staging expected-observation requirements and scoring are unchanged. The lookahead settings
have default zero; a separately reviewed configuration may opt into the staging trial.

Daily keeps five Agent lanes and sequential versions within each lane. Independent attempts
use up to `daily_attempt_workers` (default 4), under the shared `daily_attempt_budget` (default 10).
Each attempt's setup and verification turns remain ordered. Travel business attempts remain
serial because its graph-wide booking ledger is shared; staging business calls remain serial
per target even when next-session preparation is explicitly enabled.
Completed immutable evidence/card snapshots enter the four-worker assessment pipeline immediately,
while other safe lane work continues. Final aggregation waits for both execution and assessment.

`daily_travel_session_lookahead` is an opt-in integer setting: **0 (default, off)** or **1**.
It is not enabled by the four-attempt setting. For a new Daily run, 1 permits preparation of
only the next native session within the already activated Travel version while the current
attempt executes. Travel business requests remain strictly serial; no booking fixture, Agent
source, request body, attempt count or staging behavior changes. Retained traffic owned by
another run keeps the existing serial path. The option is frozen per run; resume restores it
even if configuration defaults change, and legacy runs remain off.

The adapter sends only a version-bound session-creation request, not an Agent invocation.
Existing fake-wire and local Hosted response tests do **not** establish that the platform's
session creation cannot restart or alter another active session's host/ledger. Keep this option
off until that contract is established by controlled acceptance. No additional traffic, test
run or warmup is implied by adding the option.

Current and prepared attempts both hold the global attempt permit through completion.
At most one session is ahead; budget 1 degenerates to serial preparation without deadlock.
An uncertain business response stops further Travel business calls in that version, while
already submitted preparation is drained and checkpointed. Unknown session POSTs are never
repeated blindly. Fatal checkpoint failures cancel and drain both workers before ownership
is released, including already-started threaded HTTP sends through repeated cancellation.
An unpersisted session response remains unresolved rather than authorizing a retry.
Private `attempt_phase` measurements separate `session_preparation` and
`business_execution`; `wait:prepared_session` records waiting for the preceding attempt.
Whole-attempt time includes those intervals and is not pure service time or additive wall time.

After six attributable verification attempts, Daily can continue batched evidence collection for
`daily_evidence_grace_seconds` (default 30), within the existing hydration deadline, to allow late
invocation anchors to arrive. It stops earlier when all completed verification responses have
anchors. Expiry does not raise readiness to ten or require complete child trees: six remains the
minimum, and any essential gaps are disclosed. Grace deadlines survive restart.

Live commands return `performance_path` when the private performance artifact was saved.
It identifies one process segment under the run's `artifacts/performance/` directory.
Resuming creates a separate segment instead of rewriting earlier measurements; bounded batches
and progress records retain observations during a long run. An unfinished segment is not proof
of completed execution, and an uncheckpointed tail may be unavailable after abrupt termination.

Use stage and lane timings to locate the critical path, queue timings for existing concurrency
limits, adapter/HTTP timings for awaited calls, and hydration/poll/backoff timings for waits.
These intervals nest and overlap: their sums are not the run's wall time or pure service latency.
The segment begins after source and plan selection; earlier startup remains in command-status logs.
Reused or skipped work has a null duration, not an instantaneous fresh execution. Active peaks
describe the existing concurrency, not a changed limit.

Sol token totals use actual returned usage only, with known-response counts and null values when
usage is unavailable. Payload bytes are not a token estimate. Observations contain no raw prompts,
responses, credentials or provider identifiers and are not sent to ADX, including in TEST mode.
Measurement-persistence failures surface as warnings without rerunning qualification; primary
side-effect checkpoint failures remain fatal. Performance data never affects quality scores.

## Environment boundaries

Staging uses `aiq-staging-swedencentral`; Daily uses `aiq-daily-swedencentral`. Both reuse Agent
objects and separate g30 telemetry. The canonical deployment registry is in the dedicated
Sweden private `deployment-registries` blob container. There is no legacy-region/storage fallback.

Terra capacity is profile-specific in `infra/main.bicep`: `dailyInsightGenerationCapacity`
defaults to 1000 (1,000,000 TPM), while `stagingInsightGenerationCapacity` defaults to 100
(100,000 TPM). Both retain the existing `DataZoneStandard` model deployment and pinned version.
The former shared `insightGenerationCapacity` parameter is replaced by these two parameters;
update private deployment parameter files explicitly rather than applying one value to both
profiles. These are configured rate limits, not guaranteed achieved throughput. Changing or
compiling the template does not deploy it; infrastructure updates remain separately scoped.

Staging never runs Agent Insights, scores Daily cards or sends a report email. It may emit safe
operational events. A TEST Daily still performs qualification, but keeps evidence, previews,
logs and approved report archives private; its email is only for the configured private
recipient. It creates no public report, ADX writes, generated PR or official latest update.

When blocked by access, a provider capability or an ambiguous outcome, keep checkpoints and surface
the specific decision needed. Continue unrelated safe work rather than hiding the blocker or
lowering the evidence bar.
