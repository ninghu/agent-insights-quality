from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from agent_insights_quality.contracts import ContractError, ROOT, load_data, validate_contracts
from agent_insights_quality.docs import generate_documents
from agent_insights_quality.generated_paths import changed_paths, validate_generated_paths
from agent_insights_quality.public_safety import validate_public_repository_content
from agent_insights_quality.reporting import finalize_readiness_failure, record_email_delivery
from agent_insights_quality.readiness import require_daily_runtime
from agent_insights_quality.security import validate_no_direct_trace_injection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiq-quality")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate all repository contracts and generated docs")
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
    return parser


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        run(parser.parse_args(argv))
    except ContractError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0
