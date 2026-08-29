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
from agent_insights_quality.email import (
    build_runtime_links,
    create_request,
    import_receipt,
    resolve_recipient,
    validate_published_receipt,
    write_private_report_preview,
)
from agent_insights_quality.generated_paths import validate_generated_paths
from agent_insights_quality.live import LiveRuntime
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.provisioning import (
    create_promotion_receipt,
    provision_profile,
    validate_promotion_receipt,
)
from agent_insights_quality.registry import load_registry, sync_registry
from agent_insights_quality.reporting import (
    apply_score_comparison,
    apply_staging_score_comparison,
    build_operational_failure_report,
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
    content_hash,
    file_hash,
    immutable_json,
    read_json,
    runtime_root,
)
from agent_insights_quality.validation import validate_repository
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
    for name in ("run-daily", "run-full"):
        run = commands.add_parser(name)
        run.add_argument("--report-date", required=True, type=date.fromisoformat)
        run.add_argument("--rerun", type=int, default=0)
        run.add_argument("--state-root", type=Path, default=runtime_root())
        run.add_argument("--work-items", type=Path, required=True)
        if name == "run-daily":
            run.add_argument("--test-run", action="store_true")
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
    published.add_argument("--receipt", type=Path, required=True)
    published.add_argument("--report-relative-path", required=True)
    published.add_argument("--report-markdown", type=Path, required=True)
    published.add_argument("--latest-json", type=Path, required=True)
    published.add_argument("--latest-markdown", type=Path, required=True)
    published.add_argument("--trend", type=Path, required=True)
    published.add_argument("--base-trend", type=Path, required=True)
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
        profile = RuntimeProfile.from_env(args.profile)
        approved_digests = None
        if args.profile == "daily":
            receipt = str(
                os.environ.get("AIQ_STAGING_PROMOTION_RECEIPT") or ""
            ).strip()
            if not receipt:
                raise ContractError(
                    "Daily provisioning requires a human-reviewed staging promotion receipt"
                )
            approved_digests = validate_promotion_receipt(
                Path(receipt),
                hashes,
                agent_model_contract(agents),
            )
        provision_profile(
            profile=profile,
            agents=agents,
            issues=issues,
            approved_digests=approved_digests,
        )
        return f"{args.profile} profile provisioned."
    if args.command in {"run-daily", "run-full"}:
        policy = load_automation_policy()
        profile_name = "daily" if args.command == "run-daily" else "staging"
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
        sync_registry(profile)
        registry = load_registry(
            profile.registry_path,
            profile=profile_name,
            catalog_hashes=hashes,
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
            failure = build_operational_failure_report(
                report_date=args.report_date,
                run_id=run_id(args.report_date, args.rerun),
                profile=profile_name,
                selected=selected,
                issues=issues,
                failure_code=type(error).__name__,
                catalog_hashes=hashes,
            )
            failure_root = (
                state / "final-report"
                if test_run
                else ROOT
                / "reports"
                / "daily"
                / f"{args.report_date:%Y}"
                / f"{args.report_date:%m}"
                / f"{args.report_date:%d}"
                if profile_name == "daily"
                else state
            )
            write_report(failure, failure_root)
            handoff_written = False
            try:
                recipient = resolve_recipient(test_run=test_run)
                adx_publication = (
                    {"status": "skipped_test", "error_code": None}
                    if test_run
                    else
                    publish_daily_report_best_effort(
                        failure,
                        source_path=failure_root / "report.json",
                        catalogs=(agents, issues),
                    )
                    if profile_name == "daily"
                    else None
                )
                dashboard_link = (
                    resolve_dashboard_link()
                    if profile_name == "daily" and not test_run
                    else None
                )
                request = create_request(
                    failure,
                    recipient,
                    dashboard_link=dashboard_link,
                    adx_publication=adx_publication,
                    work_items=work_items,
                    test_run=test_run,
                )
                failure["delivery"]["content_digest"] = request["content_digest"]
                write_report(failure, failure_root)
                atomic_json(state / "email-send-request.json", request)
                write_private_report_preview(
                    request,
                    state / "report-preview.html",
                )
                handoff_written = True
            except ContractError:
                pass
            raise ContractError(
                "Qualification failed closed; an INCOMPLETE report was written"
                + (
                    " with its email request"
                    if handoff_written
                    else ", but the email request could not be rendered"
                )
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
        test_run = manifest["delivery_mode"] == TEST_EMAIL_ONLY_DELIVERY
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
        write_report(report, output)
        recipient = resolve_recipient(test_run=test_run)
        runtime_profile = RuntimeProfile.from_env(manifest["profile"])
        adx_publication = (
            {"status": "skipped_test", "error_code": None}
            if test_run
            else
            publish_daily_report_best_effort(
                report,
                source_path=output / "report.json",
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
        )
        report["delivery"]["content_digest"] = request["content_digest"]
        write_report(report, output)
        private_request = args.manifest.parent / "email-send-request.json"
        atomic_json(private_request, request)
        private_preview = args.manifest.parent / "report-preview.html"
        write_private_report_preview(request, private_preview)
        if manifest["profile"] == "daily" and not test_run:
            atomic_json(args.output_root / "latest.json", report)
            (args.output_root / "latest.md").write_text(
                (output / "report.md").read_text(encoding="utf-8"),
                encoding="utf-8",
                newline="\n",
            )
            update_trend(report, args.output_root / "trend.json")
        return json.dumps(
            {
                "status": report["status"],
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
        import_receipt(read_json(args.request), args.receipt, args.output)
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
        validate_published_receipt(
            args.receipt,
            report["delivery"]["content_digest"],
        )
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
            "status": report["status"],
            "baseline_passed": report["summary"]["baseline_passed"],
            "issues_correct": report["summary"]["issues_correct"],
            "issues_expected": report["summary"]["issues_expected"],
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
            "agent_start_stagger_seconds": policy.agent_start_stagger_seconds,
            "telemetry_resource_set": policy.telemetry_resource_set,
            "seed": seed,
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
