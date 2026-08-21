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
from agent_insights_quality.docs import generate_documents
from agent_insights_quality.generated_paths import changed_paths, validate_generated_paths
from agent_insights_quality.public_safety import validate_public_repository_content
from agent_insights_quality.reporting import finalize_readiness_failure, record_email_delivery
from agent_insights_quality.readiness import require_daily_runtime
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiq-quality")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate all repository contracts and generated docs")
    plan_parser = subparsers.add_parser("plan", help="Generate a deterministic daily plan")
    plan_parser.add_argument("--report-date", required=True, type=date.fromisoformat)
    plan_parser.add_argument("--output-dir", type=Path)
    plan_parser.add_argument("--rerun", type=int, default=0)
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
        if (
            args.discover_project
            or config.azure.resource_group is None
            or config.azure.project_name is not None
        ):
            project = projects.discover_qualified()
            result["project_reference"] = _public_reference(project.project_id)
            endpoint = project.project_endpoint
        else:
            endpoint = config.azure.project_endpoint
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        run(parser.parse_args(argv))
    except (ContractError, RuntimeFailure) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0
