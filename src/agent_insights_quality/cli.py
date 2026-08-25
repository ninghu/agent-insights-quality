from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path
from agent_insights_quality.assessment import (
    load_assessments,
    load_baseline_assessments,
    rehydrate_packages,
)
from agent_insights_quality.azure import deploy_infrastructure
from agent_insights_quality.catalogs import (
    catalog_hashes,
    catalog_summary,
    generate_docs,
    load_catalogs,
)
from agent_insights_quality.email import (
    build_runtime_links,
    create_request,
    import_receipt,
    resolve_recipient,
    validate_published_receipt,
)
from agent_insights_quality.generated_paths import validate_generated_paths
from agent_insights_quality.live import LiveRuntime
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.provisioning import (
    create_promotion_receipt,
    provision_profile,
    validate_promotion_receipt,
)
from agent_insights_quality.registry import load_registry
from agent_insights_quality.reporting import (
    build_operational_failure_report,
    build_report,
    update_trend,
    write_report,
    validate_published_report,
    render_markdown,
    render_agent_markdown,
)
from agent_insights_quality.run_manifest import build_manifest, run_id, validate_manifest
from agent_insights_quality.runner import execute
from agent_insights_quality.selection import select_daily, select_full
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    immutable_json,
    read_json,
)
from agent_insights_quality.validation import validate_repository


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
    provision = commands.add_parser("provision")
    provision.add_argument("--profile", choices=("daily", "staging"), required=True)
    for name in ("run-daily", "run-full"):
        run = commands.add_parser(name)
        run.add_argument("--report-date", required=True, type=date.fromisoformat)
        run.add_argument("--rerun", type=int, default=0)
        run.add_argument("--state-root", type=Path, default=ROOT / ".aiq-runtime")
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
            approved_digests = validate_promotion_receipt(Path(receipt), hashes)
        provision_profile(
            profile=profile,
            agents=agents,
            issues=issues,
            approved_digests=approved_digests,
        )
        return f"{args.profile} profile provisioned."
    if args.command in {"run-daily", "run-full"}:
        profile_name = "daily" if args.command == "run-daily" else "staging"
        selected = (
            select_daily(args.report_date, agents, issues, hashes["issues"])
            if profile_name == "daily"
            else select_full(agents)
        )
        state = args.state_root / profile_name / run_id(args.report_date, args.rerun)
        try:
            profile = RuntimeProfile.from_env(profile_name)
            registry = load_registry(
                profile.registry_path,
                profile=profile_name,
                catalog_hashes=hashes,
            )
            runtime = LiveRuntime(profile)
            results = execute(
                agents=agents,
                issues=issues,
                selected=selected,
                registry=registry,
                runtime=runtime,
                seed=int(hashes["issues"].split(":")[1][:16], 16),
            )
            manifest = build_manifest(
                report_date=args.report_date,
                profile=profile_name,
                rerun=args.rerun,
                catalog_hashes=hashes,
                selected=selected,
                registry=registry,
                results=results,
            )
            immutable_json(state / "run-manifest.json", manifest)
            packages = rehydrate_packages(
                manifest,
                issues,
                registry,
                runtime,
                state / "assessment-packages",
            )
        except Exception as error:
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
                ROOT
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
                recipient = resolve_recipient()
                request = create_request(failure, recipient)
                failure["delivery"]["content_digest"] = request["content_digest"]
                write_report(failure, failure_root)
                atomic_json(state / "email-send-request.json", request)
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
            },
            sort_keys=True,
        )
    if args.command == "finalize":
        manifest = read_json(args.manifest)
        validate_manifest(manifest)
        issue_ids = {
            item["issue_id"]
            for agent in manifest["agents"]
            for item in agent["issues"]
        }
        assessments = load_assessments(
            args.assessment,
            issue_ids,
            args.manifest.parent / "assessment-packages",
        )
        baseline_assessments = load_baseline_assessments(
            args.baseline_assessment,
            args.manifest.parent / "assessment-packages",
        )
        report = build_report(
            manifest,
            issues,
            assessments,
            baseline_assessments,
        )
        output = (
            args.output_root
            / "daily"
            / manifest["report_date"].replace("-", os.sep)
            if manifest["profile"] == "daily"
            else args.output_root
            / "staging"
            / manifest["report_date"].replace("-", os.sep)
            / manifest["run_id"]
        )
        write_report(report, output)
        recipient = resolve_recipient()
        runtime_profile = RuntimeProfile.from_env(manifest["profile"])
        project_link, agent_links = build_runtime_links(
            runtime_profile,
            [agent["name"] for agent in manifest["agents"]],
        )
        request = create_request(
            report,
            recipient,
            project_link=project_link,
            agent_links=agent_links,
        )
        report["delivery"]["content_digest"] = request["content_digest"]
        write_report(report, output)
        private_request = args.manifest.parent / "email-send-request.json"
        atomic_json(private_request, request)
        if manifest["profile"] == "daily":
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
            },
            sort_keys=True,
        )
    if args.command == "email-receipt-import":
        import_receipt(read_json(args.request), args.receipt, args.output)
        return "Email receipt imported."
    if args.command == "replay-run":
        manifest = read_json(args.manifest)
        validate_manifest(manifest)
        profile = RuntimeProfile.from_env(manifest["profile"])
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
