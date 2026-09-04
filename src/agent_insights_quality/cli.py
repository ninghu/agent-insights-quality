from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

from agent_insights_quality.adx import (
    publish_daily_report,
    publish_daily_report_best_effort,
    render_dashboard,
    resolve_dashboard_link,
)
from agent_insights_quality.assessment import (
    load_assessments,
    load_baseline_assessments,
    rehydrate_packages,
)
from agent_insights_quality.automation_policy import load_automation_policy
from agent_insights_quality.azure import (
    deploy_analytics_infrastructure,
    deploy_infrastructure,
)
from agent_insights_quality.catalogs import (
    catalog_hashes,
    catalog_summary,
    generate_docs,
    load_catalogs,
    agent_model_contract,
)
from agent_insights_quality.daily_coordinator import (
    assert_daily_finalization_inputs,
    assert_daily_receipt_import,
    claim_daily_email,
    complete_daily_publication,
    compose_daily,
    daily_guide,
    daily_status,
    fail_daily,
    prepare_daily,
    provision_daily,
    record_daily_email_receipt,
    record_daily_finalization,
    record_daily_improvement_input,
    run_daily_agent,
    validate_daily_assessment_outputs,
)
from agent_insights_quality.email import (
    build_runtime_links,
    create_request,
    import_receipt,
    resolve_recipient,
    write_private_report_preview,
)
from agent_insights_quality.generated_paths import validate_generated_paths
from agent_insights_quality.github_preview import (
    bind_preview_publication,
    preview_links,
    publish_daily_email_test_preview,
    validate_preview_publication,
    verify_daily_email_test_preview,
)
from agent_insights_quality.live import LiveRuntime
from agent_insights_quality.models import SKIPPED_VERSION_STATUSES
from agent_insights_quality.improvement_memory import (
    build_normalized_summary,
    validate_analysis_against_summary,
    validate_published_improvement,
    write_improvement_memory,
    write_improvement_preview,
)
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.provisioning import (
    create_promotion_receipt,
    provision_profile,
)
from agent_insights_quality.registry import load_registry, sync_registry
from agent_insights_quality.reporting import (
    apply_score_comparison,
    apply_staging_score_comparison,
    build_report,
    score_comparison,
    update_trend,
    updated_trend,
    write_report,
    validate_published_report,
    render_markdown,
    render_agent_markdown,
)
from agent_insights_quality.run_manifest import (
    OFFICIAL_DELIVERY,
    TEST_EMAIL_ONLY_DELIVERY,
    build_manifest,
    run_id,
    validate_manifest,
)
from agent_insights_quality.runner import execute
from agent_insights_quality.runtime_state import (
    ActiveQualificationError,
    VersionCheckpointStore,
    profile_run_lock,
)
from agent_insights_quality.selection import select_daily, select_full
from agent_insights_quality.telemetry_cleanup import (
    apply_cleanup_plan,
    write_cleanup_plan,
)
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    atomic_text,
    content_hash,
    file_hash,
    immutable_json,
    read_json,
    runtime_root,
)
from agent_insights_quality.validation import validate_repository
from agent_insights_quality.validation_coordinator import (
    compose_test_agent_validation,
    deploy_test_agent_validation_shard,
    import_test_agent_validation_assessment,
    invoke_test_agent_validation_shard,
    prepare_test_agent_validation,
    prepare_test_agent_validation_assessment,
    reconcile_test_agent_validation_deployment,
    recover_test_agent_validation,
    release_test_agent_validation_assessment,
    run_test_agent_validation,
)
from agent_insights_quality.validation_rules import validation_matrix
from agent_insights_quality.work_items import (
    fetch_quality_work_items,
    load_quality_work_items,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiq-quality")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    docs = commands.add_parser("generate-docs")
    docs.add_argument("--check", action="store_true")
    select = commands.add_parser("select")
    select.add_argument("--report-date", required=True, type=date.fromisoformat)
    select.add_argument("--full", action="store_true")
    commands.add_parser("deploy-infrastructure")
    commands.add_parser("deploy-analytics")
    publish_adx = commands.add_parser("publish-adx")
    publish_adx.add_argument("--report", type=Path, action="append", required=True)
    dashboard = commands.add_parser("render-adx-dashboard")
    dashboard.add_argument("--output", type=Path)
    provision = commands.add_parser("provision")
    provision.add_argument("--profile", choices=("daily", "staging"), required=True)
    for name in ("run-full",):
        run = commands.add_parser(name)
        run.add_argument("--report-date", required=True, type=date.fromisoformat)
        run.add_argument("--rerun", type=int, default=0)
        run.add_argument("--state-root", type=Path, default=runtime_root())
        run.add_argument("--work-items", type=Path, required=True)
    daily_prepare = commands.add_parser(
        "daily-prepare",
        help="Start Daily after a human chooses to run it; staging is advisory.",
        description=(
            "Start a Daily lifecycle from the exact clean checkout and private "
            "work-item snapshot. Test Agent Validation is advisory and is not "
            "an admission input."
        ),
    )
    daily_prepare.add_argument("--report-date", required=True, type=date.fromisoformat)
    daily_prepare.add_argument("--rerun", type=int, default=0)
    daily_prepare.add_argument("--work-items", type=Path, required=True)
    daily_prepare.add_argument("--test-run", action="store_true")
    daily_prepare.add_argument("--publish-preview", action="store_true")
    commands.add_parser("daily-provision")
    daily_agent = commands.add_parser("daily-run-agent")
    daily_agent.add_argument(
        "--agent",
        choices=(
            "weather-agent",
            "healthcare-agent",
            "finance-agent",
            "travel-agent",
            "support-ticket-agent",
        ),
        required=True,
    )
    commands.add_parser("daily-compose")
    commands.add_parser("daily-status")
    commands.add_parser("daily-guide")
    daily_assessments = commands.add_parser("daily-validate-assessments")
    daily_assessments.add_argument(
        "--assessment",
        type=Path,
        action="append",
        required=True,
    )
    daily_assessments.add_argument(
        "--baseline-assessment",
        type=Path,
        action="append",
        required=True,
    )
    daily_assessments.add_argument(
        "--recheck-assessment",
        type=Path,
        action="append",
        default=[],
    )
    daily_assessments.add_argument(
        "--recheck-baseline-assessment",
        type=Path,
        action="append",
        default=[],
    )
    commands.add_parser("daily-email-claim")
    daily_fail = commands.add_parser("daily-fail")
    daily_fail.add_argument("--reason-code", required=True)
    daily_fail.add_argument("--confirm", action="store_true")
    daily_publication = commands.add_parser("daily-complete-publication")
    daily_publication.add_argument("--pr-number", type=int, required=True)
    daily_publication.add_argument("--path", action="append", required=True)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--manifest", type=Path, required=True)
    finalize.add_argument("--assessment", type=Path, action="append", required=True)
    finalize.add_argument(
        "--baseline-assessment",
        type=Path,
        action="append",
        required=True,
    )
    finalize.add_argument("--output-root", type=Path, default=ROOT / "reports")
    finalize.add_argument("--work-items", type=Path, required=True)
    finalize.add_argument("--improvement-analysis", type=Path)
    finalize.add_argument(
        "--prepare-improvement-input",
        action="store_true",
    )
    work_items = commands.add_parser("fetch-quality-work-items")
    work_items.add_argument("--query-url", required=True)
    work_items.add_argument("--report-date", required=True, type=date.fromisoformat)
    work_items.add_argument("--output", type=Path, required=True)
    receipt = commands.add_parser("email-receipt-import")
    receipt.add_argument("--request", type=Path, required=True)
    receipt.add_argument("--receipt", type=Path, required=True)
    receipt.add_argument("--output", type=Path, required=True)
    replay = commands.add_parser("replay-run")
    replay.add_argument("--manifest", type=Path, required=True)
    paths = commands.add_parser("validate-generated-paths")
    paths.add_argument("--path", action="append", default=[])
    published = commands.add_parser("validate-published-report")
    published.add_argument("--report", type=Path, required=True)
    published.add_argument("--report-relative-path", required=True)
    published.add_argument("--report-markdown", type=Path, required=True)
    published.add_argument("--latest-json", type=Path, required=True)
    published.add_argument("--latest-markdown", type=Path, required=True)
    published.add_argument("--trend", type=Path, required=True)
    published.add_argument("--base-trend", type=Path, required=True)
    published.add_argument("--improvement-json", type=Path, required=True)
    published.add_argument("--improvement-markdown", type=Path, required=True)
    published.add_argument(
        "--base-improvement-json",
        type=Path,
        required=True,
    )
    published.add_argument(
        "--base-improvement-markdown",
        type=Path,
        required=True,
    )
    published.add_argument(
        "--improvement-snapshot-json",
        type=Path,
        required=True,
    )
    published.add_argument(
        "--improvement-snapshot-markdown",
        type=Path,
        required=True,
    )
    published.add_argument(
        "--agent-report",
        type=Path,
        action="append",
        required=True,
    )
    promotion = commands.add_parser("create-promotion-receipt")
    promotion.add_argument("--report", type=Path, required=True)
    promotion.add_argument("--registry", type=Path, required=True)
    promotion.add_argument("--manifest", type=Path, required=True)
    promotion.add_argument("--output", type=Path, required=True)
    promotion.add_argument("--human-reviewed", action="store_true")
    cleanup = commands.add_parser("cleanup-telemetry")
    cleanup.add_argument("--plan", type=Path, required=True)
    cleanup.add_argument("--receipt", type=Path)
    cleanup.add_argument("--human-reviewed", action="store_true")
    commands.add_parser("run-test-agent-validation")
    commands.add_parser("prepare-test-agent-validation")
    commands.add_parser("recover-test-agent-validation")
    deploy_validation = commands.add_parser(
        "deploy-test-agent-validation-shard"
    )
    deploy_validation.add_argument("--shard-id", type=int, required=True)
    commands.add_parser("reconcile-test-agent-validation-deployment")
    invoke_validation = commands.add_parser(
        "invoke-test-agent-validation-shard"
    )
    invoke_validation.add_argument("--shard-id", type=int, required=True)
    commands.add_parser("prepare-test-agent-validation-assessment")
    commands.add_parser("release-test-agent-validation-assessment")
    commands.add_parser("import-test-agent-validation-assessment")
    commands.add_parser("compose-test-agent-validation")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = _dispatch(args)
    except ContractError as error:
        parser.exit(1, f"error: {error}\n")
    if result is not None:
        print(result)


def _dispatch(args: argparse.Namespace) -> str | None:
    if args.command == "run-test-agent-validation":
        return json.dumps(run_test_agent_validation(), sort_keys=True)
    if args.command == "prepare-test-agent-validation":
        return json.dumps(prepare_test_agent_validation(), sort_keys=True)
    if args.command == "recover-test-agent-validation":
        return json.dumps(recover_test_agent_validation(), sort_keys=True)
    if args.command == "deploy-test-agent-validation-shard":
        return json.dumps(
            deploy_test_agent_validation_shard(shard_id=args.shard_id),
            sort_keys=True,
        )
    if args.command == "reconcile-test-agent-validation-deployment":
        return json.dumps(
            reconcile_test_agent_validation_deployment(),
            sort_keys=True,
        )
    if args.command == "invoke-test-agent-validation-shard":
        return json.dumps(
            invoke_test_agent_validation_shard(shard_id=args.shard_id),
            sort_keys=True,
        )
    if args.command == "prepare-test-agent-validation-assessment":
        return json.dumps(
            prepare_test_agent_validation_assessment(),
            sort_keys=True,
        )
    if args.command == "release-test-agent-validation-assessment":
        return json.dumps(
            release_test_agent_validation_assessment(),
            sort_keys=True,
        )
    if args.command == "import-test-agent-validation-assessment":
        return json.dumps(
            import_test_agent_validation_assessment(),
            sort_keys=True,
        )
    if args.command == "compose-test-agent-validation":
        return json.dumps(compose_test_agent_validation(), sort_keys=True)
    if args.command == "daily-prepare":
        return json.dumps(
            prepare_daily(
                report_date=args.report_date,
                work_items_path=args.work_items,
                rerun=args.rerun,
                test_run=args.test_run,
                publish_preview=args.publish_preview,
            ),
            sort_keys=True,
        )
    if args.command == "daily-provision":
        return json.dumps(provision_daily(), sort_keys=True)
    if args.command == "daily-run-agent":
        return json.dumps(run_daily_agent(args.agent), sort_keys=True)
    if args.command == "daily-compose":
        return json.dumps(compose_daily(), sort_keys=True)
    if args.command == "daily-status":
        return json.dumps(daily_status(), sort_keys=True)
    if args.command == "daily-guide":
        return json.dumps(daily_guide(), sort_keys=True)
    if args.command == "daily-validate-assessments":
        return json.dumps(
            validate_daily_assessment_outputs(
                assessments=args.assessment,
                baseline_assessments=args.baseline_assessment,
                recheck_assessments=args.recheck_assessment,
                recheck_baseline_assessments=args.recheck_baseline_assessment,
            ),
            sort_keys=True,
        )
    if args.command == "daily-email-claim":
        return json.dumps(claim_daily_email(), sort_keys=True)
    if args.command == "daily-fail":
        return json.dumps(
            fail_daily(
                reason_code=args.reason_code,
                confirmed=args.confirm,
            ),
            sort_keys=True,
        )
    if args.command == "daily-complete-publication":
        return json.dumps(
            complete_daily_publication(
                pr_number=args.pr_number,
                generated_paths=args.path,
            ),
            sort_keys=True,
        )
    if args.command == "validate":
        validate_repository()
        return catalog_summary()
    if args.command == "generate-docs":
        generate_docs(check=args.check)
        return None
    if args.command == "fetch-quality-work-items":
        count = fetch_quality_work_items(
            args.query_url,
            args.report_date,
            args.output,
        )
        return json.dumps(
            {"quality_work_items": count, "output": str(args.output)},
            sort_keys=True,
        )
    if args.command == "cleanup-telemetry":
        if args.human_reviewed:
            if args.receipt is None:
                raise ContractError("Reviewed telemetry cleanup requires a receipt path")
            receipt = apply_cleanup_plan(args.plan, args.receipt)
            return json.dumps(
                {
                    "deleted_resource_count": receipt["deleted_resource_count"],
                    "remaining_owned_resource_count": receipt[
                        "remaining_owned_resource_count"
                    ],
                },
                sort_keys=True,
            )
        if args.receipt is not None:
            raise ContractError("Telemetry cleanup planning does not accept a receipt")
        plan = write_cleanup_plan(args.plan)
        return json.dumps(
            {"planned_resource_count": len(plan["resources"])},
            sort_keys=True,
        )
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    if args.command == "select":
        selected = (
            select_full(agents)
            if args.full
            else select_daily(args.report_date, agents, issues, hashes["issues"])
        )
        return json.dumps(selected, indent=2, sort_keys=True)
    if args.command == "deploy-infrastructure":
        deploy_infrastructure()
        return "Infrastructure deployment completed."
    if args.command == "deploy-analytics":
        deploy_analytics_infrastructure()
        return "Analytics infrastructure deployment completed."
    if args.command == "publish-adx":
        receipts = [
            publish_daily_report(
                read_json(path),
                source_path=path,
                catalogs=(agents, issues),
            )
            for path in args.report
        ]
        return json.dumps({"publications": receipts}, sort_keys=True)
    if args.command == "render-adx-dashboard":
        return str(render_dashboard(args.output))
    if args.command == "provision":
        if args.profile == "daily":
            raise ContractError(
                "Daily provisioning requires daily-prepare then daily-provision "
                "under the lifecycle quiescence lock"
            )
        profile = RuntimeProfile.from_env(args.profile)
        approved_digests = None
        provision_profile(
            profile=profile,
            agents=agents,
            issues=issues,
            approved_digests=approved_digests,
        )
        return f"{args.profile} profile provisioned."
    if args.command == "run-full":
        policy = load_automation_policy()
        profile_name = "staging"
        test_run = bool(getattr(args, "test_run", False))
        if test_run and args.rerun <= 0:
            raise ContractError("Test runs require a nonzero --rerun identity")
        delivery_mode = (
            TEST_EMAIL_ONLY_DELIVERY if test_run else OFFICIAL_DELIVERY
        )
        work_items = load_quality_work_items(
            args.work_items,
            report_date=args.report_date,
        )
        selected = (
            select_daily(args.report_date, agents, issues, hashes["issues"])
            if profile_name == "daily"
            else select_full(agents)
        )
        state = args.state_root / profile_name / run_id(args.report_date, args.rerun)
        immutable_json(
            state / "work-items-reference.json",
            {
                "schema_version": "1.0.0",
                "run_id": run_id(args.report_date, args.rerun),
                "report_date": args.report_date.isoformat(),
                "content_digest": content_hash(work_items),
            },
        )
        profile = RuntimeProfile.from_env(profile_name)
        profile.assert_insights_connection()
        profile.assert_test_agent_model(agent_model_contract(agents))
        test_region = profile.resolve_test_region()
        sync_registry(profile)
        registry = load_registry(
            profile.registry_path,
            profile=profile_name,
            catalog_hashes=hashes,
        )
        if registry["test_region"] != test_region:
            raise ContractError(
                "Live Foundry Project region does not match the deployment registry"
            )
        runtime = LiveRuntime(profile)
        seed = int(hashes["issues"].split(":")[1][:16], 16)
        run_contract_digest = _run_contract_digest(
            profile_name=profile_name,
            report_date=args.report_date.isoformat(),
            rerun=args.rerun,
            delivery_mode=delivery_mode,
            catalog_hashes=hashes,
            selected=selected,
            registry=registry,
            work_items=work_items,
            policy=policy,
            seed=seed,
            test_region=test_region,
            test_region_registry=registry["test_region"],
        )
        checkpoint_store = VersionCheckpointStore(
            state / "stage-checkpoints",
            run_contract_digest,
        )
        try:
            with profile_run_lock(
                profile_name,
                run_id(args.report_date, args.rerun),
            ):
                results = execute(
                    agents=agents,
                    issues=issues,
                    selected=selected,
                    registry=registry,
                    runtime=runtime,
                    seed=seed,
                    lookback_hours=policy.insight_lookback_hours,
                    clean_window_poll_seconds=policy.clean_window_poll_seconds,
                    clean_window_ingestion_margin_seconds=(
                        policy.clean_window_ingestion_margin_seconds
                    ),
                    clean_window_max_wait_seconds=(
                        policy.clean_window_max_wait_seconds
                    ),
                    trace_assertion_stabilization_seconds=(
                        policy.trace_assertion_stabilization_seconds
                    ),
                    insight_start_margin_seconds=policy.insight_start_margin_seconds,
                    max_recovery_versions=policy.max_recovery_versions,
                    agent_start_stagger_seconds=(
                        policy.agent_start_stagger_seconds
                    ),
                    checkpoint_store=checkpoint_store,
                )
                _assert_insight_state_resolved(checkpoint_store)
                manifest = build_manifest(
                    report_date=args.report_date,
                    profile=profile_name,
                    rerun=args.rerun,
                    delivery_mode=delivery_mode,
                    insight_lookback_hours=policy.insight_lookback_hours,
                    telemetry_resource_set=policy.telemetry_resource_set,
                    test_region=test_region,
                    test_region_registry=registry["test_region"],
                    catalog_hashes=hashes,
                    agent_catalog=agents,
                    issue_catalog=issues,
                    selected=selected,
                    registry=registry,
                    results=results,
                )
                immutable_json(state / "run-manifest.json", manifest)
                packages = _rehydrate_with_retries(
                    manifest,
                    issues,
                    registry,
                    runtime,
                    state / "assessment-packages",
                    checkpoint_store,
                )
        except ActiveQualificationError:
            raise
        except Exception as error:
            if (state / "run-manifest.json").is_file():
                raise ContractError(
                    "Qualification evidence was checkpointed; resume the same run "
                    "before finalization"
                ) from error
            atomic_json(
                state / "qualification-failure.json",
                {
                    "schema_version": "1.0.0",
                    "run_id": run_id(args.report_date, args.rerun),
                    "profile": profile_name,
                    "failure_code": type(error).__name__,
                },
            )
            raise ContractError(
                "Qualification failed before a complete score; no report was produced"
            ) from error
        return json.dumps(
            {
                "manifest": str(state / "run-manifest.json"),
                "assessment_packages": len(packages),
                "delivery_mode": delivery_mode,
            },
            sort_keys=True,
        )
    if args.command == "finalize":
        manifest = read_json(args.manifest)
        validate_manifest(manifest)
        daily_active = assert_daily_finalization_inputs(
            manifest_path=args.manifest,
            assessments=args.assessment,
            baseline_assessments=args.baseline_assessment,
            prepare_improvement_input=args.prepare_improvement_input,
        )
        test_run = manifest["delivery_mode"] == TEST_EMAIL_ONLY_DELIVERY
        publish_preview = bool(
            daily_active is not None
            and daily_active.value["bindings"]["publish_preview"]
        )
        if publish_preview and not test_run:
            raise ContractError(
                "GitHub preview publication requires an email-only test run"
            )
        planned_preview_links = (
            preview_links(manifest["run_id"]) if publish_preview else None
        )
        work_items = load_quality_work_items(
            args.work_items,
            report_date=date.fromisoformat(manifest["report_date"]),
        )
        work_items_reference = read_json(
            args.manifest.parent / "work-items-reference.json"
        )
        expected_reference = {
            "schema_version": "1.0.0",
            "run_id": manifest["run_id"],
            "report_date": manifest["report_date"],
            "content_digest": content_hash(work_items),
        }
        if work_items_reference != expected_reference:
            raise ContractError(
                "Quality work-item snapshot does not match the qualification run"
            )
        issue_ids = {
            item["issue_id"]
            for agent in manifest["agents"]
            for item in agent["issues"]
            if item.get("status") not in SKIPPED_VERSION_STATUSES
        }
        assessments = load_assessments(
            args.assessment,
            issue_ids,
            args.manifest.parent / "assessment-packages",
            manifest,
        )
        baseline_assessments = load_baseline_assessments(
            args.baseline_assessment,
            args.manifest.parent / "assessment-packages",
            manifest,
        )
        report = build_report(
            manifest,
            issues,
            assessments,
            baseline_assessments,
        )
        improvement_analysis = None
        if manifest["profile"] == "daily":
            living_state_path = (
                args.output_root / "insight-engine-improvement.json"
            )
            previous_improvement_state = (
                read_json(living_state_path)
                if not test_run and living_state_path.exists()
                else None
            )
            normalized_summary = (
                build_normalized_summary(report, previous_improvement_state)
                if previous_improvement_state is not None
                else build_normalized_summary(report)
            )
            analysis_input = (
                args.manifest.parent / "insight-engine-improvement-input.json"
            )
            atomic_json(analysis_input, normalized_summary)
            if args.prepare_improvement_input:
                if args.improvement_analysis is not None:
                    raise ContractError(
                        "Improvement input preparation does not accept analysis output"
                    )
                if daily_active is not None:
                    record_daily_improvement_input(daily_active, analysis_input)
                return json.dumps(
                    {"improvement_analysis_input": str(analysis_input)},
                    sort_keys=True,
                )
            if args.improvement_analysis is None:
                raise ContractError(
                    "Daily finalization requires a schema-valid GPT-5.6 Sol "
                    "Insight Engine improvement analysis; normalized input was "
                    f"written to {analysis_input}"
                )
            improvement_analysis = read_json(args.improvement_analysis)
            validate_analysis_against_summary(
                improvement_analysis,
                normalized_summary,
            )
        elif (
            args.improvement_analysis is not None
            or args.prepare_improvement_input
        ):
            raise ContractError(
                "Insight Engine improvement analysis applies only to Daily"
            )
        if manifest["profile"] == "daily":
            apply_score_comparison(report, args.output_root / "trend.json")
        else:
            apply_staging_score_comparison(
                report,
                runtime_root() / "promotion-receipts",
            )
        output = (
            args.manifest.parent / "final-report"
            if test_run
            else args.output_root
            / "daily"
            / manifest["report_date"].replace("-", os.sep)
            if manifest["profile"] == "daily"
            else args.output_root
            / "staging"
            / manifest["report_date"].replace("-", os.sep)
            / manifest["run_id"]
        )
        official_daily = manifest["profile"] == "daily" and not test_run
        if improvement_analysis is not None:
            if test_run:
                write_improvement_preview(
                    report=report,
                    analysis=improvement_analysis,
                    output=(
                        args.manifest.parent
                        / "insight-engine-improvement-preview"
                    ),
                )
        recipient = resolve_recipient(test_run=test_run)
        runtime_profile = RuntimeProfile.from_env(manifest["profile"])
        official_report_candidate = (
            args.manifest.parent / "official-report-candidate.json"
        )
        if official_daily:
            atomic_json(official_report_candidate, report)
        adx_publication = (
            {"status": "skipped_test", "error_code": None}
            if test_run
            else
            publish_daily_report_best_effort(
                report,
                source_path=official_report_candidate,
                catalogs=(agents, issues),
            )
            if manifest["profile"] == "daily"
            else None
        )
        dashboard_link = (
            resolve_dashboard_link()
            if manifest["profile"] == "daily" and not test_run
            else None
        )
        project_link, agent_links = build_runtime_links(
            runtime_profile,
            [agent["name"] for agent in manifest["agents"]],
        )
        request = create_request(
            report,
            recipient,
            project_link=project_link,
            agent_links=agent_links,
            dashboard_link=dashboard_link,
            adx_publication=adx_publication,
            work_items=work_items,
            test_run=test_run,
            preview_links=planned_preview_links,
        )
        report["delivery"]["content_digest"] = request["content_digest"]
        if official_daily:
            atomic_json(
                official_report_candidate,
                report,
            )
            write_improvement_memory(
                report=report,
                analysis=improvement_analysis,
                reports_root=args.output_root,
                living_state_path=living_state_path,
                report_output=output,
            )
        else:
            write_report(
                report,
                output,
                include_improvement_link=not test_run,
            )
        preview_publication_path = None
        if publish_preview:
            preview_publication_path = (
                args.manifest.parent / "github-preview-publication.json"
            )
            if preview_publication_path.is_file():
                preview_publication = read_json(preview_publication_path)
                validate_preview_publication(
                    preview_publication,
                    run_id=manifest["run_id"],
                )
                verify_daily_email_test_preview(
                    output,
                    preview_publication,
                )
            else:
                preview_publication = publish_daily_email_test_preview(
                    output,
                    run_id=manifest["run_id"],
                )
                immutable_json(preview_publication_path, preview_publication)
            request = bind_preview_publication(request, preview_publication)
        private_request = args.manifest.parent / "email-send-request.json"
        immutable_json(private_request, request)
        private_preview = args.manifest.parent / "report-preview.html"
        write_private_report_preview(request, private_preview)
        if manifest["profile"] == "daily" and not test_run:
            atomic_json(args.output_root / "latest.json", report)
            atomic_text(
                args.output_root / "latest.md",
                (output / "report.md").read_text(encoding="utf-8"),
            )
            update_trend(report, args.output_root / "trend.json")
        if daily_active is not None:
            record_daily_finalization(
                daily_active,
                report_path=output / "report.json",
                email_request_path=private_request,
                improvement_analysis_path=args.improvement_analysis,
                adx_publication_status=(
                    adx_publication["status"]
                    if adx_publication is not None
                    else "not_applicable"
                ),
                preview_publication_path=preview_publication_path,
            )
        validation_policy = None
        if test_run:
            n, k = validation_matrix("baseline")
            validation_policy = {
                "schema_version": "1.0.0",
                "policy": "unified_target_evidence_v1",
                "attempts_per_target": n,
                "required_conclusive_attempts": k,
                "maximum_trace_unknown_attempts": n - k,
            }
            validation_policy["policy_digest"] = content_hash(
                validation_policy
            )
        return json.dumps(
            {
                "quality_score": report["summary"]["quality_score"],
                "report": str(output / "report.json"),
                "email_request": str(private_request),
                "report_preview": str(private_preview),
                "adx_publication": (
                    adx_publication["status"]
                    if adx_publication is not None
                    else "not_applicable"
                ),
                "adx_error_code": (
                    adx_publication.get("error_code")
                    if adx_publication is not None
                    else None
                ),
                "delivery_mode": manifest["delivery_mode"],
                "generated_report": not test_run,
                "validation_policy": validation_policy,
                "github_preview": (
                    str(preview_publication_path)
                    if preview_publication_path is not None
                    else None
                ),
                "pull_request": (
                    "skipped_test"
                    if test_run
                    else "required"
                    if manifest["profile"] == "daily"
                    else "not_applicable"
                ),
            },
            sort_keys=True,
        )
    if args.command == "email-receipt-import":
        daily_active = assert_daily_receipt_import(args.request, args.output)
        import_receipt(read_json(args.request), args.receipt, args.output)
        if daily_active is not None:
            record_daily_email_receipt(daily_active, args.output)
        return "Email receipt imported."
    if args.command == "replay-run":
        manifest = read_json(args.manifest)
        validate_manifest(manifest)
        profile = RuntimeProfile.from_env(
            manifest["profile"],
            manifest["telemetry_resource_set"],
        )
        profile.assert_insights_connection()
        result = LiveRuntime(profile).replay_manifest(manifest)
        return json.dumps(result, indent=2, sort_keys=True)
    if args.command == "validate-generated-paths":
        validate_generated_paths(args.path)
        return None
    if args.command == "validate-published-report":
        report = read_json(args.report)
        if report.get("catalog_hashes") != hashes:
            raise ContractError("Published report does not match trusted base catalogs")
        expected_selection = select_daily(
            date.fromisoformat(report["report_date"]),
            agents,
            issues,
            hashes["issues"],
        )
        validate_published_report(report, issues, expected_selection)
        expected_path = (
            "reports/daily/"
            + report["report_date"].replace("-", "/")
            + "/report.json"
        )
        if args.report_relative_path.replace("\\", "/") != expected_path:
            raise ContractError("Published report date does not match its path")
        expected_markdown = render_markdown(report)
        if (
            args.report_markdown.read_text(encoding="utf-8") != expected_markdown
            or args.latest_markdown.read_text(encoding="utf-8") != expected_markdown
            or read_json(args.latest_json) != report
        ):
            raise ContractError("Published report and latest views are inconsistent")
        expected_agent_reports = {
            item["agent"]: render_agent_markdown(report, item["agent"])
            for item in report["baseline"]
        }
        actual_agent_reports = {
            path.stem: path.read_text(encoding="utf-8")
            for path in args.agent_report
        }
        if actual_agent_reports != expected_agent_reports:
            raise ContractError("Published per-Agent reports are inconsistent")
        validate_published_improvement(
            report=report,
            living_state=read_json(args.improvement_json),
            living_markdown=args.improvement_markdown.read_text(
                encoding="utf-8"
            ),
            snapshot=read_json(args.improvement_snapshot_json),
            snapshot_markdown=args.improvement_snapshot_markdown.read_text(
                encoding="utf-8"
            ),
            previous_state=read_json(args.base_improvement_json),
            previous_markdown=args.base_improvement_markdown.read_text(
                encoding="utf-8"
            ),
        )
        trend = read_json(args.trend)
        base_trend = read_json(args.base_trend)
        matching_days = [
            item
            for item in trend.get("days", [])
            if isinstance(item, dict)
            and item.get("report_date") == report["report_date"]
        ]
        expected_day = {
            "report_date": report["report_date"],
            "baseline_passed": report["summary"]["baseline_passed"],
            "issues_correct": report["summary"]["issues_correct"],
            "issues_incorrect": report["summary"]["issues_incorrect"],
            "issues_missing": report["summary"]["issues_missing"],
            "issues_expected": report["summary"]["issues_expected"],
            "noise_cards": report["summary"]["noise_cards"],
            "duplicate_cards": report["summary"]["duplicate_cards"],
            "quality_score": report["summary"]["quality_score"],
        }
        if matching_days != [expected_day]:
            raise ContractError("Published trend does not match the report")
        if trend != updated_trend(report, base_trend):
            raise ContractError("Published trend rewrites historical results")
        if report.get("score_comparison") != score_comparison(report, base_trend):
            raise ContractError("Published score comparison does not match the trend")
        return None
    if args.command == "create-promotion-receipt":
        report = read_json(args.report)
        registry = read_json(args.registry)
        manifest = read_json(args.manifest)
        receipt = create_promotion_receipt(
            report=report,
            registry=registry,
            manifest=manifest,
            issue_catalog=issues,
            human_reviewed=args.human_reviewed,
        )
        atomic_json(args.output, receipt)
        return "Staging promotion receipt created."
    raise AssertionError("unreachable")
def _run_contract_digest(
    *,
    profile_name: str,
    report_date: str,
    rerun: int,
    delivery_mode: str,
    catalog_hashes: dict[str, str],
    selected: dict[str, list[str]],
    registry: dict[str, Any],
    work_items: dict[str, Any],
    policy: Any,
    seed: int,
    test_region: str,
    test_region_registry: str,
) -> str:
    runtime_files = {
        path.relative_to(ROOT).as_posix(): file_hash(path)
        for path in sorted((ROOT / "src" / "agent_insights_quality").glob("*.py"))
    }
    runtime_files["config/automation.yaml"] = file_hash(
        ROOT / "config" / "automation.yaml"
    )
    for relative in (
        "schemas/run-manifest.schema.json",
        "schemas/prompt-traffic.schema.json",
        "schemas/assessment-package.schema.json",
        "src/agent_insights_quality/prompts/assessment.md",
    ):
        runtime_files[relative] = file_hash(ROOT / relative)
    return content_hash(
        {
            "schema_version": "1.0.0",
            "profile": profile_name,
            "report_date": report_date,
            "rerun": rerun,
            "delivery_mode": delivery_mode,
            "catalog_hashes": catalog_hashes,
            "selected": selected,
            "registry_hash": content_hash(registry),
            "work_items_hash": content_hash(work_items),
            "lookback_hours": policy.insight_lookback_hours,
            "max_parallel_agents": policy.max_parallel_agents,
            "agent_start_stagger_seconds": policy.agent_start_stagger_seconds,
            "telemetry_resource_set": policy.telemetry_resource_set,
            "seed": seed,
            "test_region": test_region,
            "test_region_registry": test_region_registry,
            "runtime_files": runtime_files,
        }
    )


def _assert_insight_state_resolved(
    checkpoint_store: VersionCheckpointStore,
) -> None:
    if checkpoint_store.has_unresolved_insight_state():
        raise ContractError(
            "Qualification has unresolved Agent Insights state; resume before "
            "creating the immutable manifest"
        )


def _rehydrate_with_retries(
    manifest: dict[str, Any],
    issues: dict[str, Any],
    registry: dict[str, Any],
    runtime: Any,
    output: Path,
    checkpoint_store: VersionCheckpointStore,
) -> list[Path]:
    for attempt in range(3):
        try:
            return rehydrate_packages(
                manifest,
                issues,
                registry,
                runtime,
                output,
                checkpoint_store,
            )
        except ContractError:
            if attempt == 2:
                raise
            runtime.report_progress(
                "assessment package generation failed transiently; "
                f"retrying ({attempt + 2}/3)"
            )
            time.sleep(2**attempt)
    raise ContractError("Assessment package retry loop did not execute")
