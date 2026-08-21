from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from agent_insights_quality.contracts import (
    ContractError,
    ROOT,
    load_agent_manifests,
    load_data,
    load_scenario_catalog,
    validate_contracts,
    validate_daily_plan_semantics,
    validate_instance,
)
from agent_insights_quality.ado import AdoClient, AdoRuntimeConfig, plan_bug_action
from agent_insights_quality.docs import generate_documents
from agent_insights_quality.finalizer import (
    build_failure_report,
    build_preflight_plan,
    create_failure_send_request,
    finalize_success,
    render_failure_email_html,
    write_daily_artifacts,
)
from agent_insights_quality.generated_paths import changed_paths, validate_generated_paths
from agent_insights_quality.judging import (
    export_judge_package,
    import_judgment,
    project_evidence,
    validate_judge_package,
)
from agent_insights_quality.memory import reconcile_memory, render_memory_markdown
from agent_insights_quality.public_safety import validate_public_repository_content
from agent_insights_quality.reporting import finalize_readiness_failure, record_email_delivery
from agent_insights_quality.readiness import require_daily_runtime
from agent_insights_quality.reporting import (
    create_email_send_request,
    import_email_receipt,
    render_email_html,
    render_report_markdown,
    resolve_recipient,
)
from agent_insights_quality.artifact_io import content_hash, read_json_object, write_json
from agent_insights_quality.scoring import case_to_insight_mappings, score_run
from agent_insights_quality.security import validate_no_direct_trace_injection
from agent_insights_quality.runtime.adapters import load_runtime_hooks
from agent_insights_quality.runtime.artifacts import AzureBlobArtifactStore, LocalArtifactStore
from agent_insights_quality.runtime.azure import AzureCli, AzureProjectManager, select_azure_context
from agent_insights_quality.runtime.config import RuntimeConfig
from agent_insights_quality.runtime.errors import RuntimeFailure
from agent_insights_quality.runtime.orchestrator import PlanInput, ProductionOrchestrator
from agent_insights_quality.runtime.receipts import MonitorOwnershipRegistry, read_receipt
from agent_insights_quality.insights.client import AgentInsightsClient
from agent_insights_quality.runtime.azure import AzureCliCredential


def _require_private_runtime_output(path: Path) -> None:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(ROOT.resolve())
    except ValueError:
        return
    if not relative.parts or relative.parts[0] != ".aiq-runtime":
        raise ContractError(
            "Private email/link handoffs must be written outside public repository paths "
            "or under the ignored .aiq-runtime directory"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiq-quality")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate all repository contracts and generated docs")
    plan_parser = subparsers.add_parser("plan", help="Generate a deterministic daily plan")
    plan_parser.add_argument("--report-date", required=True, type=date.fromisoformat)
    plan_parser.add_argument("--output-dir", type=Path)
    plan_parser.add_argument("--rerun", type=int, default=0)
    plan_parser.add_argument(
        "--full-catalog",
        action="store_true",
        help="Run the non-human-daily full catalog for special release qualification",
    )
    docs_parser = subparsers.add_parser("generate-docs", help="Render manifest-backed documentation")
    docs_parser.add_argument("--check", action="store_true", help="Fail instead of writing stale docs")
    path_parser = subparsers.add_parser(
        "validate-generated-paths",
        help="Enforce the daily automation generated-path allowlist",
    )
    path_parser.add_argument("--base-ref", help="Git ref used to discover changed paths")
    path_parser.add_argument("--path", action="append", default=[], help="Explicit changed path")
    subparsers.add_parser(
        "check-runtime-readiness",
        help="Fail closed with INCONCLUSIVE until every daily runtime component is ready",
    )
    run_parser = subparsers.add_parser(
        "run-daily",
        help="Start the daily workflow only after the reviewed runtime readiness gate passes",
    )
    run_parser.add_argument("--report-date", required=True, help="Pacific report date (YYYY-MM-DD)")
    run_parser.add_argument("--output-root", type=Path, default=ROOT / "reports")
    failure_parser = subparsers.add_parser(
        "finalize-readiness-failure",
        help="Render the safe INCONCLUSIVE report and one-message mail handoff",
    )
    failure_parser.add_argument("--report-date", required=True, help="Pacific report date (YYYY-MM-DD)")
    failure_parser.add_argument("--output-root", type=Path, default=ROOT / "reports")
    delivery_parser = subparsers.add_parser(
        "record-email-result",
        help="Record the sanitized result of the one required Copilot mail action",
    )
    delivery_parser.add_argument("--handoff", required=True, type=Path)
    delivery_parser.add_argument("--status", required=True, choices=("sent", "failed"))
    delivery_parser.add_argument("--receipt-reference")
    delivery_parser.add_argument("--error-code")
    preflight = subparsers.add_parser("preflight", help="Validate protected runtime configuration and Azure identity")
    preflight.add_argument("--discover-project", action="store_true")
    for command, help_text in (
        ("run", "Execute a reviewed plan through the configured runtime adapter"),
        ("resume", "Resume an idempotent runtime execution from its receipt"),
    ):
        runtime = subparsers.add_parser(command, help=help_text)
        runtime.add_argument("--plan", type=Path, required=True)
        runtime.add_argument("--state", type=Path, required=True)
        runtime.add_argument("--dry-run", action="store_true")
        runtime.add_argument("--max-parallel-agents", type=int, default=5)
    status = subparsers.add_parser("status", help="Read a public-safe runtime receipt")
    status.add_argument("--state", type=Path, required=True)
    cleanup = subparsers.add_parser("cleanup", help="Delete only exact owned expired runtime resources")
    cleanup.add_argument("--execute", action="store_true", help="Apply cleanup; default is dry-run")
    projection = subparsers.add_parser(
        "project-evidence", help="Build and validate a bounded judge evidence bundle"
    )
    projection.add_argument("--input", required=True)
    projection.add_argument("--output", required=True)

    judge_export = subparsers.add_parser(
        "judge-package-export", help="Export a primary Copilot judgment handoff"
    )
    judge_export.add_argument("--bundle", required=True)
    judge_export.add_argument("--output", required=True)
    judge_import = subparsers.add_parser(
        "judge-package-import", help="Validate and import a primary Copilot judgment"
    )
    judge_import.add_argument("--package", required=True)
    judge_import.add_argument("--judgment", required=True)
    judge_import.add_argument("--output", required=True)
    verifier_export = subparsers.add_parser(
        "verifier-export", help="Export an independent blinded-verifier handoff"
    )
    verifier_export.add_argument("--bundle", required=True)
    verifier_export.add_argument("--output", required=True)
    verifier_import = subparsers.add_parser(
        "verifier-import", help="Validate and import a blinded-verifier judgment"
    )
    verifier_import.add_argument("--package", required=True)
    verifier_import.add_argument("--judgment", required=True)
    verifier_import.add_argument("--output", required=True)

    score = subparsers.add_parser("score", help="Recompute the deterministic strict scorecard")
    score.add_argument("--plan", required=True)
    score.add_argument("--evidence", action="append", default=[], required=True)
    score.add_argument("--judgment", action="append", default=[], required=True)
    score.add_argument("--output", required=True)
    score.add_argument("--mappings-output")

    memory = subparsers.add_parser(
        "memory-reconcile", help="Reconcile durable issue memory from confirmed findings"
    )
    memory.add_argument("--memory", required=True)
    memory.add_argument("--findings", required=True)
    memory.add_argument("--report-id", required=True)
    memory.add_argument("--run-id", required=True)
    memory.add_argument("--report-date", required=True)
    memory.add_argument("--report-path", required=True)
    memory.add_argument("--generated-at", required=True)
    memory.add_argument("--complete", action="store_true")
    memory.add_argument("--output", required=True)
    memory.add_argument("--markdown-output")

    ado_dry = subparsers.add_parser(
        "ado-dry-run", help="Plan ADO candidate/create/update/reopen behavior without side effects"
    )
    ado_dry.add_argument("--candidate", required=True)
    ado_dry.add_argument("--existing")
    ado_dry.add_argument(
        "--mode", choices=("candidate-only", "dry-run"), default="dry-run"
    )
    ado_dry.add_argument("--output", required=True)
    ado_apply = subparsers.add_parser(
        "ado-apply", help="Apply an eligible ADO action using protected runtime values"
    )
    ado_apply.add_argument("--candidate", required=True)
    ado_apply.add_argument("--output", required=True)

    report = subparsers.add_parser(
        "render-report", help="Render detailed engineering Markdown from a canonical report"
    )
    report.add_argument("--report", required=True)
    report.add_argument("--output", required=True)
    email = subparsers.add_parser(
        "render-email", help="Render and validate the direct-email handoff"
    )
    email.add_argument("--report", required=True)
    email.add_argument("--trend", required=True)
    email.add_argument("--agent-links", required=True)
    email.add_argument("--output", required=True)
    email.add_argument("--request-output", required=True)
    receipt = subparsers.add_parser(
        "email-receipt-import",
        help="Validate a connected-mail provider receipt for an email handoff",
    )
    receipt.add_argument("--request", required=True)
    receipt.add_argument("--receipt", required=True)
    receipt.add_argument("--output", required=True)

    failure = subparsers.add_parser(
        "render-failure", help="Finalize an INCONCLUSIVE report and explicit unsent email"
    )
    failure.add_argument("--plan", required=True)
    failure.add_argument("--failure", required=True)
    failure.add_argument("--generated-at", required=True)
    failure.add_argument("--output-root", default=str(ROOT))
    failure.add_argument("--request-output", required=True)

    finalization = subparsers.add_parser(
        "finalize", help="Write generated-only daily artifacts and an unsent email request"
    )
    finalization.add_argument("--plan", required=True)
    finalization.add_argument("--report", required=True)
    finalization.add_argument("--prior-report", action="append", default=[])
    finalization.add_argument("--agent-links", required=True)
    finalization.add_argument("--output-root", default=str(ROOT))
    finalization.add_argument("--request-output", required=True)
    return parser


def _load_plan(path: Path) -> PlanInput:
    payload = load_data(path)
    if not isinstance(payload, dict):
        raise RuntimeFailure("invalid_plan", "Runtime plan must be a JSON object.")
    validate_instance(payload, ROOT / "schemas" / "daily-plan.schema.json", str(path))
    agents = load_agent_manifests()
    catalog = load_scenario_catalog({agent["id"] for agent in agents})
    validate_daily_plan_semantics(payload, agents, catalog, str(path))
    return PlanInput.from_daily_plan(payload)


def _runtime_context(config: RuntimeConfig) -> tuple[AzureCli, AzureProjectManager]:
    cli = AzureCli()
    context = select_azure_context(cli, config.azure)
    return cli, AzureProjectManager(cli, context, config.azure, config.automation_owner)


def _public_reference(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _public_references(values: Sequence[str]) -> list[str]:
    return [_public_reference(value) for value in values]


def _finalize_readiness(args: argparse.Namespace, readiness: dict[str, object]) -> Path:
    return finalize_readiness_failure(
        readiness,
        load_data(ROOT / "config" / "reporting.yaml"),
        args.report_date,
        output_root=args.output_root,
    )


def run(args: argparse.Namespace) -> None:
    if args.command == "validate":
        validate_contracts()
        generate_documents(check=True)
        validate_no_direct_trace_injection()
        validate_public_repository_content()
        print("Repository contracts are valid.")
    elif args.command == "plan":
        from agent_insights_quality.planning import write_daily_plan

        json_path, markdown_path = write_daily_plan(
            args.report_date,
            args.output_dir,
            rerun=args.rerun,
            full_catalog=args.full_catalog,
        )
        print(f"Wrote {json_path} and {markdown_path}.")
    elif args.command == "generate-docs":
        generate_documents(check=args.check)
        print("Generated documentation is current.")
    elif args.command == "validate-generated-paths":
        paths = list(args.path)
        if args.base_ref:
            paths.extend(changed_paths(args.base_ref))
        if not paths:
            raise ContractError("No changed paths supplied")
        validate_generated_paths(paths)
        print("Generated change paths are allowed.")
    elif args.command == "check-runtime-readiness":
        require_daily_runtime(load_data(ROOT / "config" / "runtime-readiness.yaml"))
        print("Daily runtime is ready.")
    elif args.command in {"run-daily", "finalize-readiness-failure"}:
        readiness = load_data(ROOT / "config" / "runtime-readiness.yaml")
        if args.command == "finalize-readiness-failure":
            handoff = _finalize_readiness(args, readiness)
            print(f"Readiness failure finalized; email handoff: {handoff}")
            return
        try:
            require_daily_runtime(readiness)
        except ContractError as error:
            handoff = _finalize_readiness(args, readiness)
            raise ContractError(f"{error} Email-required handoff: {handoff}") from error
        else:
            raise ContractError(
                "INCONCLUSIVE: readiness is enabled but the runtime entry point is not installed."
            )
    elif args.command == "record-email-result":
        record_email_delivery(
            args.handoff,
            status=args.status,
            receipt_reference=args.receipt_reference,
            error_code=args.error_code,
        )
        print("Email delivery result recorded.")
    elif args.command == "preflight":
        config = RuntimeConfig.from_env()
        cli, projects = _runtime_context(config)
        result: dict[str, object] = {"status": "ready", "configuration": config.public_summary()}
        if args.discover_project or config.azure.resource_group is None:
            project = projects.discover_qualified()
        else:
            project = projects.validate_explicit_project()
        result["project_reference"] = _public_reference(project.project_id)
        endpoint = project.project_endpoint
        if endpoint is None:
            raise RuntimeFailure("missing_project_endpoint", "Project endpoint could not be resolved.")
        AgentInsightsClient(endpoint, AzureCliCredential(cli)).probe()
        print(json.dumps(result, sort_keys=True))
    elif args.command in {"run", "resume"}:
        config = RuntimeConfig.from_env()
        plan = _load_plan(args.plan)
        hooks = load_runtime_hooks(config, validation_only=args.dry_run)
        state = ProductionOrchestrator(
            hooks,
            args.state,
            max_parallel_agents=args.max_parallel_agents,
        ).run(plan, resume=args.command == "resume", dry_run=args.dry_run)
        print(json.dumps(state.public_dict(), sort_keys=True))
    elif args.command == "status":
        print(json.dumps(read_receipt(args.state), sort_keys=True))
    elif args.command == "cleanup":
        config = RuntimeConfig.from_env()
        cli, projects = _runtime_context(config)
        if config.artifacts.backend == "local":
            artifacts = LocalArtifactStore(Path(config.artifacts.location))
        else:
            artifacts = AzureBlobArtifactStore.from_identity(
                account_url=config.artifacts.location,
                container=config.artifacts.container or "",
                credential=AzureCliCredential(cli),
            )
        dry_run = not args.execute
        result: dict[str, object] = {
            "dry_run": dry_run,
        }
        cleanup_failures: list[str] = []
        selected_monitors: list[str] = []
        credential = AzureCliCredential(cli)
        for project in projects.qualified_projects(cleanup_failures):
            ownership = MonitorOwnershipRegistry(
                Path(config.monitor_ownership_receipt),
                project.project_id,
            )
            try:
                monitor_ids = AgentInsightsClient(
                    project.project_endpoint,
                    credential,
                    ownership_registry=ownership,
                ).cleanup_owned_monitors(
                    dry_run=dry_run,
                )
            except RuntimeFailure as error:
                cleanup_failures.append(error.code)
                continue
            selected_monitors.extend(
                f"{project.project_name}:{monitor_id}" for monitor_id in monitor_ids
            )
        try:
            selected_connections = projects.cleanup_owned_connections(
                config.automation_owner,
                dry_run=dry_run,
            )
        except RuntimeFailure as error:
            cleanup_failures.append(error.code)
            selected_connections = []
        try:
            selected_artifacts = artifacts.cleanup_expired(
                config.automation_owner,
                dry_run=dry_run,
            )
        except RuntimeFailure as error:
            cleanup_failures.append(error.code)
            selected_artifacts = []
        try:
            selected_projects = projects.cleanup_expired(dry_run=dry_run)
        except RuntimeFailure as error:
            cleanup_failures.append(error.code)
            selected_projects = []
        if cleanup_failures:
            raise RuntimeFailure(
                "cleanup_partial_failure",
                "Cleanup processed every resource class but one or more owned resources failed.",
                {
                    "failure_count": len(cleanup_failures),
                    "failure_codes": sorted(set(cleanup_failures)),
                },
            )
        result["project_references"] = _public_references(selected_projects)
        result["connection_references"] = _public_references(selected_connections)
        result["monitor_references"] = _public_references(selected_monitors)
        result["artifact_references"] = _public_references(selected_artifacts)
        print(json.dumps(result, sort_keys=True))
    elif args.command == "project-evidence":
        write_json(
            Path(args.output),
            project_evidence(read_json_object(Path(args.input), "raw evidence")),
        )
        print("Bounded evidence bundle written.")
    elif args.command in {"judge-package-export", "verifier-export"}:
        role = "primary" if args.command == "judge-package-export" else "blinded_verifier"
        bundle = read_json_object(Path(args.bundle), "evidence bundle")
        write_json(Path(args.output), export_judge_package(bundle, role))
        print(f"{role.replace('_', ' ').title()} handoff written.")
    elif args.command in {"judge-package-import", "verifier-import"}:
        package = read_json_object(Path(args.package), "judge package")
        expected = "primary" if args.command == "judge-package-import" else "blinded_verifier"
        validate_judge_package(package)
        if package["judge_role"] != expected:
            raise ContractError(f"Expected a {expected} judge package")
        judgment = import_judgment(
            package,
            read_json_object(Path(args.judgment), "judgment"),
        )
        write_json(Path(args.output), judgment)
        print(f"{expected.replace('_', ' ').title()} judgment imported.")
    elif args.command == "score":
        plan = read_json_object(Path(args.plan), "daily plan")
        evidence = [
            read_json_object(Path(path), "evidence bundle") for path in args.evidence
        ]
        judgments = [
            read_json_object(Path(path), "judgment") for path in args.judgment
        ]
        scorecard = score_run(plan, evidence, judgments)
        write_json(Path(args.output), scorecard)
        if args.mappings_output:
            write_json(
                Path(args.mappings_output),
                case_to_insight_mappings(plan, evidence, judgments),
            )
        print(scorecard["verdict"])
    elif args.command == "memory-reconcile":
        findings = load_data(Path(args.findings))
        if not isinstance(findings, list):
            raise ContractError("findings must be a JSON array")
        updated, changes = reconcile_memory(
            read_json_object(Path(args.memory), "quality memory"),
            findings,
            report_id=args.report_id,
            run_id=args.run_id,
            report_date=args.report_date,
            report_path=args.report_path,
            generated_at=args.generated_at,
            complete=args.complete,
        )
        write_json(Path(args.output), updated)
        if args.markdown_output:
            Path(args.markdown_output).write_bytes(render_memory_markdown(updated).encode("ascii"))
        print(json.dumps(changes, sort_keys=True))
    elif args.command == "ado-dry-run":
        candidate = read_json_object(Path(args.candidate), "ADO candidate")
        existing = load_data(Path(args.existing)) if args.existing else []
        if not isinstance(existing, list):
            raise ContractError("existing ADO work items must be a JSON array")
        write_json(
            Path(args.output),
            plan_bug_action(candidate, existing, mode=args.mode),
        )
        print("ADO side-effect-free plan written.")
    elif args.command == "ado-apply":
        candidate = read_json_object(Path(args.candidate), "ADO candidate")
        if not plan_bug_action(candidate, [], mode="apply")["eligible"]:
            result = plan_bug_action(candidate, [], mode="candidate-only")
        else:
            client = AdoClient(AdoRuntimeConfig.from_env())
            existing = client.search_duplicates(candidate)
            result = plan_bug_action(candidate, existing, mode="apply")
            matched = next(
                (
                    item
                    for item in existing
                    if content_hash({"work_item_id": item.get("id")})
                    == result["matched_reference"]
                ),
                None,
            )
            if result["action"] == "created":
                applied = client.create_bug(candidate, client.fetch_template())
            elif result["action"] == "reopened" and matched:
                applied = client.reopen(
                    int(matched["id"]),
                    candidate,
                    client.fetch_template(),
                )
            elif result["action"] == "updated" and matched:
                applied = client.update_bug(int(matched["id"]), candidate)
            else:
                raise ContractError(f"Unhandled eligible ADO action: {result['action']}")
            if not isinstance(applied.get("id"), int):
                raise ContractError("ADO apply did not confirm the work-item ID")
            result["confirmed_reference"] = content_hash(
                {"work_item_id": applied["id"]}
            )
        write_json(Path(args.output), result)
        print(result["action"])
    elif args.command == "render-report":
        markdown = render_report_markdown(
            read_json_object(Path(args.report), "canonical report")
        )
        Path(args.output).write_bytes(markdown.encode("ascii"))
        print("Detailed report rendered.")
    elif args.command == "render-email":
        report = read_json_object(Path(args.report), "canonical report")
        trend = read_json_object(Path(args.trend), "trend")
        links = read_json_object(Path(args.agent_links), "runtime agent links")
        reporting = load_data(ROOT / "config" / "reporting.yaml")
        recipient = resolve_recipient(reporting)
        subject, body = render_email_html(report, trend, links)
        _require_private_runtime_output(Path(args.output))
        _require_private_runtime_output(Path(args.request_output))
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_bytes(body.encode("ascii"))
        write_json(
            Path(args.request_output),
            create_email_send_request(report, trend, links, recipient),
        )
        print(subject)
    elif args.command == "email-receipt-import":
        receipt = import_email_receipt(
            read_json_object(Path(args.request), "email send request"),
            read_json_object(Path(args.receipt), "email receipt"),
        )
        write_json(Path(args.output), receipt)
        print(receipt["state"])
    elif args.command == "render-failure":
        plan = read_json_object(Path(args.plan), "daily plan")
        failure = read_json_object(Path(args.failure), "failure")
        report = build_failure_report(plan, failure, generated_at=args.generated_at)
        body = render_failure_email_html(report)
        request = create_failure_send_request(
            report,
            resolve_recipient(load_data(ROOT / "config" / "reporting.yaml")),
        )
        report["delivery"]["request_reference"] = request["request_hash"]
        _require_private_runtime_output(Path(args.request_output))
        write_daily_artifacts(
            Path(args.output_root),
            plan,
            report,
            failure_email=body,
        )
        write_json(Path(args.request_output), request)
        print("INCONCLUSIVE failure artifacts written; email remains unsent.")
    elif args.command == "finalize":
        plan = read_json_object(Path(args.plan), "daily plan")
        report = read_json_object(Path(args.report), "canonical report")
        prior = [
            read_json_object(Path(path), "prior canonical report")
            for path in args.prior_report
        ]
        links = read_json_object(Path(args.agent_links), "runtime agent links")
        recipient = resolve_recipient(load_data(ROOT / "config" / "reporting.yaml"))
        _require_private_runtime_output(Path(args.request_output))
        result = finalize_success(report, prior, links, recipient)
        write_daily_artifacts(Path(args.output_root), plan, result["report"])
        validate_generated_paths(["reports/trend.json"])
        write_json(Path(args.output_root) / "reports" / "trend.json", result["trend"])
        write_json(Path(args.request_output), result["email_send_request"])
        print("Generated artifacts written; email remains unsent.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        run(parser.parse_args(argv))
    except (ContractError, RuntimeFailure) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0
